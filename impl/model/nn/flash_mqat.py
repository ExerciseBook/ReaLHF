from typing import Callable, List, Optional, Tuple, Union
import copy
import dataclasses
import math
import os

import torch
import torch.nn as nn
import torch.nn.functional as F
import transformers

from impl.model.utils.data import (build_packed_inputs, mask_eos_token, repeat_kv,
                                   TensorDataclassToTupleInterface, upcast_masked_softmax, upcast_softmax)
from impl.model.utils.logits_warper import top_k_top_p_logits
from impl.model.utils.modules import LayerNormLinear, LayerNormMLP
import api.model

try:
    from flash_attn import flash_attn_func, flash_attn_varlen_func, flash_attn_with_kvcache
except ModuleNotFoundError:
    pass


@dataclasses.dataclass
class FlashMQATConfig:
    n_layers: int
    n_kv_heads: int
    head_dim: int
    hidden_dim: int
    intermediate_dim: int  # for mlp, usually 4*h
    vocab_size: int
    n_positions: int
    embd_pdrop: float = 0.1
    resid_pdrop: float = 0.1
    attn_pdrop: float = 0.1
    layer_norm_epsilon: float = 1e-5
    activation_function: str = "gelu"


@dataclasses.dataclass
class PipeTransferData(TensorDataclassToTupleInterface):
    """Data structure for transferring data between stages.

    Attributes:
        pp_input: The input to the current stage. Usually hidden states
            with shape [bs, seq_len, hidden_dim].
        pp_output: The output of the current stage, also the input to the next stage.
            Usually hidden states with shape [bs, seq_len, hidden_dim].
        cu_seqlens: The cumulative sequence lengths of packed input_ids.
            Used by flash_attn_varlen_func. Will not be used during generation.
            Shape [bs + 1].
        max_seqlen: The maximum sequence length of packed input_ids.
            Used by flash_attn_varlen_func. Will not be used during generation.
        head_mask: The head mask for attention. Use case not clear.
    """
    pp_input: Optional[torch.Tensor] = None
    pp_output: Optional[torch.Tensor] = None

    # The followings are "configuration"-like data that should be passed across all stages.
    cu_seqlens: Optional[torch.Tensor] = None
    max_seqlen: Optional[int] = None
    head_mask: Optional[torch.Tensor] = None

    # Only used for debugging
    attention_mask: Optional[torch.Tensor] = None


@dataclasses.dataclass
class PipeCacheData(TensorDataclassToTupleInterface):
    """Data structure for caching data locally that will not be trasferred.

    Attributes:
        input_ids: The input token ids. Used only at the first stage.
            Can be packed with shape [total_seq_len] or unpacked with shape [bs, seq].
        position_ids: Input position IDs. Can be resolved automatically in most cases.
            Used only at the first stage. The same shape as input_ids.
        k_cache: Key cache used for generation, shape [bs, max_seq, n_kv_heads, head_dim].
            Note that this is the cache for a specific layer, not for all layers.
        v_cache: Value cache used for generation, shape [bs, max_seq, n_kv_heads, head_dim].
            Note that this is the cache for a specific layer, not for all layers.
        cache_seqlens: The sequence lengths of the cached tokens. Used for generation. Shape [bs]. 
    """
    # Only cached in the first stage.
    input_ids: Optional[torch.Tensor] = None
    position_ids: Optional[torch.Tensor] = None
    # Cached in each transformer layer.
    k_cache: Optional[torch.Tensor] = None
    v_cache: Optional[torch.Tensor] = None
    cache_seqlens: Optional[torch.Tensor] = None


