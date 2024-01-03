from typing import Dict, List, Optional

import deepspeed
import torch
import torch.utils.data
import tqdm

from base.namedarray import from_dict, NamedArray, recursive_apply
from impl.model.backend.pipe_engine.ds_pipe_engine import DeepSpeedPipelineEngine
from impl.model.nn.flash_mqat.flash_generate import generate, GenerationConfig
from impl.model.utils.functional import (build_leave_one_indices, build_shift_one_indices,
                                         gather_packed_shifted_log_probs)
from impl.model.utils.model_parallel.modules import vocab_parallel_cross_entropy
from impl.model.utils.save_load import save_hf_or_lora_model, save_pipeline_model
import api.data
import api.model
import base.constants
import base.dataparallel

try:
    from flash_attn.bert_padding import unpad_input
except ModuleNotFoundError:
    pass


def compute_packed_sft_loss(
    logits: torch.Tensor,
    packed_input_ids: torch.Tensor,
    cu_seqlens: torch.Tensor,
    prompt_mask: torch.Tensor,
    **kwargs,
) -> torch.Tensor:
    # **kwargs is used to ensure the correctness of invoking this function
    shift_one_indices = build_shift_one_indices(logits, cu_seqlens)
    leave_one_indices = build_leave_one_indices(logits, cu_seqlens)
    if base.constants.model_parallel_world_size() > 1:
        labels = torch.nn.functional.pad(packed_input_ids[1:], (0, 1), "constant", 0)
        # NOTE: logprobs is freaking sensitive to input_ids. If the input sequence is a natural sequence, everything will be fine.
        # However, if we input random token IDs, parallel cross entropy can produce VERY different results than the normal
        # torch.gather based version (e.g., the maximum absolute different can reach ~50).
        logprobs = -vocab_parallel_cross_entropy(logits, labels)[leave_one_indices].float()

        ########### sanity check ###########
        # world_size = base.constants.model_parallel_world_size()
        # dim_size = [logits.shape[1] * world_size, logits.shape[0]]
        # all_gather_buffer = torch.zeros(*dim_size, dtype=logits.dtype, device=logits.device)
        # torch.distributed._all_gather_base(
        #     all_gather_buffer,
        #     logits.transpose(0, 1).contiguous(),
        #     group=base.constants.model_parallel_group(),
        # )
        # logits2 = all_gather_buffer.transpose(0, 1).contiguous()
        # logprobs2 = gather_packed_shifted_log_probs(logits2, cu_seqlens, packed_input_ids).float()
        # assert torch.allclose(logprobs, logprobs2, atol=2e-2), (
        #     (logprobs - logprobs2).abs().max(),
        #     logprobs,
        #     logprobs2,
        # )
        ########### sanity check ###########
    else:
        logprobs = gather_packed_shifted_log_probs(logits, cu_seqlens, packed_input_ids).float()
    prompt_mask = prompt_mask[shift_one_indices]
    # float16 will overflow here
    loss = -torch.where(prompt_mask, 0, logprobs).sum() / (prompt_mask.numel() - prompt_mask.count_nonzero())
    return loss, {"loss": loss.detach()}


