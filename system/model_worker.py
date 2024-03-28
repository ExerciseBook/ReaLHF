from typing import Any, Dict, List, Optional, Tuple
import collections
import gc
import itertools
import multiprocessing as mp
import os
import queue
import socket
import time
import uuid

import deepspeed
import numpy as np
import torch
import torch.distributed as dist
import torch.utils.data

from api.config.config_base import ModelName
from base.monitor import gpu_utilization_monitor, time_mark
from base.topology import ParallelGrid
from impl.model.nn.flash_mqat.flash_mqat_api import FlashMQATModel
import api.config.config_system as config_system
import api.config.dfg
import api.data
import api.model
import base.constants
import base.datapack as datapack
import base.dataparallel as dataparallel
import base.gpu_utils as gpu_utils
import base.logging as logging
import base.namedarray as namedarray
import base.numpy_utils
import base.seeding as seeding
import base.timeutil
import base.topology
import system.request_reply_stream as request_reply_stream
import system.worker_base as worker_base

# Register all implemented datasets and models.
import impl.model  # isort:skip
import impl.dataset  # isort:skip

logger = logging.getLogger("Model Worker", "colored")
blogger = logging.getLogger("benchmark")

TIME_RECORD_RPCS = ["generate", "inference", "train_step", "save", "evaluate", "initialize"]


class NoRequestToHandle(Exception):
    pass


def _get_seqlens_from_batch_sample(sample: namedarray.NamedArray) -> torch.Tensor | None:
    if "input_lens" in sample.keys():
        return sample["input_lens"].cpu().numpy()
    elif "cu_seqlens" in sample.keys():
        return sample["cu_seqlens"][1:] - sample["cu_seqlens"][:-1]
    # NOTE: The order matters. We should first try to get the length of the generated text rather than prompts.
    elif "prompt_lens" in sample.keys():
        return sample["prompt_lens"]
    elif "prompt_cu_seqlens" in sample.keys():
        return sample["prompt_cu_seqlens"][1:] - sample["prompt_cu_seqlens"][:-1]
    else:
        return None


def split_packed_batch_into_seqs(
    sample: namedarray.NamedArray,
    input_lens: Optional[torch.Tensor] = None,
    return_seqlens: bool = False,
) -> List[namedarray.NamedArray]:
    if input_lens is None:
        if "input_lens" in sample:
            input_lens = sample["input_lens"]
        elif "prompt_lens" in sample:
            input_lens = sample["prompt_lens"]
        elif "cu_seqlens" in sample:
            input_lens = sample["cu_seqlens"][1:] - sample["cu_seqlens"][:-1]
        elif "prompt_cu_seqlens" in sample:
            input_lens = sample["prompt_cu_seqlens"][1:] - sample["prompt_cu_seqlens"][:-1]

    partitions = [(i, i + 1) for i in range(input_lens.shape[0])]
    sample["input_lens"] = input_lens
    res = dataparallel.PackedParallelDataBroker.scatter_to(sample,
                                                           n_dp=len(input_lens),
                                                           partitions=partitions)
    if not return_seqlens:
        return res
    else:
        return res, input_lens


def _get_shape_from_key_and_seqlen(k: str, seqlen: int):
    if k in ["input_lens", "prompt_lens", "seq_no_eos_mask", "rewards", "reward_score", "group_factor"]:
        shape = (1,)
    elif k in ["cu_seqlens", "prompt_cu_seqlens"]:
        shape = (2,)
    elif k in ["packed_seq", "prompt_mask", "packed_input_ids", "values", "packed_prompts"]:
        shape = (seqlen,)
    elif k in [
            "packed_logprobs",
            "packed_ref_logprobs",
            "old_logp",
            "ref_logp",
            "advantages",
            "ppo_loss_mask",
            "kl_rewards",
            "returns",
    ]:
        shape = (seqlen - 1,)
    else:
        raise NotImplementedError(f"Unknown key {k} in packed data.")
    return shape


