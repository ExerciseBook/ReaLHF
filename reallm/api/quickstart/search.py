from typing import List, Optional
import dataclasses

from reallm.api.core.dfg import ModelRPC
from reallm.api.quickstart.device_mesh import DeviceMesh
from reallm.api.quickstart.model import ParallelismConfig


@dataclasses.dataclass
class RPCExecution:
    rpc: ModelRPC
    device_mesh: DeviceMesh
    parallel_strategy: ParallelismConfig
    time_cost: Optional[int] = None
    mem: Optional[int] = None
    static_mem: Optional[int] = None

    def __repr__(self):
        return f"RPCExecution({self.rpc}, {self.device_mesh}, {self.parallel_strategy})"

    def __hash__(self):
        return hash((
            self.rpc.name,
            self.device_mesh.cluster_mesh,
            self.device_mesh.device_mesh_name,
            str(self.parallel_strategy),
        ))


@dataclasses.dataclass
class RPCInstance:
    rpc: ModelRPC
    iteration_id: int
    parents: List[ModelRPC]
    children: List[ModelRPC]

    @property
    def name(self):
        return f"{self.rpc.name}:{self.iteration_id}"

    def __repr__(self):
        if len(self.parents) == 0 and len(self.children) == 0:
            return f"RPCInstance({self.rpc.name}, {self.iteration_id})"
        else:
            return (f"RPCInstance({self.rpc.name}, {self.iteration_id}, "
                    f"{self.parents}, {self.children})")

    def __hash__(self):
        return hash((self.rpc.name, self.iteration_id))