def torch_attn_func(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    causal: bool,
    dropout_p: float,
    softmax_scale: float,
    upcast_unscale: float = 1.0,
    attention_mask: Optional[torch.Tensor] = None,
):
    """We don't use pytorch efficient kernels here for debugging."""
    n_rep = q.shape[-2] // k.shape[-2]
    bsz, seqlen = q.shape[:2]
    # repeat k/v heads if n_kv_heads < n_heads
    k = repeat_kv(k, n_rep)  # (bs, seqlen, nq, head_dim)
    v = repeat_kv(v, n_rep)  # (bs, seqlen, nq, head_dim)

    q = q.transpose(1, 2)  # (bs, nq, seqlen, head_dim)
    k = k.transpose(1, 2)
    v = v.transpose(1, 2)
    scores = torch.matmul(q, k.transpose(2, 3)) * softmax_scale
    if attention_mask is not None:
        mask_softmax = True
        mask = attention_mask
    elif causal:
        mask_softmax = True
        mask = torch.tril(torch.ones(seqlen, seqlen, device=q.device, dtype=torch.bool))
    else:
        mask_softmax = False
    if mask_softmax:
        scores = upcast_masked_softmax(scores,
                                       mask,
                                       mask_value=torch.full([],
                                                             torch.finfo(torch.float32).min,
                                                             device=scores.device,
                                                             dtype=torch.float32),
                                       scale=upcast_unscale,
                                       softmax_dtype=torch.float32)
    else:
        scores = upcast_softmax(scores, scale=upcast_unscale, softmax_dtype=torch.float32)
    scores = nn.functional.dropout(scores, p=dropout_p)
    scores = scores.to(q.dtype)
    output = torch.matmul(scores, v)  # (bs, nq, seqlen, head_dim)
    output = output.transpose(1, 2).contiguous()
    return output


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
        dtype: Optional[torch.dtype] = None,
        device: Optional[Union[str, torch.device]] = None,
    ):
        super().__init__()
        if dtype is None:
            dtype = torch.float16
        assert hidden_dim % head_dim == 0
        n_q_heads = hidden_dim // head_dim
        self.c_attn = LayerNormLinear(hidden_dim,
                                      head_dim * (n_q_heads + 2 * n_kv_heads),
                                      layer_norm_epsilon=layer_norm_epsilon,
                                      dtype=dtype,
                                      device=device)
        self.c_proj = nn.Linear(hidden_dim, hidden_dim, dtype=dtype, device=device)
        self.resid_dropout = nn.Dropout(resid_pdrop)

        self.attn_pdrop = attn_pdrop

        self.applied_attn_pdrop = attn_pdrop

        # constant
        self.h = hidden_dim
        self.nq = n_q_heads
        self.nkv = n_kv_heads
        if self.nq % self.nkv != 0:
            raise ValueError("n_kv_heads must divide n_q_heads")
        self.d = head_dim

        self.layer_index = layer_index

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
            max_seqlen: Optional[int] = None,
            k_cache: Optional[torch.Tensor] = None,
            v_cache: Optional[torch.Tensor] = None,
            cache_seqlens: Optional[Union[int, torch.Tensor]] = None,
            attention_mask: Optional[torch.BoolTensor] = None,  # only used for debugging
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # input shape: [bs, seq, hidden_dim]
        # default upcast, scale
        unscale = self.layer_index + 1
        scale_factor = unscale**-1
        scale_factor /= self.d**0.5

        qkv: torch.Tensor = self.c_attn(hidden_states)
        if str(qkv.device) == 'cpu':
            # Use vanilla pytorch attention, for debugging.
            q, k, v = torch.split(qkv, (self.d * self.nq, self.d * self.nkv, self.d * self.nkv), dim=-1)
            k = k.view(*k.shape[:2], self.nkv, self.d)
            v = v.view(*v.shape[:2], self.nkv, self.d)
            q = q.view(*q.shape[:2], self.nq, self.d)
            hidden_states = torch_attn_func(q,
                                            k,
                                            v,
                                            causal=True,
                                            dropout_p=self.applied_attn_pdrop,
                                            softmax_scale=scale_factor,
                                            upcast_unscale=unscale,
                                            attention_mask=attention_mask)
        elif k_cache is not None:
            # k_cache/v_cache shape: [bs, max_seq, n_kv_heads, head_dim]
            if cache_seqlens is None:
                raise RuntimeError("cache_seqlens must be provided if kv_cache is not None.")
            assert qkv.shape[1] == 1, (qkv.shape, "Can only generate one token at a time.")
            q, k, v = torch.split(qkv, (self.d * self.nq, self.d * self.nkv, self.d * self.nkv), dim=-1)
            q = q.view(*q.shape[:2], self.nq, self.d)
            v = v.view(*v.shape[:2], self.nkv, self.d)
            k = k.view(*k.shape[:2], self.nkv, self.d)
            # k_cache and v_cache will be modified in-place.
            hidden_states = flash_attn_with_kvcache(q,
                                                    k_cache,
                                                    v_cache,
                                                    k,
                                                    v,
                                                    cache_seqlens,
                                                    scale_factor,
                                                    causal=False)
        elif cu_seqlens is not None:
            assert max_seqlen is not None
            assert len(qkv.shape) == 2
            q, k, v = torch.split(qkv, (self.d * self.nq, self.d * self.nkv, self.d * self.nkv), dim=-1)
            q = q.view(q.shape[0], self.nq, self.d)
            v = v.view(v.shape[0], self.nkv, self.d)
            k = k.view(k.shape[0], self.nkv, self.d)
            hidden_states = flash_attn_varlen_func(q,
                                                   k,
                                                   v,
                                                   cu_seqlens.int(),
                                                   cu_seqlens.int(),
                                                   int(max_seqlen),
                                                   int(max_seqlen),
                                                   dropout_p=self.applied_attn_pdrop,
                                                   softmax_scale=scale_factor,
                                                   causal=True)
        else:
            q, k, v = torch.split(qkv, (self.d * self.nq, self.d * self.nkv, self.d * self.nkv), dim=-1)
            k = k.view(*k.shape[:2], self.nkv, self.d)
            v = v.view(*v.shape[:2], self.nkv, self.d)
            q = q.view(*q.shape[:2], self.nq, self.d)
            hidden_states = flash_attn_func(q,
                                            k,
                                            v,
                                            dropout_p=self.applied_attn_pdrop,
                                            softmax_scale=scale_factor,
                                            causal=True)
        hidden_states = self.c_proj(hidden_states.flatten(start_dim=-2))
        hidden_states = self.resid_dropout(hidden_states)
        return hidden_states, k, v