def _get_dtype_from_key(k: str):
    if k in [
            "seq_no_eos_mask",
            "ppo_loss_mask",
            "prompt_mask",
    ]:
        dtype = torch.bool
    elif k in [
            "reward_score",
            "packed_ref_logprobs",
            "old_logp",
            "ref_logp",
            "advantages",
            "kl_rewards",
            "returns",
            "values",
    ]:
        dtype = torch.float16
    elif k in ["input_lens", "prompt_lens", "cu_seqlens", "prompt_cu_seqlens"]:
        dtype = torch.int32
    elif k in ["packed_seq", "packed_input_ids", "packed_prompts"]:
        dtype = torch.int64
    elif k in ["rewards", "packed_logprobs", "group_factor"]:
        dtype = torch.float32
    else:
        raise NotImplementedError(f"Unknown key {k} in packed data.")
    return dtype


def _get_tensor_buffer_from_key_and_seqlen(k: str, seqlen: int):
    shape = _get_shape_from_key_and_seqlen(k, seqlen)
    dtype = _get_dtype_from_key(k)
    return base.constants.get_global_memory_buffer().get_tensor(shape, dtype, name="data_transfer")


class ModelWorker(worker_base.Worker):

    def _configure(self, cfg: config_system.ModelWorker):
        self.config = cfg
        self.model_names = [s.id.model_name for s in cfg.shards]
        self.shard_indices = [
            cfg.model_topos[s.id.model_name].get_rank(data=s.id.dp_rank,
                                                      pipe=s.id.pp_rank,
                                                      model=s.id.mp_rank) for s in cfg.shards
        ]

        self.__experiment_name = self.config.worker_info.experiment_name
        self.__trial_name = self.config.worker_info.trial_name

        self.config.model_rpcs, _ = api.config.dfg.build_graph(self.config.model_rpcs)
        self.data2required_rpc_names = self.config.model_rpcs[0].data2required_rpc_names

        # NOTE: here worker_index is different from peer/ddp rank
        self.__worker_index = cfg.worker_info.worker_index

        torch.backends.cudnn.benchmark = cfg.cudnn_benchmark
        torch.backends.cudnn.deterministic = cfg.cudnn_deterministic

        seeding.set_random_seed(cfg.seed)

        # Reveal DDP identity of this worker to world.
        gpu_utils.reveal_ddp_identity(self.__experiment_name, self.__trial_name, self.__worker_index)
        self.__dist_env_resolved = False

        self.__clear_cache_frequency = base.timeutil.FrequencyControl(
            frequency_steps=self.config.cuda_cache_clear_freq)

        r = self.config.worker_info

        # log info
        self.__total_time = 0.01

        return r

    @property
    def _mp_rank(self) -> int:
        return base.constants.model_parallel_rank()

    @property
    def _pp_rank(self) -> int:
        return base.constants.pipe_parallel_rank()

    @property
    def _dp_rank(self) -> int:
        return base.constants.data_parallel_rank()

    @property
    def _pp_size(self) -> int:
        return base.constants.pipe_parallel_world_size()

    @property
    def _mp_size(self) -> int:
        return base.constants.model_parallel_world_size()

    @property
    def _dp_size(self) -> int:
        return base.constants.data_parallel_world_size()

    @property
    def _is_dp_head(self) -> bool:
        return self._mp_rank == 0 and self._pp_rank == self._pp_size - 1

    @property
    def _model(self) -> api.model.Model:
        return self.__models[base.constants.model_name()]

    @property
    def _interface(self) -> api.model.ModelInterface:
        return self.__interfaces[base.constants.model_name()]

    @property
    def _eval_dataloader(self) -> torch.utils.data.DataLoader:
        return self.__eval_dataloaders[base.constants.model_name()]

    @property
    def _module(self) -> torch.nn.Module | FlashMQATModel:
        return self.__unwrapped_models[base.constants.model_name()]

    @property
    def _backend(self) -> api.model.ModelBackend:
        return self.__backends[base.constants.model_name()]

    def __lazy_setup(self):
        # Add an additional subscript pattern for source RPCs.
        self.__has_dataset = False
        self.__dataset_dp_size = self.__dataset_dp_rank = 0
        sub_patterns = [s.id for s in self.config.shards]
        src_rpc = [rpc for rpc in self.config.model_rpcs if rpc.is_src][0]
        self.__src_rpc_model_name = src_rpc.model_name
        for s in self.config.shards:
            _pp_size = s.id.topo.get_dim("pipe")
            if not (s.id.mp_rank == 0 and s.id.pp_rank == _pp_size - 1):
                continue
            if src_rpc.model_name == s.id.model_name:
                self.__has_dataset = True
                self.__dataset_dp_size = s.id.topo.get_dim("data")
                self.__dataset_dp_rank = s.id.dp_rank
                sub_patterns.append(f"__data{self.__dataset_dp_rank}__")
                break

        # Build stream connecting with master workers.
        self.__stream = request_reply_stream.make_worker_stream(
            self.config.worker_info,
            idx=self.__worker_index,
        )

        self.__pg_info = gpu_utils.setup_ddp(
            expr_name=self.__experiment_name,
            trial_name=self.__trial_name,
            worker_index=self.__worker_index,
            model_topos=self.config.model_topos,
            msid2mwid=self.config.msid2mwid,
            param_sync_pairs=self.config.sync_param_pairs,
            data_transfer_pairs=self.config.data_transfer_pairs,
        )

        base.constants.set_experiment_trial_names(self.__experiment_name, self.__trial_name)

        # logger.info(f"SetUp Information - Model worker index {self.__worker_index} located at "
        #             f"{socket.gethostname()} GPU {self.__pg_info.local_gpu_id}.")

        deepspeed.init_distributed()
        # self.logger.info("deepspeed init distributed on model worker")
        self.__device = torch.device("cuda:0")

        for model_name_, topo_ in self.config.model_topos.items():
            base.constants.set_parallelism_group(
                model_name_,
                self.__pg_info.model_groups[model_name_],
            )
            base.constants.set_rank_mapping(model_name_, topo_, self.config.msid2mwid)
            grid = ParallelGrid(
                topology=topo_,
                rank_mapping=base.constants.rank_mapping_of_model(model_name_),
                process_group=self.__pg_info.model_groups[model_name_],
            )
            base.constants.set_grid(model_name_, grid)

        # Set up training dataset for source RPCs.
        if self.__has_dataset:
            datasets = [
                api.data.make_dataset(
                    d,
                    self.config.seed,
                    self.__dataset_dp_rank,
                    self.__dataset_dp_size,
                    self.config.tokenizer_name_or_path,
                    self.config.worker_info.experiment_name,
                    self.config.worker_info.trial_name,
                    cache_root=(None
                                if not self.config.use_dataset_cache else self.config.dataset_cahce_root),
                ) for d in self.config.datasets
            ]
            if len(self.config.datasets) == 1:
                self.__dataset = datasets[0]
            else:
                self.__dataset = torch.utils.data.ConcatDataset(datasets)
            self.__max_seqlen = max(d.max_seqlen for d in datasets)
            self.__dataloader = api.data.make_dataloader(self.config.dataloader, self.__dataset)
            self.__data_generator = enumerate([])

            self.__dataset_epoch = -1
            self.__dataset_global_step = self.__dataset_epoch_step = 0
            self.__dict_sample = None

            self.__fetched_data = None

        self.__models: Dict[ModelName, api.model.Model] = dict()
        self.__model_is_handle: Dict[ModelName, bool] = dict()
        self.__interfaces: Dict[ModelName, api.model.ModelInterface] = dict()
        self.__eval_dataloaders: Dict[ModelName, torch.utils.data.DataLoader] = dict()

        self.__backends: Dict[ModelName, api.model.ModelBackend] = dict()
        self.__unwrapped_models: Dict[ModelName, torch.nn.Module | FlashMQATModel] = dict()

        self.__backend_initialized: Dict[ModelName, bool] = dict()
        self.__profiler_launched = False

        for s in self.config.shards:
            with base.constants.model_scope(s.id.model_name):
                self.__backend_initialized[s.id.model_name] = False
                self.__models[s.id.model_name] = model = api.model.make_model(s.model,
                                                                              name=s.id.model_name,
                                                                              device=self.__device)
                self.__unwrapped_models[s.id.model_name] = model.module
                if s.id.model_name.replica_id == 0:
                    assert isinstance(model.module, FlashMQATModel)
                    model.module.instantiate()
                    self.__model_is_handle[s.id.model_name] = False
                else:
                    self.__model_is_handle[s.id.model_name] = True
                self.__backends[s.id.model_name] = api.model.make_backend(s.backend)
                interface_impl = [
                    rpc.interface_impl for rpc in self.config.model_rpcs if rpc.model_name == s.id.model_name
                ]
                assert all(x == interface_impl[0] for x in interface_impl)
                self.__interfaces[s.id.model_name] = api.model.make_interface(interface_impl[0])

                if s.eval_datasets is not None and s.eval_dataloader is not None:
                    eval_datasets = [
                        api.data.make_dataset(
                            d,
                            self.config.seed,
                            s.id.dp_rank,
                            s.id.topo.get_dim("data"),
                            self.__models[s.id.model_name].tokenizer,
                            self.config.worker_info.experiment_name,
                            self.config.worker_info.trial_name,
                            cache_root=(None if not self.config.use_dataset_cache else
                                        self.config.dataset_cahce_root),
                        ) for d in s.eval_datasets
                    ]
                    if len(eval_datasets) > 1:
                        eval_dataset = torch.utils.data.ConcatDataset(eval_datasets)
                    else:
                        eval_dataset = eval_datasets[0]
                    eval_dataloader = api.data.make_dataloader(self.config.eval_dataloader, eval_dataset)
                else:
                    eval_dataloader = None
                self.__eval_dataloaders[s.id.model_name] = eval_dataloader

        self.__request_cache = []
        self.__ack_cache = {}

        self.__request_queue = queue.Queue(maxsize=8)
        self.__reply_queue = queue.Queue(maxsize=8)
        self.__request_sample_size = dict()

        # Storing model function call outputs. Model worker will serve as
        # the data producer when other model workers require this data.
        self.__data_owner_storage = dict()
        # Record which RPCs have received the data.
        # After all RPCs have received the data, remove it from storage.
        self.__data_send_record = collections.defaultdict(list)

        self.__compute_input_queues = dict(
            train_step=queue.Queue(4),
            inference=queue.Queue(4),
            generate=queue.Queue(4),
            evaluate=queue.Queue(4),
        )

        # A monitoring process.
        self.__gpu_util_mp = mp.Process(target=gpu_utilization_monitor,
                                        args=(self.__worker_index, 20, 7200))
        self.__gpu_util_mp.start()

    def __prefetch_from_dataset(self):
        if self.__dict_sample is None:
            try:
                self.__dataset_epoch_step, self.__dict_sample = next(self.__data_generator)
            except StopIteration:
                self.__dataset_epoch += 1
                self.__data_generator = enumerate(self.__dataloader)
                self.__dataset_epoch_step, self.__dict_sample = next(self.__data_generator)

    def __handle_one_rpc_hook(self, hook: str, hook_data: Any):
        if hook == "data_transfer":
            # torch.cuda.synchronize()
            tik = time.perf_counter()
            self.__data_transfer_among_workers(hook_data)
            blogger.debug(f"data transfer CPU time: {time.perf_counter() - tik:.4f}s")
            # torch.cuda.synchronize()
        elif hook == "param_sync":
            tik = time.perf_counter()
            # torch.cuda.synchronize()
            from_model_name: ModelName = hook_data["from_model_name"]
            to_model_name: ModelName = hook_data["to_model_name"]
            from_topo: base.topology.PipeModelDataParallelTopology = hook_data["from_topo"]
            to_topo: base.topology.PipeModelDataParallelTopology = hook_data["to_topo"]
            to_model_config = hook_data["to_model_config"]
            if from_model_name in self.__unwrapped_models:
                m = self.__unwrapped_models[from_model_name]
            else:
                m = self.__unwrapped_models[to_model_name]
            assert isinstance(m, FlashMQATModel), type(m)
            new_layers, new_contiguous_param_mem = m.build_reparallelized_layers(
                from_model_name=from_model_name,
                to_model_name=to_model_name,
                from_topo=from_topo,
                to_topo=to_topo,
                to_model_config=to_model_config,
                pg_info=self.__pg_info,
            )
            if from_model_name in self.__models:
                self.__model_is_handle[from_model_name] = True
            if to_model_name in self.__models:
                self.__unwrapped_models[to_model_name].layers = new_layers
                self.__unwrapped_models[to_model_name].contiguous_param = new_contiguous_param_mem
                self.__model_is_handle[to_model_name] = False
            blogger.debug(f"param_sync CPU time: {time.perf_counter() - tik:.4f}s")
        elif hook == "offload":
            tik = time.perf_counter()
            m = self.__unwrapped_models[hook_data["model_name"]]
            assert isinstance(m, FlashMQATModel), type(m)
            m.async_offload()
            blogger.debug(f"async_offload enqueue CUDA request time: {time.perf_counter() - tik:.4f}s")
        else:
            raise NotImplementedError(f"Unknown hook {hook}.")

    def __model_poll_step(self, request: request_reply_stream.Payload, data: Any, handled: bool,
                          res: Optional[Any]) -> worker_base.PollResult:
        tik = time.perf_counter()
        # self.logger.info(f"Model worker {self.__worker_index} #{request.handler}# "
        #                  f"start handle request *{request.handle_name}*, "
        #                  f"request_id {request.request_id}.")

        if isinstance(request.handler, str):
            assert request.handler == f"__data{self.__dataset_dp_rank}__"
            handler_model_name = self.__src_rpc_model_name
        else:
            handler_model_name = request.handler.model_name

        if len(request.pre_hooks) > 0:
            assert len(request.pre_hooks) == len(request.pre_hook_data)
            assert not handled and res is None
            self.__handle_one_rpc_hook(request.pre_hooks.pop(0), request.pre_hook_data.pop(0))
            self.__request_queue.put_nowait((request, data, False, None))
            return
        if handled and len(request.post_hooks) > 0:
            assert len(request.post_hooks) == len(request.post_hook_data)
            self.__handle_one_rpc_hook(request.post_hooks.pop(0), request.post_hook_data.pop(0))
            self.__request_queue.put_nowait((request, data, True, res))
            return
        if handled and len(request.post_hooks) == 0:
            self.__reply_queue.put_nowait((request, res))

            # self.logger.info(f"Model worker {self.__worker_index} #{request.handler}# "
            #                          f"finish handling request *{request.handle_name}*, "
            #                          f"request_id {request.request_id}.")
            sample_count = data.length(0) if isinstance(data, namedarray.NamedArray) else 1
            self.__request_sample_size[request.request_id] = sample_count
            return

        assert not handled and res is None

        with base.constants.model_scope(handler_model_name):
            res = None
            if request.handle_name == "empty":
                # Empty request is used for executing hooks, e.g., data transfer, parameter syncrhonization.
                pass
            ############## initialization ##############
            elif request.handle_name == "initialize":
                assert not self.__model_is_handle[request.handler.model_name]
                base.constants.set_max_seqlen(data.max_seqlen)  # used by cuda graph buffer for generation
                self.__models[request.handler.model_name] = self._backend.initialize(self._model, data)
                self.__backend_initialized[request.handler.model_name] = True
            elif request.handle_name == "model_config":
                assert isinstance(self.__unwrapped_models[request.handler.model_name], FlashMQATModel)
                res = self.__unwrapped_models[request.handler.model_name].config
            ############## data loading ##############
            elif request.handle_name == "fetch":
                assert self.__fetched_data is None
                res = api.data.DataBatch(
                    data=self.__dict_sample,
                    epoch=self.__dataset_epoch,
                    epoch_step=self.__dataset_epoch_step,
                    global_step=self.__dataset_global_step,
                )
                self.__fetched_data = split_packed_batch_into_seqs(namedarray.from_dict(self.__dict_sample))
                self.__dict_sample = None
                self.__dataset_global_step += 1
            elif request.handle_name == "store":
                buffer_indices = request.data
                assert len(buffer_indices) == len(self.__fetched_data)
                for buf_idx, x in zip(buffer_indices, self.__fetched_data):
                    for k, v in x.items():
                        self.__data_owner_storage[(buf_idx, k)] = v.to(self.__device)
                self.__fetched_data = None
                res = None
            elif request.handle_name == "spec":
                if self.__dataloader.batch_size is not None:
                    batch_size = self.__dataloader.batch_size
                else:
                    # We assume that this is a packed dataset. Batch size equals to the number of tokens in the batch.
                    assert isinstance(self.__dataloader.dataset, torch.utils.data.IterableDataset)
                    batch_size = list(self.__dict_sample.values())[0].shape[0]
                res = api.model.FinetuneSpec(
                    total_train_epochs=-1,  # place-holder, to be filled by master worker
                    total_train_steps=-1,  # place-holder, to be filled by master worker
                    steps_per_epoch=len(self.__dataloader),
                    batch_size_per_device=batch_size,
                    max_seqlen=self.__max_seqlen,
                )
            ############## computation function calls ##############
            elif request.handle_name == "inference":
                assert not self.__model_is_handle[request.handler.model_name], request.handler.model_name
                data, buffer_indices, seqlens, output_key_remap = self.__compute_input_queues[
                    request.handle_name].get_nowait()
                res = self._interface.inference(self._model, data)  # -> NamedArray
                if res is not None:
                    new_res = {}
                    for k, v in res.items():
                        if k in output_key_remap:
                            new_res[output_key_remap[k]] = v
                        else:
                            new_res[k] = v
                    new_res = namedarray.from_dict(new_res)
                    res = new_res, buffer_indices, seqlens
            elif request.handle_name == "train_step":
                assert not self.__model_is_handle[request.handler.model_name], request.handler.model_name
                data, *_ = self.__compute_input_queues[request.handle_name].get_nowait()
                res = self._interface.train_step(self._model, data)  # -> Dict
            elif request.handle_name == "generate":
                assert not self.__model_is_handle[request.handler.model_name], request.handler.model_name
                data, buffer_indices, seqlens, output_key_remap = self.__compute_input_queues[
                    request.handle_name].get_nowait()
                res = self._interface.generate(self._model, data)  # -> NamedArray
                if res is not None:
                    new_res = {}
                    for k, v in res.items():
                        if k in output_key_remap:
                            new_res[output_key_remap[k]] = v
                        else:
                            new_res[k] = v
                    new_res = namedarray.from_dict(new_res)
                    res = new_res, buffer_indices, seqlens
            elif request.handle_name == "evaluate":
                assert not self.__model_is_handle[request.handler.model_name], request.handler.model_name
                res = self._interface.evaluate(self._model, self._eval_dataloader)  # -> Dict
            elif request.handle_name == "save":
                assert not self.__model_is_handle[request.handler.model_name], request.handler.model_name
                res = self._interface.save(self._model, data)  # -> None
            else:
                raise NotImplementedError(f"Unknown request type: {request.handle_name}.")

            if request.handle_name in TIME_RECORD_RPCS and self._is_dp_head and self._dp_rank == 0:
                blogger.info(f"Model worker #{handler_model_name}# handle request *{request.handle_name}*"
                             f" in ${time.perf_counter() - tik:.4f}$s")

        self.__request_queue.put_nowait((request, data, True, res))

    def __data_transfer_among_workers(self, hook_data: Dict[str, Any]):
        from impl.model.nn.flash_mqat.flash_mqat_parallel import pipeline_repartition_strategy

        keys = hook_data["keys"]
        target = hook_data["target"]
        global_buffer_indices = hook_data["buffer_indices"]
        global_seqlens = hook_data["seqlens"]

        data = collections.defaultdict(list)
        for k in keys:
            local_buffer_indices = []
            local_seqlens = []

            producer_name = hook_data["producer_names"][k]
            producer_mapping = hook_data["producer_mappings"][(producer_name, k)]
            target_mapping = hook_data["target_mapping"]
            if producer_name not in self.__models and target not in self.__models:
                continue

            # partition mapping starts from zero, which is different from buffer indices
            repart_strat = pipeline_repartition_strategy(producer_mapping, target_mapping)

            target_dp_rank = producer_dp_rank = None
            if target in self.__models:
                with base.constants.model_scope(target):
                    target_dp_rank = base.constants.data_parallel_rank()
            if producer_name in self.__models:
                with base.constants.model_scope(producer_name):
                    producer_dp_rank = base.constants.data_parallel_rank()
                    producer_mp_rank = base.constants.model_parallel_rank()
                    producer_pp_rank = base.constants.pipe_parallel_rank()
                    producer_pp_size = base.constants.pipe_parallel_world_size()
                producer_is_dp_head = producer_mp_rank == 0 and producer_pp_rank == producer_pp_size - 1

            for (dp_i, dp_j), comm_slots in repart_strat.items():
                if len(comm_slots) == 0:
                    continue
                if target_dp_rank == dp_j:
                    # receiver
                    group_key = gpu_utils.DataTransferPair(
                        src=producer_name,
                        src_dp_rank=dp_i,
                        dst=target,
                        dst_dp_rank=dp_j,
                    )
                    bcast_src = self.__pg_info.data_transfer_src_ranks[group_key]
                    group = self.__pg_info.data_transfer_groups[group_key]
                    for _i in comm_slots:
                        buf_idx = global_buffer_indices[_i]
                        seqlen = global_seqlens[_i]
                        if bcast_src == dist.get_rank():
                            v = self.__data_owner_storage[(buf_idx, k)]
                        else:
                            buf = _get_tensor_buffer_from_key_and_seqlen(k, seqlen)
                            dist.broadcast(buf, src=bcast_src, group=group)
                            v = buf.clone()
                        data[k].append(v)
                        local_buffer_indices.append(buf_idx)
                        local_seqlens.append(seqlen)
                if producer_dp_rank == dp_i and producer_is_dp_head:
                    # sender
                    group_key = gpu_utils.DataTransferPair(
                        src=producer_name,
                        src_dp_rank=dp_i,
                        dst=target,
                        dst_dp_rank=dp_j,
                    )
                    bcast_src = self.__pg_info.data_transfer_src_ranks[group_key]
                    group = self.__pg_info.data_transfer_groups[group_key]
                    for _i in comm_slots:
                        buf_idx = global_buffer_indices[_i]
                        v = self.__data_owner_storage[(buf_idx, k)]
                        assert v.dtype == _get_dtype_from_key(k), (k, v.dtype, _get_dtype_from_key(k))
                        assert v.shape == _get_shape_from_key_and_seqlen(k, global_seqlens[_i]), (
                            k,
                            v.shape,
                            global_seqlens[_i],
                        )
                        dist.broadcast(v, src=bcast_src, group=group)
                        # Mark data as sent and remove it from storage if all targets have received it.
                        rpc_name = hook_data["rpc_name"]
                        if rpc_name not in self.__data_send_record[(buf_idx, k)]:
                            self.__data_send_record[(buf_idx, k)].append(rpc_name)
                        if not set(self.data2required_rpc_names[k]).difference(
                                self.__data_send_record[(buf_idx, k)]):
                            self.__data_owner_storage.pop((buf_idx, k))
                            self.__data_send_record.pop((buf_idx, k))

        # if target in self.__models:
        #     with base.constants.model_scope(target):
        #         dist.barrier(group=base.constants.parallelism_group())
        # for producer_name in hook_data['producer_names'].keys():
        #     if producer_name in self.__models:
        #         with base.constants.model_scope(target):
        #             dist.barrier(group=base.constants.parallelism_group())

        if len(data) > 0:
            assert target_dp_rank is not None
            assert len(set(local_buffer_indices)) == len(local_buffer_indices), local_buffer_indices
            input_key_remap = hook_data["input_key_remap"]
            _data = []
            l = len(list(data.values())[0])
            for i in range(l):
                d = {}
                for k, v in data.items():
                    if k in input_key_remap:
                        k = input_key_remap[k]
                    d[k] = v[i]
                _data.append(namedarray.from_dict(d))
            r = dataparallel.PackedParallelDataBroker.gather_from(_data)
            self.__compute_input_queues[hook_data["handle_name"]].put_nowait(
                (r, local_buffer_indices, local_seqlens, hook_data["output_key_remap"]))

    def __post_one_response(self, request: request_reply_stream.Payload, res):
        if isinstance(res, tuple) and isinstance(res[0], namedarray.NamedArray):
            res, buffer_indices, seqlens = res
            new_seqlens = _get_seqlens_from_batch_sample(res)
            reply = request_reply_stream.Payload(
                handler="master",
                request_id=request.request_id,
                handle_name=request.handle_name,
                data={
                    "keys": list(res.keys()),
                    "seqlens": list(seqlens) if new_seqlens is None else new_seqlens.cpu().numpy().tolist(),
                    "buffer_indices": list(buffer_indices),
                },
            )
        else:
            reply = request_reply_stream.Payload(
                handler="master",
                request_id=request.request_id,
                handle_name=request.handle_name,
                data=res,
            )

        self.__stream.post(reply)
        # logger.info(f"handle_name {request.handle_name} Posted req id = {request.request_id}")

        if isinstance(request.handler, config_system.ModelShardID) and isinstance(res, namedarray.NamedArray):
            with base.constants.model_scope(request.handler.model_name):
                if self._is_dp_head:
                    if new_seqlens is None:
                        xs = split_packed_batch_into_seqs(
                            res, torch.tensor(seqlens, device='cuda', dtype=torch.int32))
                    else:
                        xs = split_packed_batch_into_seqs(res, new_seqlens)
                    for buffer_idx, x in zip(buffer_indices, xs):
                        for k, v in x.items():
                            self.__data_owner_storage[(buffer_idx, k)] = v

    def __maybe_post_responses(self):
        ready_to_post = []
        try:
            request, res = self.__reply_queue.get_nowait()
            ready_to_post.append((request, res))
        except queue.Empty:
            pass

        batch_size = sample_size = 0
        for request, res in ready_to_post:
            request: request_reply_stream.Payload
            self.__post_one_response(request, res)
            sample_size += self.__request_sample_size.pop(request.request_id)
            batch_size += 1
        return worker_base.PollResult(sample_count=sample_size, batch_count=batch_size)

    def __maybe_receive_one_request(self):
        try:
            r: request_reply_stream.Payload = self.__stream.poll()
            if r.handle_name == "ack":
                self.__ack_cache[r.request_id] = r
            else:
                self.__stream.post(
                    request_reply_stream.Payload(
                        handler="master",
                        request_id=r.syn_reply_id,
                        handle_name="syn",
                    ),)
                self.__request_cache.append(r)
        except request_reply_stream.NoMessage:
            return

    def __maybe_receive_requests(self):
        for _ in range(8):
            self.__maybe_receive_one_request()

        while len(self.__request_cache) > 0:
            request: request_reply_stream.Payload = self.__request_cache[0]
            while request.ack_reply_id not in self.__ack_cache:
                self.__maybe_receive_one_request()

            self.__ack_cache.pop(request.ack_reply_id)
            self.__request_cache.pop(0)

            self.__request_queue.put_nowait((request, request.data, False, None))

    def _poll(self):
        if not self.__dist_env_resolved:
            self.__lazy_setup()
            self.__dist_env_resolved = True
            # self.tracer.start()
            # self.__profiler = torch.profiler.profile(
            #     activities=[torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA],
            #     # with_stack=True,
            #     # with_flops=True,
            # )

        if self.__has_dataset:
            self.__prefetch_from_dataset()

        st = time.monotonic()
        self.__maybe_receive_requests()

        for _ in range(16):
            # TODO: we can have a smarter scheduling plan, the current plan is basically round-robin
            try:
                request, data, handled, res = self.__request_queue.get_nowait()
                if (request.handle_name in ["train_step", "inference", "generate"]
                        and not self.__profiler_launched):
                    self.tracer.start()
                    # self.__profiler.start()
                    self.__profiler_launched = True
                    self.__profiler_ctl = base.timeutil.FrequencyControl(frequency_seconds=10)
                self.__model_poll_step(request, data, handled, res)
            except queue.Empty:
                break

        r = self.__maybe_post_responses()

        if r.batch_count > 0:
            if self.config.cuda_cache_cleanliness and self.__clear_cache_frequency.check():
                # following huggingface trl # ALWAYS COST 0.3+ SEC
                st = time.monotonic()
                gc.collect()
                torch.cuda.empty_cache()
                gc.collect()
                et = time.monotonic()
                blogger.debug(f"Model worker {self.__worker_index} cleared cache in {et-st:.4f}s")

            # tik = time.perf_counter()
            # blogger.debug(("Model worker #{}#: MemAllocated=*{}*GB, MaxMemAllocated=${}$GB".format(
            #     ",".join(self.model_names),
            #     round(get_accelerator().memory_allocated() / 1024**3, 2),
            #     round(get_accelerator().max_memory_allocated() / 1024**3, 2),
            # )))
            # blogger.debug(f"monitoring overhead {time.perf_counter()-tik}s")
            # if os.environ.get("DLLM_TRACE", "0") == "1":
            #     self.tracer.save()
            if self.__profiler_launched and self.__profiler_ctl.check():
                self.tracer.save()
                # import torch.autograd.profiler as prof
                # self.__profiler.stop()
                # prof.KinetoStepTracker.erase_step_count("ProfilerStep")
                # self.__profiler
                # self.__profiler.stop_trace()
                # self.__profiler_launched = False
                # self.__profiler.export_chrome_trace(self._tracer_output_file)

        t = time.monotonic() - st
        self.__total_time += t
        # blogger.debug(
        #     f"Model worker #{','.join(self.model_names)}# poll time: {t:.4f}s, engine poll time {pt:.4f}s, percent {pt/t:.4f}"
        # )
        return r
