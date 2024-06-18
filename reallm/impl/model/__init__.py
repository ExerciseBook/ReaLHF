import functools
import os
import re

import torch

from reallm.api.core.model_api import HF_MODEL_FAMILY_REGISTRY
from reallm.base.importing import import_module
from reallm.impl.model.conversion.hf_registry import HFModelRegistry
from reallm.impl.model.nn.real_llm_api import ReaLModel

# Import all HuggingFace model implementations.
import reallm.api.from_hf

# Import all model implementations.
_p = re.compile(r"^(?!.*__init__).*\.py$")
_filepath = os.path.dirname(__file__)
import_module(os.path.join(_filepath, "backend"), _p)
import_module(os.path.join(_filepath, "interface"), _p)
import_module(os.path.join(_filepath, "nn"), _p)

# Set PyTorch JIT options, following Megatron-LM.
if torch.cuda.is_available():
    torch._C._jit_set_profiling_executor(True)
    torch._C._jit_set_profiling_mode(True)
    torch._C._jit_override_can_fuse_on_cpu(False)
    torch._C._jit_override_can_fuse_on_gpu(False)
    torch._C._jit_set_texpr_fuser_enabled(False)
    torch._C._jit_set_nvfuser_enabled(True)
    torch._C._debug_set_autodiff_subgraph_inlining(False)

# Add HuggingFace hooks to ReaLModel.
_HF_REGISTRIES = {}


def _load_from_hf(
    model: ReaLModel, registry_name, load_dir: str, init_critic_from_actor: bool
):
    r = _HF_REGISTRIES[registry_name]
    setattr(
        model,
        "save_to_hf",
        functools.partial(_save_to_hf, model, registry_name),
    )
    return r.load(model, load_dir, init_critic_from_actor)


def _save_to_hf(model: ReaLModel, registry_name, tokenizer, save_dir: str):
    r = _HF_REGISTRIES[registry_name]
    r.save(model, tokenizer, save_dir)


def _config_from_hf(
    registry_name, hf_config=None, model_path=None, is_critic=False
):
    r = _HF_REGISTRIES[registry_name]
    return r.config_from_hf(hf_config, model_path, is_critic)


def _config_to_hf(registry_name, config):
    r = _HF_REGISTRIES[registry_name]
    return r.config_to_hf(config)


for name, helpers in HF_MODEL_FAMILY_REGISTRY.items():
    _HF_REGISTRIES[name] = r = HFModelRegistry(**helpers)

    _load_from_hf_ = functools.partialmethod(_load_from_hf, name)
    setattr(ReaLModel, f"from_{name}", _load_from_hf_)

    _save_to_hf_ = functools.partialmethod(_save_to_hf, name)
    setattr(ReaLModel, f"to_{name}", _save_to_hf_)

    _config_from_hf_ = functools.partial(_config_from_hf, name)
    setattr(ReaLModel, f"config_from_{name}", staticmethod(_config_from_hf_))

    _config_to_hf_ = functools.partial(_config_to_hf, name)
    setattr(ReaLModel, f"config_to_{name}", staticmethod(_config_to_hf_))