class FlashMQATBlock(nn.Module):

    def __init__(
        self,
        config: FlashMQATConfig,
        layer_index: int,
        output_layernorm: bool = False,
        dtype: Optional[torch.dtype] = None,
        device: Optional[Union[str, torch.device]] = None,
    ):
        super().__init__()
        self.attn = CausalSelfAttentionLayer(hidden_dim=config.hidden_dim,
                                             n_kv_heads=config.n_kv_heads,
                                             head_dim=config.head_dim,
                                             resid_pdrop=config.resid_pdrop,
                                             attn_pdrop=config.attn_pdrop,
                                             layer_index=layer_index,
                                             layer_norm_epsilon=config.layer_norm_epsilon,
                                             dtype=dtype,
                                             device=device)
        self.mlp = LayerNormMLP(hidden_dim=config.hidden_dim,
                                intermediate_dim=config.intermediate_dim,
                                resid_pdrop=config.resid_pdrop,
                                activation_function=config.activation_function,
                                layer_norm_epsilon=config.layer_norm_epsilon,
                                dtype=dtype,
                                device=device)
        self.output_layernorm = output_layernorm
        if output_layernorm:
            self.ln_f = nn.LayerNorm(config.hidden_dim,
                                     eps=config.layer_norm_epsilon,
                                     dtype=dtype,
                                     device=device)

    def forward(self, x: PipeTransferData, y: PipeCacheData) -> PipeTransferData:
        h = x.pp_input
        attn_out, k, v = self.attn(
            hidden_states=h,
            cu_seqlens=x.cu_seqlens,
            max_seqlen=x.max_seqlen,
            k_cache=y.k_cache,
            v_cache=y.v_cache,
            cache_seqlens=y.cache_seqlens,
            attention_mask=x.attention_mask,
        )
        h = h + attn_out
        h = self.mlp(h) + h
        if self.output_layernorm:
            h = self.ln_f(h)
        x.pp_output = h
        if y.k_cache is None:
            y.k_cache = k.detach()
        if y.v_cache is None:
            y.v_cache = v.detach()
        if y.cache_seqlens is None and x.cu_seqlens is not None:
            y.cache_seqlens = x.cu_seqlens[1:] - x.cu_seqlens[:-1]
        return x


