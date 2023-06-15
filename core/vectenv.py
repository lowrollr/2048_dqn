from typing import Optional, Tuple
import torch

from core import GLOB_FLOAT_TYPE
from core.torchscripts import fast_weighted_sample

class VectEnv:
    def __init__(self, 
            num_parallel_envs: int, 
            state_shape: torch.Size, 
            policy_shape: torch.Size, 
            value_shape: torch.Size, 
            device: torch.device, 
            is_stochastic: bool
    ):
        
        self.state_shape = state_shape
        self.policy_shape = policy_shape
        self.value_shape = value_shape

        self.states = torch.zeros((num_parallel_envs, *state_shape), dtype=GLOB_FLOAT_TYPE, device=device, requires_grad=False)
        self.invalid_mask = torch.zeros(num_parallel_envs, dtype=torch.bool, device=device, requires_grad=False)
        
        self.device = device
        self.is_stochastic = is_stochastic
        self.num_parallel_envs = num_parallel_envs


        # Tensors we re-use for indexing and sampling
        self.randn = torch.zeros((num_parallel_envs,1), dtype=GLOB_FLOAT_TYPE, device=device, requires_grad=False)
       
        self.env_indices = torch.arange(num_parallel_envs, device=device, requires_grad=False)
        self.fws = torch.jit.trace(fast_weighted_sample, (torch.rand((num_parallel_envs, 4), device=device, requires_grad=False, dtype=GLOB_FLOAT_TYPE), self.randn), check_trace=False)
    
    def reset(self, seed=None):
        raise NotImplementedError()
    
    def step(self, actions):
        self.push_actions(actions)
        if self.is_stochastic:
            # make step on legal states
            self.stochastic_step(torch.logical_not(self.invalid_mask))
        self.update_invalid_mask()
        return self.invalid_mask
    
    def update_invalid_mask(self):
        self.invalid_mask = self.is_terminal()

    def is_terminal(self):
        raise NotImplementedError()
    
    def push_actions(self, actions):
        raise NotImplementedError()
    
    def get_legal_actions(self):
        return torch.ones(self.num_parallel_envs, *self.policy_shape, dtype=torch.bool, device=self.device, requires_grad=False)

    def stochastic_step(self, mask=None) -> None:
        progs, probs = self.get_stochastic_progressions()
        indices = self.fast_weighted_sample(probs)
                
        if mask is not None:
            self.states = torch.where(mask.view(self.num_parallel_envs, 1, 1, 1), progs[(self.env_indices, indices)].unsqueeze(1), self.states)
        else:
            self.states = progs[(self.env_indices, indices)].unsqueeze(1)


    def get_stochastic_progressions(self) -> Tuple[torch.Tensor, torch.Tensor]:
        raise NotImplementedError()
    
    def reset_invalid_states(self):
        raise NotImplementedError()

    def fast_weighted_sample(self, weights): 
        return self.fws(weights, self.randn)
    