from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.utils.checkpoint

from .mlp import LayerNormQKVLinear
from .rotary import RotaryEmbedding
from impl.model.parallelism.model_parallel.modules import RowParallelLinear
from impl.model.utils.functional import torch_attn_func
import base.logging as logging

try:
    from flash_attn import (flash_attn_func, flash_attn_varlen_func, flash_attn_varlen_func_with_kvcache,
                            flash_attn_with_kvcache)
except ModuleNotFoundError:
    pass
import base.logging as logging

logger = logging.getLogger("Attention")


class CausalSelfAttentionLayer(nn.Module):

    def __init__(
        self,
        hidden_dim: int,
        n_kv_heads: int,
        head_dim: int,
        resid_pdrop: float,
        attn_pdrop: float,
        layer_index: int,
        layer_norm_epsilon: float,
        # gpt2 does not scale attn by inverse layer idx, in contrast to starcoder
        scale_attn_by_inverse_layer_idx: bool,
        # llama does not require attention bias
        use_attention_bias: bool,
        # layer norm type is special for llama
        layer_norm_type: Optional[str] = None,
        # rotary embedding
        apply_rotary: bool = False,
        rotary_base: float = 10000.0,
        rotary_interleaved: bool = False,  # False for LLaMA, GPT-neoX; True for GPT-J
        rotary_scaling: Optional[float] = None,
        rotary_scaling_type: Optional[str] = None,
        # parallel settings
        model_parallel: bool = False,
        sequence_parallel: bool = False,
        gradient_accumulation_fusion: bool = False,
        # device and dtype
        dtype: Optional[torch.dtype] = None,
        device: Optional[Union[str, torch.device]] = None,
    ):
        super().__init__()
        if dtype is None:
            dtype = torch.float16
        assert hidden_dim % head_dim == 0
        n_q_heads = hidden_dim // head_dim
        self.c_attn = LayerNormQKVLinear(
            input_dim=hidden_dim,
            head_dim=head_dim,
            n_q_heads=n_q_heads,
            n_kv_heads=n_kv_heads,
            model_parallel=model_parallel,
            sequence_parallel=sequence_parallel,
            gradient_accumulation_fusion=gradient_accumulation_fusion,
            layer_norm_epsilon=layer_norm_epsilon,
            layer_norm_type=layer_norm_type,
            use_attention_bias=use_attention_bias,
            dtype=dtype,
            device=device,
            layer_index=layer_index,
        )

        if model_parallel:
            self.c_proj = RowParallelLinear(
                hidden_dim,
                hidden_dim,
                bias=use_attention_bias,
                sequence_parallel=sequence_parallel,
                gradient_accumulation_fusion=gradient_accumulation_fusion,
                dtype=dtype,
                device=device,
            )
        else:
            self.c_proj = nn.Linear(
                hidden_dim,
                hidden_dim,
                bias=use_attention_bias,
                dtype=dtype,
                device=device,
            )

        self.resid_dropout = nn.Dropout(resid_pdrop)

        self.attn_pdrop = attn_pdrop

        self.applied_attn_pdrop = attn_pdrop

        self.apply_rotary = apply_rotary
        self.rotary_interleaved = rotary_interleaved
        if self.apply_rotary:
            # Will layzily update the cache sequence length of cache.,
            # so we don't need to pass in max_positions.
            self.rotary_emb = RotaryEmbedding(
                head_dim,
                base=rotary_base,
                scale_factor=rotary_scaling,
                scale_type=rotary_scaling_type,
                interleaved=rotary_interleaved,
                device=device,
            )

        # constant
        self.h = hidden_dim
        self.nq = n_q_heads
        self.nkv = n_kv_heads
        if self.nq % self.nkv != 0:
            raise ValueError(f"n_kv_heads ({self.nkv}) must divide n_q_heads ({self.nq}).")
        self.d = head_dim

        self.layer_index = layer_index

        self.scale_attn_by_inverse_layer_idx = scale_attn_by_inverse_layer_idx

    def train(self, mode: bool):
        if not mode:
            self.applied_attn_pdrop = 0.0
        else:
            self.applied_attn_pdrop = self.attn_pdrop
        super().train(mode)
        return self

    def forward(
        self,
        hidden_states: torch.Tensor,
        cu_seqlens: Optional[torch.Tensor] = None,
        k_cache: Optional[torch.Tensor] = None,
        v_cache: Optional[torch.Tensor] = None,
        cache_seqlens: Optional[Union[int, torch.Tensor]] = None,
        attention_mask: Optional[torch.BoolTensor] = None,  # only used for debugging
        max_seqlen: Optional[int] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # input shape: [bs, seq, hidden_dim]

        # NOTE: we must ensure the passed-in argument is an interger
        # if we convert the argument to implicitly when calling rotary embedding or flash-attn,
        # aten::item will be called, which will cause a device-host sync and slow down performance.
        assert max_seqlen is None or isinstance(max_seqlen, int), type(max_seqlen)
        assert cu_seqlens is None or cu_seqlens.dtype == torch.int32

        # default upcast, scale
        if self.scale_attn_by_inverse_layer_idx:
            unscale = self.layer_index + 1
            scale_factor = unscale**-1
        else:
            unscale = 1.0
            scale_factor = 1
        scale_factor /= self.d**0.5

        q, k, v = self.c_attn(hidden_states)

        if self.apply_rotary and k_cache is None:
            # otherwise, we input rotary cos/sin directly into flash_attn_with_kvcache
            qk = self.rotary_emb(
                torch.cat([q, k], dim=-2),
                cu_seqlens=cu_seqlens,
                max_seqlen=max_seqlen,
            )
            q, k = qk.split((q.shape[-2], k.shape[-2]), dim=-2)
        elif self.apply_rotary:
            self.rotary_emb._update_cos_sin_cache(k_cache.shape[1], device=q.device, dtype=q.dtype)
            # Rotary cos/sin will be automatically offset by cache_seqlens in flash_attn.
            rotary_cos, rotary_sin = self.rotary_emb._cos_cached, self.rotary_emb._sin_cached
        else:
            rotary_cos = rotary_sin = None

        if str(q.device) == "cpu":
            # Use vanilla pytorch attention, for debugging.
            hidden_states = torch_attn_func(
                q,
                k,
                v,
                causal=True,
                dropout_p=self.applied_attn_pdrop,
                softmax_scale=scale_factor,
                upcast_unscale=unscale,
                attention_mask=attention_mask,
            )
        elif k_cache is not None and len(q.shape) == 4:
            # k_cache/v_cache shape: [bs, max_seq, n_kv_heads, head_dim]
            if cache_seqlens is None:
                raise RuntimeError("cache_seqlens must be provided if kv_cache is not None.")
            if not (q.shape[1] == k.shape[1] == v.shape[1] == 1):
                raise RuntimeError(
                    "Can only generate one token at a time, "
                    f"while seqence length (q={q.shape[1]}, k={k.shape[1]}, v={v.shape[1]}) is larger than 1."
                )
            # k_cache and v_cache will be modified in-place.
            hidden_states = flash_attn_with_kvcache(
                q,
                k_cache,
                v_cache,
                k=k,
                v=v,
                cache_seqlens=cache_seqlens,
                softmax_scale=scale_factor,
                causal=False,  # True or False doesn't matter because seqlen=1
                rotary_cos=rotary_cos,
                rotary_sin=rotary_sin,
                rotary_interleaved=self.rotary_interleaved,
            )
        elif k_cache is not None and len(q.shape) == 3:
            hidden_states = flash_attn_varlen_func_with_kvcache(
                q=q,
                cu_seqlens_q=cu_seqlens,
                max_seqlen_q=max_seqlen,
                k_cache=k_cache,
                v_cache=v_cache,
                cache_seqlens=cache_seqlens,
                k=k,
                v=v,
                cu_seqlens_k=cu_seqlens,
                softmax_scale=scale_factor,
                causal=True,
                rotary_cos=rotary_cos,
                rotary_sin=rotary_sin,
                rotary_interleaved=self.rotary_interleaved,
            )
        elif cu_seqlens is not None:
            assert max_seqlen is not None
            assert len(q.shape) == 3
            hidden_states = flash_attn_varlen_func(
                q,
                k,
                v,
                cu_seqlens,
                cu_seqlens,
                max_seqlen,
                max_seqlen,
                dropout_p=self.applied_attn_pdrop,
                softmax_scale=scale_factor,
                causal=True,
            )
        else:
            hidden_states = flash_attn_func(
                q,
                k,
                v,
                dropout_p=self.applied_attn_pdrop,
                softmax_scale=scale_factor,
                causal=True,
            )
        hidden_states = self.c_proj(hidden_states.flatten(start_dim=-2))
        hidden_states = self.resid_dropout(hidden_states)
        return hidden_states, k, v