class VocabPositionEmbedding(nn.Module):

    def __init__(self,
                 vocab_size: int,
                 n_positions: int,
                 hidden_dim: int,
                 dtype: Optional[torch.dtype] = None,
                 device: Optional[Union[str, torch.device]] = None):
        super().__init__()
        self.wte = nn.Embedding(vocab_size, hidden_dim, dtype=dtype, device=device)
        self.wpe = nn.Embedding(n_positions, hidden_dim, dtype=dtype, device=device)

        self.self_attention_mask = torch.tril(
            torch.ones((n_positions, n_positions), dtype=torch.bool, device=device))

    def forward(self, x: PipeTransferData, y: PipeCacheData) -> PipeTransferData:
        is_gen = y.cache_seqlens is not None
        if is_gen and len(y.input_ids.shape) == 2:
            assert y.input_ids.shape[1] == 1
        elif is_gen and len(y.input_ids.shape) == 1:
            y.input_ids = y.input_ids.unsqueeze(-1)
        packed = (len(y.input_ids.shape) == 1)
        if packed and ((x.cu_seqlens is None) or (x.max_seqlen is None)):
            raise ValueError("cu_seqlens and max_seqlen must be both provided for packed input.")

        # Set position ids.
        if not packed and y.position_ids is None:
            # input_ids is given
            batch_size, input_length = y.input_ids.shape
            device = y.input_ids.device
            y.position_ids = torch.arange(input_length, dtype=torch.long, device=device)
            if y.cache_seqlens is not None:  # during generation
                y.position_ids = y.position_ids + y.cache_seqlens.unsqueeze(1)
            else:
                y.position_ids = y.position_ids.repeat(batch_size, 1)
        elif y.position_ids is None:
            # packed_input_ids is given
            lengths = x.cu_seqlens[1:] - x.cu_seqlens[:-1]
            y.position_ids = torch.cat(
                [torch.arange(int(l), dtype=torch.int32, device=y.input_ids.device) for l in lengths])
            assert y.position_ids.shape == y.input_ids.shape

        if x.attention_mask is not None:
            # For debugging only.
            # create position_ids on the fly for batch generation
            attention_mask = x.attention_mask
            y.position_ids = attention_mask.long().cumsum(-1) - 1
            y.position_ids.masked_fill_(attention_mask == 0, 1)
            seqlen = y.input_ids.shape[-1]
            self_attention_mask = self.self_attention_mask[None, :seqlen, :seqlen]
            self_attention_mask = self_attention_mask * attention_mask.view(batch_size, 1, -1).to(
                dtype=torch.bool, device=self_attention_mask.device)
            x.attention_mask = self_attention_mask.unsqueeze(1)

        inputs_embeds = self.wte(y.input_ids)
        position_embeds = self.wpe(y.position_ids)
        x.pp_output = inputs_embeds + position_embeds
        return x


class FlashMQATBase(nn.Module):

    def __init__(
        self,
        config: FlashMQATConfig,
        dtype: Optional[torch.dtype] = None,
        device: Optional[Union[str, torch.device]] = None,
    ):
        super().__init__()
        self.config = config
        self.embedding_layer = VocabPositionEmbedding(config.vocab_size,
                                                      config.n_positions,
                                                      config.hidden_dim,
                                                      dtype=dtype,
                                                      device=device)
        self.h = nn.ModuleList([
            FlashMQATBlock(config,
                           layer_index=i,
                           output_layernorm=(i == config.n_layers - 1),
                           dtype=dtype,
                           device=device) for i in range(config.n_layers)
        ])

    def to_layers(self) -> List[nn.Module]:
        return [self.embedding_layer] + list(self.h)

    def forward(self, x: PipeTransferData, ys: List[PipeCacheData]) -> PipeTransferData:
        layers = self.to_layers()
        assert len(ys) == len(layers)
        raw_pp_input = x.pp_input
        for layer, y in zip(layers, ys):
            x = layer(x, y)  # This will set pp_output.
            x.pp_input = x.pp_output
        # Finally, pp_input is the input of this pipeline stage (maybe across several layers),
        # pp_output is the output of this pipeline stage.
        # In the first stage, pp_input is None.
        x.pp_input = raw_pp_input
        return x


