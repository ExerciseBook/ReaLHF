import os
import random

import torch
import torch.multiprocessing as mp

import base.gpu_utils
import base.name_resolve as name_resolve
import base.names as names
import gc
import pynvml
import base.namedarray
from base.topology import ParallelGrid, PipeModelDataParallelTopology
import torch.distributed as dist
import base.constants
import api.model

EXPR_NAME = "test"
TRIAL_NAME = "test"
MODEL_NAME = "default"
WORKER_TYPE = "model_worker"

BARRIER = None


def setup_barrier(world_size):
    global BARRIER
    BARRIER = mp.Barrier(world_size)


def setup_gpu(rank, world_size):
    os.environ["DLLM_MODE"] = "LOCAL"
    os.environ["CUDA_DEVICE_MAX_CONNECTIONS"] = "1"
    # os.environ["CUDA_LAUNCH_BLOCKING"] = "1"

    BARRIER.wait()
    base.gpu_utils.isolate_cuda_device(WORKER_TYPE, rank, world_size, EXPR_NAME, TRIAL_NAME)
    # print(f"rank {rank} isolated cuda device")
    BARRIER.wait()
    base.gpu_utils.reveal_ddp_identity(EXPR_NAME, TRIAL_NAME, rank)
    # print(f"rank {rank} revealed ddp identity")
    BARRIER.wait()
    info = base.gpu_utils.setup_ddp(EXPR_NAME, TRIAL_NAME, rank)
    world_size = info.world_size
    device = torch.device("cuda", 0)
    # print(f"rank {rank} setup ddp")
    import deepspeed

    deepspeed.init_distributed()
    # print(f"rank {rank} setup deepspeed")
    pynvml.nvmlInit()
    pytorch_memory_burnin(rank)
    return device


def clear_name_resolve():
    name_resolve.clear_subtree(names.trial_root(experiment_name=EXPR_NAME, trial_name=TRIAL_NAME))


def make_finetune_spec(bs_per_device, total_train_epochs=1, total_train_steps=10, steps_per_epoch=10):

    finetune_spec = api.model.FinetuneSpec(
        total_train_epochs=total_train_epochs,
        total_train_steps=total_train_steps,
        steps_per_epoch=steps_per_epoch,
        batch_size_per_device=bs_per_device,
    )
    return finetune_spec


def random_sentence(min_len=100, max_len=128):
    words = ["the", "quick", "brown", "fox", "jumped", "over", "the", "lazy", "dog"]
    sentence_length = random.randint(min_len, max_len)
    return " ".join(random.choices(words, k=sentence_length))
    # return "Output less than 50 words:"


def make_input(tokenizer, device, s):
    tokenizer.padding_side = "left"
    prompts = tokenizer(s, return_tensors="pt", padding=True)

    input_ids, attention_mask = prompts["input_ids"], prompts["attention_mask"]
    input_ids = input_ids.to(device)
    attention_mask = attention_mask.to(device)

    print(f"make input input_ids.shape {input_ids.shape}")

    return input_ids, attention_mask


def make_batch(tokenizer, device, batch_size, dp_rank, dp_worldsize, seed=373):
    random.seed(seed)
    whole_batch = [random_sentence() for _ in range(batch_size)]
    dp_batch = whole_batch[batch_size // dp_worldsize * dp_rank : batch_size // dp_worldsize * (dp_rank + 1)]
    return make_input(tokenizer, device, dp_batch)


def init_global_constants(num_dp, num_mp, num_pp, model_name=None):
    if model_name is None:
        model_name = MODEL_NAME

    topo = PipeModelDataParallelTopology(num_dp=num_dp, num_mp=num_mp, num_pp=num_pp)
    ws = num_dp * num_mp * num_pp

    wg = dist.new_group(ranks=range(ws))

    base.constants.set_parallelism_group(model_name=MODEL_NAME, pgroup=wg)
    grid = ParallelGrid(process_group=wg, topology=topo)
    base.constants.set_grid(model_name=MODEL_NAME, grid=grid)
    base.constants.set_experiment_trial_names(EXPR_NAME, TRIAL_NAME)
    base.constants.set_rank_mapping(model_name, topo)
    base.constants.set_max_seqlen(1024)


def init_data(tokenizer, device, batch_size, seed, dp_rank=None, num_dp=None):
    from flash_attn.bert_padding import unpad_input

    if dp_rank == None:
        assert num_dp == None
        dp_rank = base.constants.data_parallel_rank()
        num_dp = base.constants.data_parallel_world_size()
    input_ids, attention_mask = make_batch(tokenizer, device, batch_size, dp_rank % num_dp, num_dp, seed=seed)
    packed_input_ids, _, cu_seqlens, max_seqlen = unpad_input(input_ids, attention_mask)
    prompt_mask = torch.zeros_like(packed_input_ids)
    data = base.namedarray.NamedArray(
        packed_input_ids=packed_input_ids,
        cu_seqlens=cu_seqlens,
        prompts=input_ids,
        prompt_mask=prompt_mask.bool(),
        prompt_att_mask=attention_mask,
    )
    return data


def pytorch_memory_burnin(rank):
    torch.cuda.set_device(rank)
    torch.cuda.init()
    x = torch.randn(1, device="cuda", dtype=torch.float64, requires_grad=True)
    y = x * torch.randn(1000, device="cuda", dtype=torch.float64)
    y.mean().backward()
    del x, y
    gc.collect()
    torch.cuda.empty_cache()
    gc.collect()


def clear_gpu_cache():
    gc.collect()
    torch.cuda.empty_cache()
    gc.collect()


def get_memory(rank):
    handle = pynvml.nvmlDeviceGetHandleByIndex(rank)
    memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
    # total_memory = memory_info.total / (1024**2)  # Convert bytes to megabytes
    used_memory = memory_info.used / (1024**2)
    return used_memory


def get_llama7b_flash_config():
    from impl.model.nn.flash_mqat.flash_mqat_base import FlashMQATConfig

    return FlashMQATConfig(
        n_layers=4,
        n_kv_heads=32,
        head_dim=128,
        hidden_dim=4096,
        intermediate_dim=11008,
        vocab_size=32000,
        n_positions=4096,
        activation_function="silu",
        use_attention_bias=False,
        layer_norm_type="rms",
        mlp_type="llama",
        apply_rotary=True,
    )


def get_pytorch_profiler(save_fn: str):
    def trace_handler(p: torch.profiler._KinetoProfile):
        # print(
        #     p.key_averages().table(
        #         sort_by="cuda_memory_usage", row_limit=20, max_name_column_width=30, max_src_column_width=30
        #     )
        # )
        p.export_chrome_trace(save_fn)

    return torch.profiler.profile(
        activities=[torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA],
        record_shapes=True,
        profile_memory=True,
        with_stack=True,
        on_trace_ready=trace_handler,
        with_flops=True,
    )
