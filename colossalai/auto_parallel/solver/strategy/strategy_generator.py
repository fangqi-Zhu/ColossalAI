import operator
import torch
from colossalai.tensor.sharding_spec import ShardingSpec
from functools import reduce
from abc import ABC, abstractmethod
from colossalai.tensor.shape_consistency import CollectiveCommPattern, CommSpec
from colossalai.tensor.sharding_spec import ShardingSpec
from colossalai.device.device_mesh import DeviceMesh
from typing import Dict, List, Union, Any
from ..sharding_strategy import OperationData, ShardingStrategy_V2, TrainCycleItem


class StrategyGenerator_V2(ABC):
    """
    StrategyGenerator is used to generate the same group of sharding strategies. 

    TODO: remove the original strategy_generator.py after refactoring
    """

    def __init__(self, operation_data_mapping: Dict[str, OperationData], device_mesh: DeviceMesh):
        self.op_data = operation_data_mapping
        self.device_mesh = device_mesh

    def get_sharding_strategy(self, name: str, sharding_spec_mapping: Dict[str, ShardingSpec],
                              communication_action_mapping: Dict[str, CommSpec]):
        """
        A factory method to produce a ShardingStrategy object.

        Args:
            sharding_spec_mapping (Dict[str, ShardingSpec]): the mapping between the operation data name and the ShardingSpec object.
            communication_action_mapping (Dict[str, CommSpec]): the mapping between the operation data name and the CommSpec object.
        """
        sharding_specs = self.replace_op_name_with_op_data(sharding_spec_mapping)
        communication_actions = self.replace_op_name_with_op_data(communication_action_mapping)
        return ShardingStrategy_V2(name=name,
                                   sharding_specs=sharding_specs,
                                   communication_actions=communication_actions)

    def to_sharding_spec_mapping(self, mapping: Dict[str, Dict[int, List[int]]]):
        """
        A utility method to convert the the dim partition dict to a ShardingSpec object.

        Args:
            mapping (Dict[str, Dict[int, List[int]]]): the key of the mapping is the operation data name and the value is a dim partition dictionary.
        """
        results = {}
        for op_data_name, dim_partition_dict in mapping.items():
            op_data = self.op_data[op_data_name]
            sharding_spec = ShardingSpec(device_mesh=self.device_mesh,
                                         entire_shape=op_data.logical_shape,
                                         dim_partition_dict=dim_partition_dict)
            results[op_data_name] = sharding_spec
        return results

    def replace_op_name_with_op_data(self, mapping: Dict[str, Any]):
        """
        Convert the key of the dictionary from the operation data name to an OperationData object.
        """
        results = {}
        for k, v in mapping.items():
            op_data = self.op_data[k]
            results[op_data] = v
        return results

    def get_communication_spec(self, sharding_spec: ShardingSpec, communication_pattern: CollectiveCommPattern,
                               logical_process_axis: Union[int, List[int]]):
        """
        A factory method to produce a CommSpec object.
        """
        # use flatten device mesh the same action is applied to two axes
        if isinstance(logical_process_axis, list) and len(logical_process_axis) == 2:
            sharding_spec.device_mesh = sharding_spec.device_mesh.flatten()
            logical_process_axis = 0
        return CommSpec(comm_pattern=communication_pattern,
                        sharding_spec=sharding_spec,
                        logical_process_axis=logical_process_axis)

    def update_communication_cost(self, strategy: ShardingStrategy_V2) -> ShardingStrategy_V2:
        """
        Compute the communication cost involved in the forward and backward iteration.
        """

        comm_cost = TrainCycleItem(fwd=0, bwd=0)

        def _compute_and_add(data: OperationData, comm_spec: CommSpec):
            num_ele_in_comm = comm_spec.get_comm_cost()
            dtype = operand.data.dtype
            size_per_elem_bytes = torch.tensor([], dtype=dtype).element_size()
            cost = size_per_elem_bytes * num_ele_in_comm

            # compute the fwd
            # TODO: comm_spec.get_comm_cost should return a TrainCycleItem instead of the total cost.
            # it works fine here because only REDUCE_FWD_IDENTITY_BWD and IDENTITY_FWD_ALLREDUCE_BWD are used,
            # so total cost is either for fwd or bwd.
            if comm_spec.comm_pattern == CollectiveCommPattern.REDUCE_FWD_IDENTITY_BWD:
                comm_cost.fwd += cost
            elif comm_spec.comm_pattern == CollectiveCommPattern.IDENTITY_FWD_ALLREDUCE_BWD:
                comm_cost.fwd += cost
            else:
                raise ValueError(f"Found unknown CommunicationType {comm_spec.comm_pattern}")

        # check if communication action exists
        # if so, loop over each action and compute the cost of each action
        if strategy.communication_actions is not None:
            for operand, comm_spec in strategy.communication_actions:
                _compute_and_add(operand, comm_spec)

        # update the communication cost attribute in-place
        strategy.communication_cost = comm_cost
        return strategy

    @abstractmethod
    def update_compute_cost(self, strategy: ShardingStrategy_V2) -> ShardingStrategy_V2:
        """
        Customize this method to compute the computation flops.
        """
        pass

    @abstractmethod
    def update_memory_cost(self, strategy: ShardingStrategy_V2) -> ShardingStrategy_V2:
        """
        Customize this method to compute the memory cost in bytes.
        """
        pass

    def _compute_size_in_bytes(self, strategy: ShardingStrategy_V2, key: str):
        """
        Compute the size of a tensor in bytes.
        
        Args:
            strategy (ShardingStrategy): the ShardingStrategy generated.
            key (str): the name of the operation data defined by the generator.

        """
        op_data = self.op_data[key]
        sharded_shape = strategy.sharding_specs[op_data].get_sharded_shape_per_device()
        dtype = self.op_data[key].data.dtype
        size_per_elem_bytes = torch.tensor([], dtype=dtype).element_size()
        return reduce(operator.mul, sharded_shape) * size_per_elem_bytes

    @abstractmethod
    def generate(self) -> List[ShardingStrategy_V2]:
        """
        Generate all possible sharding strategies for this operation.
        """
        pass

    @abstractmethod
    def validate(self, *args, **kwargs) -> bool:
        """
        Validate if the operands are of desired shape. 
        If True, means this generator can be used for the current operation.
        """
        pass