class LanguageModelHead(nn.Linear):

    def forward(self, x: PipeTransferData, ys: List[PipeCacheData]) -> PipeTransferData:
        x.pp_output = nn.functional.linear(x.pp_input, self.weight, self.bias)
        return x


class FlashMQATForCausalLM(nn.Module):

    def __init__(
        self,
        config: FlashMQATConfig,
        dtype: Optional[torch.dtype] = None,
        device: Optional[Union[str, torch.device]] = None,
    ):
        super().__init__()
        self.config = config
        self.transformer = FlashMQATBase(config, dtype=dtype, device=device)
        self.lm_head = LanguageModelHead(
            config.hidden_dim,
            config.vocab_size,
            bias=False,
            device=device,
            dtype=dtype,
        )

    def to_layers(self) -> List[nn.Module]:
        return self.transformer.to_layers() + [self.lm_head]

    def forward(self, x: PipeTransferData, ys: List[PipeCacheData]) -> PipeTransferData:
        layers = self.to_layers()
        assert len(ys) == len(layers)
        raw_pp_input = x.pp_input
        for layer, y in zip(layers, ys):
            x = layer(x, y)  # This will set pp_output.
            x.pp_input = x.pp_output
        # Finally, pp_input is the input of this pipeline stage (maybe across several layers),
        # pp_output is the output of this pipeline stage.
        # In the first stage, pp_input is None.
        x.pp_input = raw_pp_input
        return x

    @classmethod
    def from_starcoder(
        cls,
        from_model: Optional[transformers.PreTrainedModel] = None,
        model_path: Optional[str] = None,
        dtype: Optional[torch.dtype] = None,
        device: Optional[Union[str, torch.device]] = None,
    ):
        if from_model is None:
            assert model_path is not None
            starcoder_config = transformers.AutoConfig.from_pretrained(os.path.join(
                model_path, "config.json"))
        else:
            starcoder_config = from_model.config
        config = FlashMQATConfig(
            n_layers=starcoder_config.n_layer,
            n_kv_heads=1,
            attn_pdrop=starcoder_config.attn_pdrop,
            embd_pdrop=starcoder_config.embd_pdrop,
            layer_norm_epsilon=starcoder_config.layer_norm_epsilon,
            hidden_dim=starcoder_config.n_embd,
            head_dim=starcoder_config.n_embd // starcoder_config.n_head,
            intermediate_dim=starcoder_config.n_inner,
            n_positions=starcoder_config.n_positions,
            resid_pdrop=starcoder_config.resid_pdrop,
            vocab_size=starcoder_config.vocab_size,
        )
        model = cls(config, dtype=dtype, device=device)

        if from_model is None:
            try:
                state_dict = torch.load(os.path.join(model_path, "pytorch_model.bin"))
            except FileNotFoundError:
                state_dict = transformers.AutoModelForCausalLM.from_pretrained(model_path).state_dict()
        else:
            state_dict = from_model.state_dict()

        new_state_dict = {}
        replace_from = [
            ".wte",
            ".wpe",
            ".ln_1.",
            ".ln_2.",
            ".c_attn.weight",
            ".c_attn.bias",
            "transformer.ln_f.",
        ]
        replace_to = [
            ".embedding_layer.wte",
            ".embedding_layer.wpe",
            ".attn.c_attn.ln.",
            ".mlp.ln.",
            ".c_attn.linear.weight",
            ".c_attn.linear.bias",
            f"transformer.h.{config.n_layers - 1}.ln_f.",
        ]
        for k, v in state_dict.items():
            for rf, rt in zip(replace_from, replace_to):
                if rf in k:
                    k = k.replace(rf, rt)
            new_state_dict[k] = v
        model.load_state_dict(new_state_dict)
        return model


@dataclasses.dataclass
class GenerationConfig:
    min_new_tokens: int = 1
    max_new_tokens: int = 10
    temperature: float = 1.0
    greedy: bool = True
    top_p: float = 1.0
    top_k: int = 0
    num_samples: int = 1


