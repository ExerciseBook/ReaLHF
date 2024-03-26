from collections import defaultdict
from typing import Any, Callable, Dict, Optional, Union
import abc
import dataclasses

import deepspeed
import torch
import torch.utils.data
import transformers

from base.namedarray import NamedArray
import api.config.config_system
import api.config.dfg
from api.config.config_base import ModelName
import base.logging as logging

logger = logging.getLogger("model")

NeuralNetwork = Union[deepspeed.DeepSpeedEngine, transformers.PreTrainedModel, torch.nn.Module]


@dataclasses.dataclass
class ModelVersion:
    epoch: int = 0
    epoch_step: int = 0
    global_step: int = 0


@dataclasses.dataclass
class FinetuneSpec:
    total_train_epochs: int
    total_train_steps: int
    steps_per_epoch: int
    batch_size_per_device: int
    max_seqlen: int


@dataclasses.dataclass
class Model:
    name: ModelName
    module: NeuralNetwork
    tokenizer: transformers.PreTrainedTokenizerFast
    device: Union[str, torch.device]
    dtype: Optional[torch.dtype] = None
    version: ModelVersion = dataclasses.field(default_factory=ModelVersion)
    ft_spec: FinetuneSpec = None  # will be initialized by the backend

    def __post_init__(self):
        try:
            self.module = self.module.to(self.device)
        except ValueError as e:
            # 4-bit and 8-bit model may fail here
            logger.warning(f"Failed to move model to device {self.device} because {e}. Abort to device.")

    def inc_version(self):
        self.version.global_step += 1
        self.version.epoch_step += 1
        if self.version.epoch_step >= self.ft_spec.steps_per_epoch:
            self.version.epoch_step = 0
            self.version.epoch += 1


class ModelBackend(abc.ABC):

    @abc.abstractmethod
    def _initialize(self, model: Model, spec: FinetuneSpec) -> Model:
        raise NotImplementedError()

    def initialize(self, model: Model, spec: FinetuneSpec) -> Model:
        model.ft_spec = spec
        return self._initialize(model, spec)

class NullBackend(ModelBackend):
    
    def _initialize(self, model: Model, spec: FinetuneSpec) -> Model:
        return model

class ModelInterface(abc.ABC):

    def __post_init__(self):
        self._is_future_interface = False
        self._hooks = {}

    @property
    def is_future_interface(self):
        return self._is_future_interface

    def save(self, model: Model, save_dir: str):
        pass

    def evaluate(self, model: Model, eval_dataloader: torch.utils.data.DataLoader) -> Dict:
        return {}

    def inference(self, model: Model, data: NamedArray) -> NamedArray:
        raise NotImplementedError()

    def generate(self, model: Model, data: NamedArray) -> NamedArray:
        raise NotImplementedError()

    def train_step(self, model: Model, data: NamedArray) -> Dict:
        raise NotImplementedError()

    def register_post_hook(self, func_name: str, post_hook: Callable):
        """ Register hook for API in the interface, only used in interfaces for StreamPipeEngine.
        Hooks should be registered in the __post_init__ function of interface class.
        Only one hook can be registered for each API, and for one API type, only one instance will be executing at a time.
        Hooks should take model, data and future as input, and return a dict or namedarray as collected result.
        
        Args:
            func_name (str): name of the function to be hooked, should be one of [
                "evaluate", "inference", "generate", "train_step"
            ]
            post_hook (Callable): the function to be called after the original function.
        """
        assert self.is_future_interface
        self._hooks[func_name] = post_hook

    def execute_post_hook(self, func_name, model, data, future):
        """ Execute resgitered hooks, only used in interfaces for StreamPipeEngine.
        Execute in model worker after the futures of APIs are completed.
        """
        assert self.is_future_interface
        return self._hooks[func_name](model, data, future)


ALL_MODEL_CLASSES = {}
ALL_INTERFACE_CLASSES = {}
ALL_BACKEND_CLASSES = {}
ALL_WRAPPER_CLASSES = {}


def register_model(name, model_cls):
    assert name not in ALL_MODEL_CLASSES
    ALL_MODEL_CLASSES[name] = model_cls


def register_interface(name, cls_):
    assert name not in ALL_INTERFACE_CLASSES
    assert issubclass(cls_, ModelInterface)
    ALL_INTERFACE_CLASSES[name] = cls_


def register_backend(name, cls_):
    assert name not in ALL_BACKEND_CLASSES
    assert issubclass(cls_, ModelBackend)
    ALL_BACKEND_CLASSES[name] = cls_


def register_wrapper(name, cls_):
    assert name not in ALL_WRAPPER_CLASSES
    ALL_WRAPPER_CLASSES[name] = cls_


def make_model_wrapper(cfg: api.config.config_system.ModelWrapper) -> Callable[[Model], Model]:
    cls_ = ALL_WRAPPER_CLASSES[cfg.type_]
    return cls_(**cfg.args)


def make_model(cfg: api.config.config_system.Model, name: ModelName, device: Union[str, torch.device]) -> Model:
    logger.info(f"making model {cfg.type_} on {device}")
    model_cls = ALL_MODEL_CLASSES[cfg.type_]
    model = model_cls(**cfg.args, name=name, device=device)
    assert isinstance(model, Model)
    for w in cfg.wrappers:
        model = make_model_wrapper(w)(model)
        assert isinstance(model, Model)
    return model


def make_interface(cfg: api.config.dfg.ModelInterface) -> ModelInterface:
    cls_ = ALL_INTERFACE_CLASSES[cfg.type_]
    return cls_(**cfg.args)


def make_backend(cfg: api.config.config_system.ModelBackend) -> ModelBackend:
    cls_ = ALL_BACKEND_CLASSES[cfg.type_]
    return cls_(**cfg.args)

register_backend("null", NullBackend)