class PackedSupervisedFinetuningInterface(api.model.ModelInterface):

    def train_step(self, model: api.model.Model, data: NamedArray) -> Dict:
        data = recursive_apply(data, lambda x: x.to(model.device))
        packed_input_ids: torch.Tensor = data['packed_input_ids']  # shape [tot_seqlen]
        cu_seqlens: torch.Tensor = data['cu_seqlens']
        prompt_mask: torch.BoolTensor = data['prompt_mask']  # shape [tot_seqlen]
        module: deepspeed.DeepSpeedEngine = model.module
        max_seqlen = int(max(cu_seqlens[1:] - cu_seqlens[:-1]))

        module.train()

        if isinstance(module, DeepSpeedPipelineEngine):
            loss_fn_kwargs = dict(
                prompt_mask=prompt_mask,
                input_lens=cu_seqlens[1:] -
                cu_seqlens[:-1],  # this is used to partition other loss_fn_kwargs into microbatches
            )
            loss, _ = module.train_batch(
                packed_input_ids=packed_input_ids,
                cu_seqlens=cu_seqlens,
                loss_fn=compute_packed_sft_loss,
                **loss_fn_kwargs,
            )
        else:
            logits = module(packed_input_ids=packed_input_ids, cu_seqlens=cu_seqlens,
                            max_seqlen=max_seqlen).logits
            loss, _ = compute_packed_sft_loss(logits, packed_input_ids, cu_seqlens, prompt_mask)
            module.backward(loss)
            module.step()

        cur_epoch = model.version.epoch
        model.inc_version()
        if model.version.epoch > cur_epoch:
            module.tput_timer.update_epoch_count()

        res = dict()
        if loss is not None:
            res['loss'] = float(loss)
        return res

    def save(self, model: api.model.Model, save_dir: str):
        if isinstance(model.module, DeepSpeedPipelineEngine):
            save_pipeline_model(model, save_dir)
        else:
            save_hf_or_lora_model(model, save_dir)

    @torch.inference_mode()
    def evaluate(self, model_: api.model.Model, eval_dataloader: torch.utils.data.DataLoader) -> Dict:
        device = model_.device
        module = model_.module

        module.eval()
        losses = 0
        n_seqs = 0

        for step, data in enumerate(tqdm.tqdm(eval_dataloader)):
            data = recursive_apply(from_dict(data), lambda x: x.to(device))
            packed_input_ids: torch.Tensor = data["packed_input_ids"]  # shape [tot_seqlen]
            cu_seqlens: torch.Tensor = data["cu_seqlens"]
            prompt_mask: torch.BoolTensor = data["prompt_mask"]  # shape [tot_seqlen]
            max_seqlen = int(max(cu_seqlens[1:] - cu_seqlens[:-1]))

            if isinstance(module, DeepSpeedPipelineEngine):
                loss_fn_kwargs = dict(
                    prompt_mask=prompt_mask,
                    input_lens=cu_seqlens[1:] - cu_seqlens[:-1],
                )
                loss, _ = module.eval_batch(packed_input_ids,
                                            cu_seqlens,
                                            loss_fn=compute_packed_sft_loss,
                                            **loss_fn_kwargs)
            else:
                logits = module(packed_input_ids=packed_input_ids,
                                cu_seqlens=cu_seqlens,
                                max_seqlen=max_seqlen).logits
                loss, _ = compute_packed_sft_loss(logits, packed_input_ids, cu_seqlens, prompt_mask)

            if loss is not None:
                losses += (cu_seqlens.shape[0] - 1) * loss.float()
                n_seqs += cu_seqlens.shape[0] - 1

        res = dict()
        if n_seqs > 0:
            losses = losses / n_seqs
            try:
                perplexity = torch.exp(losses).item()
            except OverflowError:
                perplexity = float("inf")
            return dict(ppl=perplexity)
        return res

    @torch.inference_mode()
    def inference(self, model: api.model.Model, data: NamedArray) -> Dict:
        device = model.device
        module = model.module
        module.eval()

        data = recursive_apply(data, lambda x: x.to(device))
        packed_input_ids: torch.Tensor = data["packed_input_ids"]
        cu_seqlens: torch.Tensor = data["cu_seqlens"]
        max_seqlen = int((cu_seqlens[1:] - cu_seqlens[:-1]).max())

        if isinstance(module, DeepSpeedPipelineEngine):
            logits = module.forward(packed_input_ids=packed_input_ids, cu_seqlens=cu_seqlens)
            if logits is not None:
                logits = logits
        else:
            logits = model.module(packed_input_ids=packed_input_ids,
                                  cu_seqlens=cu_seqlens,
                                  max_seqlen=max_seqlen).logits
        return dict(logits=logits)

    # for testing only
    @torch.no_grad()
    def generate(self, model: api.model.Model, data: NamedArray, gconfig: GenerationConfig) -> NamedArray:
        module = model.module

        module.eval()

        data = recursive_apply(data, lambda x: x.to(model.device))
        prompts: torch.LongTensor = data["prompts"]
        prompt_att_mask: torch.BoolTensor = data["prompt_att_mask"]
        bs, prompt_max_len = prompts.shape[:2]

        if isinstance(module, DeepSpeedPipelineEngine):
            packed_input_ids, _, cu_seqlens, _ = unpad_input(prompts, prompt_att_mask)

            res = module.generate(
                tokenizer=model.tokenizer,
                packed_input_ids=packed_input_ids,
                cu_seqlens=cu_seqlens,
                gconfig=gconfig,
            )
            if res is None:
                return dict()

            gen_tokens, logprobs, logits_mask, *_ = res
        else:
            # unwrap deepspeed engine here
            module = module.module
            gen_res = module.generate(
                tokenizer=model.tokenizer,
                input_ids=prompts,
                attention_mask=prompt_att_mask,
                gconfig=gconfig,
            )
            gen_tokens = gen_res.sequences
            logprobs = gen_res.scores
            logits_mask = gen_res.logits_mask

        return dict(
            gen_tokens=gen_tokens,
            log_probs=logprobs,
            logits_mask=logits_mask,
        )


api.model.register_interface("flash_sft", PackedSupervisedFinetuningInterface)