def genstep(
    next_token_logits: torch.Tensor,
    tokenizer: transformers.PreTrainedTokenizerFast,
    unfinished_sequences: torch.Tensor,
    generated_idx: int,
    gconfig: GenerationConfig,
) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor], bool, torch.Tensor]:
    """Advance generation by one step given logits.

    Args:
        next_token_logits (torch.Tensor): Shape [bs, vocab_size].
        tokenizer (transformers.PreTrainedTokenizerFast): .
        unfinished_sequences (torch.Tensor): Bool tensor indicator of whether a sequence is finished.
            Shape [bs].
        generated_idx (int): The token index to be generated.
        gconfig (GenerationConfig): .

    Returns:
        Tuple[torch.Tensor, torch.Tensor, torch.Tensor, bool, torch.Tensor]: 
    """

    next_token_logits = next_token_logits.float()
    if generated_idx < gconfig.min_new_tokens:
        next_token_logits = mask_eos_token(next_token_logits, eos_token_id=tokenizer.eos_token_id)

    if not gconfig.greedy:
        next_token_logits /= gconfig.temperature
        next_token_logits = top_k_top_p_logits(
            next_token_logits,
            top_k=gconfig.top_k,
            top_p=gconfig.top_p,
            inplace=True,
            ordered=False,
        )

    distrb = torch.distributions.Categorical(logits=next_token_logits)
    next_tokens = distrb.mode if gconfig.greedy else distrb.sample()
    logprob = distrb.log_prob(next_tokens)

    if tokenizer.eos_token_id is not None:
        if tokenizer.pad_token_id is None:
            raise ValueError("If `eos_token_id` is defined, make sure that `pad_token_id` is defined.")
        next_tokens = next_tokens * unfinished_sequences + tokenizer.pad_token_id * (1 - unfinished_sequences)
    unfinished_sequences = next_tokens.ne(tokenizer.eos_token_id).long() * unfinished_sequences

    # terminate check
    terminate = (generated_idx >= gconfig.max_new_tokens - 1) or (unfinished_sequences.max() == 0)

    logits_mask = next_token_logits != torch.finfo(next_token_logits.dtype).min
    if logits_mask.all():
        logits_mask = None

    return next_tokens, logprob, logits_mask, terminate, unfinished_sequences


@torch.no_grad()
def generate(
    model: api.model.NeuralNetwork,
    tokenizer: transformers.PreTrainedTokenizerFast,
    input_ids: torch.Tensor,
    attention_mask: Optional[torch.Tensor] = None,
    k_caches: Optional[List[torch.Tensor]] = None,
    v_caches: Optional[List[torch.Tensor]] = None,
    cache_seqlens: Optional[torch.Tensor] = None,
    gconfig: GenerationConfig = dataclasses.field(default_factory=GenerationConfig),
) -> Tuple[torch.Tensor, torch.Tensor]:
    if (k_caches is None) != (v_caches is None) or (k_caches is None) != (cache_seqlens is None):
        raise ValueError("k_cache, v_cache, cache_seqlens must be all None or all not None")
    device = input_ids.device
    mconfig: FlashMQATConfig = model.config
    bs, prompt_padded_len = input_ids.shape[:2]

    terminate = False
    generated_idx = 0
    unfinished_sequences = torch.ones(bs, dtype=torch.long, device=device)

    gen_token_ph = []
    gen_logprob_ph = []
    gen_logits_mask_ph = []

    # Prepare inputs for generation iterations
    if k_caches is None:
        # Generate from scratch.
        # Input_ids may have different lengths, we should first pack them into a large batch
        # to use varlen flash attention, then record kv caches for the following inferences.
        packed_input_ids, cu_seqlens, max_seq_len = build_packed_inputs(input_ids, attention_mask)
        input_lens = cu_seqlens[1:] - cu_seqlens[:-1]

        x = PipeTransferData(cu_seqlens=cu_seqlens, max_seqlen=max_seq_len)
        # one embedding layer, n_layers transformer block, one output layer
        ys = [PipeCacheData(input_ids=packed_input_ids)
              ] + [PipeCacheData() for _ in range(mconfig.n_layers + 1)]
        # Model forward will set k/v cache in PipeCacheData.
        logits = model(x, ys).pp_output
        logits = logits[cu_seqlens[1:] - 1]
        for y in ys[1:-1]:
            assert y.k_cache is not None and y.v_cache is not None and y.cache_seqlens is not None
            k_cache = torch.zeros((bs, max_seq_len + gconfig.max_new_tokens, *y.k_cache.shape[1:]),
                                  dtype=y.k_cache.dtype,
                                  device=device)
            v_cache = torch.zeros((bs, max_seq_len + gconfig.max_new_tokens, *y.v_cache.shape[1:]),
                                  dtype=y.v_cache.dtype,
                                  device=device)
            for i in range(bs):
                k_cache[i, :input_lens[i]] = y.k_cache[cu_seqlens[i]:cu_seqlens[i + 1]]
                v_cache[i, :input_lens[i]] = y.v_cache[cu_seqlens[i]:cu_seqlens[i + 1]]
            y.k_cache = k_cache
            y.v_cache = v_cache
            y.cache_seqlens = input_lens.clone()
        x = PipeTransferData()
        ys[0].cache_seqlens = input_lens.clone()
        # Next, we will generate the next token after prompts.
        # cache_seqlens is exactly the lengths of prompts.
        next_tokens, logprob, logits_mask, terminate, unfinished_sequences = genstep(
            logits, tokenizer, unfinished_sequences, generated_idx, gconfig)
        gen_token_ph.append(next_tokens)
        gen_logprob_ph.append(logprob)
        gen_logits_mask_ph.append(logits_mask)
        generated_idx += 1
    else:
        # Resume from a previous generation state.
        if prompt_padded_len != 1:
            raise ValueError("prompt_padded_len must be 1 when resuming from a previous generation state.")
        x = PipeTransferData()
        ys = [PipeCacheData(input_ids=input_ids, cache_seqlens=cache_seqlens.clone())] + [
            PipeCacheData(k_cache=k, v_cache=v, cache_seqlens=cache_seqlens.clone())
            for k, v in zip(k_caches, v_caches)
        ] + [PipeCacheData()]
        next_tokens = input_ids[:, -1]

    # The main loop.
    while not terminate:
        # the next round of inference
        ys[0].input_ids = next_tokens.unsqueeze(-1)  # [bs, 1]
        ys[0].position_ids = None
        # K/v cache will be changed in-place with flash attention.
        logits = model(x, ys).pp_output.squeeze()
        for yidx, y in enumerate(ys[:-1]):
            y.cache_seqlens += 1

        next_tokens, logprob, logits_mask, terminate, unfinished_sequences = genstep(
            logits, tokenizer, unfinished_sequences, generated_idx, gconfig)
        gen_token_ph.append(next_tokens)
        gen_logprob_ph.append(logprob)
        gen_logits_mask_ph.append(logits_mask)
        generated_idx += 1

    gen_tokens = torch.stack(gen_token_ph, -1)
    log_probs = torch.stack(gen_logprob_ph, -1)
    if all([m is None for m in gen_logits_mask_ph]):
        logits_mask = None
    else:
        mm = next(m for m in gen_logits_mask_ph if m is not None)
        gen_logits_mask_ph = [torch.zeros_like(mm) if m is None else m for m in gen_logits_mask_ph]
        logits_mask = torch.stack(gen_logits_mask_ph, -2)

    return gen_tokens, log_probs, logits_mask


@torch.no_grad()
def vanilla_packed_generate(
    model: api.model.NeuralNetwork,
    tokenizer: transformers.PreTrainedTokenizerFast,
    input_ids: torch.Tensor,
    attention_mask: Optional[torch.Tensor] = None,
    gconfig: GenerationConfig = dataclasses.field(default_factory=GenerationConfig),
) -> Tuple[torch.Tensor, torch.Tensor]:
    mconfig: FlashMQATConfig = model.config

    terminate = False
    generated_idx = 0
    unfinished_sequences = torch.ones(input_ids.shape[0], dtype=torch.long, device=input_ids.device)

    gen_token_ph = []
    gen_logprob_ph = []
    gen_logits_mask_ph = []

    # The main loop.
    while not terminate:
        packed_input_ids, cu_seqlens, max_seq_len = build_packed_inputs(input_ids, attention_mask)
        x = PipeTransferData(cu_seqlens=cu_seqlens, max_seqlen=max_seq_len)
        # one embedding layer, n_layers transformer block, one output layer
        ys = [PipeCacheData(input_ids=packed_input_ids)
              ] + [PipeCacheData() for _ in range(mconfig.n_layers + 1)]
        # Model forward will set k/v cache in PipeCacheData.
        logits = model(x, ys).pp_output
        logits = logits[cu_seqlens[1:] - 1]
        # Next, we will generate the next token after prompts.
        # cache_seqlens is exactly the lengths of prompts.
        next_tokens, logprob, logits_mask, terminate, unfinished_sequences = genstep(
            logits, tokenizer, unfinished_sequences, generated_idx, gconfig)
        gen_token_ph.append(next_tokens)
        gen_logprob_ph.append(logprob)
        gen_logits_mask_ph.append(logits_mask)
        generated_idx += 1

        input_ids = torch.cat([input_ids, next_tokens.unsqueeze(-1)], 1)
        am = torch.logical_and(
            next_tokens.unsqueeze(-1).not_equal(tokenizer.eos_token_id),
            next_tokens.unsqueeze(-1).not_equal(tokenizer.pad_token_id))
        attention_mask = torch.cat([attention_mask, am], 1)

    gen_tokens = torch.stack(gen_token_ph, -1)
    log_probs = torch.stack(gen_logprob_ph, -1)
    if all([m is None for m in gen_logits_mask_ph]):
        logits_mask = None
    else:
        mm = next(m for m in gen_logits_mask_ph if m is not None)
        gen_logits_mask_ph = [torch.zeros_like(mm) if m is None else m for m in gen_logits_mask_ph]
        logits_mask = torch.stack(gen_logits_mask_ph, -2)

    return gen_tokens, log_probs, logits_mask


@torch.no_grad()
def vanilla_cpu_generate(
    model: api.model.NeuralNetwork,
    tokenizer: transformers.PreTrainedTokenizerFast,
    input_ids: torch.Tensor,
    attention_mask: Optional[torch.Tensor] = None,
    gconfig: GenerationConfig = dataclasses.field(default_factory=GenerationConfig),
) -> Tuple[torch.Tensor, torch.Tensor]:
    mconfig: FlashMQATConfig = model.config
    assert str(input_ids.device) == 'cpu'

    terminate = False
    generated_idx = 0
    unfinished_sequences = torch.ones(input_ids.shape[0], dtype=torch.long, device=input_ids.device)

    gen_token_ph = []
    gen_logprob_ph = []
    gen_logits_mask_ph = []

    # The main loop.
    while not terminate:
        x = PipeTransferData(attention_mask=attention_mask)
        # one embedding layer, n_layers transformer block, one output layer
        ys = [PipeCacheData(input_ids=input_ids)] + [PipeCacheData() for _ in range(mconfig.n_layers + 1)]
        # Model forward will set k/v cache in PipeCacheData.
        logits = model(x, ys).pp_output[:, -1, :]
        # Next, we will generate the next token after prompts.
        # cache_seqlens is exactly the lengths of prompts.
        next_tokens, logprob, logits_mask, terminate, unfinished_sequences = genstep(
            logits, tokenizer, unfinished_sequences, generated_idx, gconfig)
        gen_token_ph.append(next_tokens)
        gen_logprob_ph.append(logprob)
        gen_logits_mask_ph.append(logits_mask)
        generated_idx += 1

        input_ids = torch.cat([input_ids, next_tokens.unsqueeze(-1)], 1)
        am = torch.logical_and(
            next_tokens.unsqueeze(-1).not_equal(tokenizer.eos_token_id),
            next_tokens.unsqueeze(-1).not_equal(tokenizer.pad_token_id))
        attention_mask = torch.cat([attention_mask, am], 1)

    gen_tokens = torch.stack(gen_token_ph, -1)
    log_probs = torch.stack(gen_logprob_ph, -1)
    if all([m is None for m in gen_logits_mask_ph]):
        logits_mask = None
    else:
        mm = next(m for m in gen_logits_mask_ph if m is not None)
        gen_logits_mask_ph = [torch.zeros_like(mm) if m is None else m for m in gen_logits_mask_ph]
        logits_mask = torch.stack(gen_logits_mask_ph, -2)

    return gen_tokens, log_probs, logits_mask