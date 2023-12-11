

from typing import Callable, Tuple
import jax
import jax.numpy as jnp
from flax import struct
from dataclasses import dataclass
from core.envs.env import Env, EnvState
from functools import partial

from core.evaluators.evaluator import Evaluator, EvaluatorConfig, EvaluatorState

@dataclass
class MCTSConfig(EvaluatorConfig):
    max_nodes: int
    puct_coeff: float
    dirichlet_alpha: float
    dirichlet_epsilon: float

@struct.dataclass
class MCTSState(EvaluatorState):
    key: jax.random.PRNGKey
    idx_action_map: jnp.ndarray
    p_vals: jnp.ndarray
    n_vals: jnp.ndarray
    w_vals: jnp.ndarray
    visits: jnp.ndarray
    depth: jnp.ndarray
    max_depth: jnp.ndarray
    cur_node: jnp.ndarray
    next_empty: jnp.ndarray
    reward_indices: jnp.ndarray
    subtrees: jnp.ndarray
    parents: jnp.ndarray

@struct.dataclass
class IterationState:
    state: MCTSState
    env_state: EnvState
    root_state: EnvState


class MCTS(Evaluator):
    def __init__(self,
        env: Env,
        config: MCTSConfig,
        **kwargs
    ):
        super().__init__(env, config, **kwargs)


        self.config: MCTSConfig
        self.policy_shape: Tuple[int, ...] = env.get_action_shape()
        self.flat_policy_size: int = jnp.prod(jnp.array(self.policy_shape))
        self.policy_reshape_fn = None
        self.num_players = env.num_players()
        self.valid_slots = self.config.max_nodes - 1
        self.reward_indices = self.get_reward_indices()

    def get_reward_indices(self) -> jnp.ndarray:
        num_repeats = int(jnp.ceil(self.valid_slots / self.num_players).item())
        return jnp.tile(jnp.array([1] + [0] * (self.num_players - 1), dtype=jnp.int32), num_repeats)[:self.valid_slots]

    def init_visits(self) -> jnp.ndarray:
        visits = jnp.zeros((self.valid_slots, ), dtype=jnp.int32)
        visits = visits.at[0].set(1)
        return visits

    def reset(self, key: jax.random.PRNGKey) -> MCTSState:

        return MCTSState(
            key=key,
            idx_action_map=jnp.zeros((self.config.max_nodes, self.flat_policy_size), dtype=jnp.int32),
            p_vals=jnp.zeros((self.config.max_nodes, self.flat_policy_size), dtype=jnp.float32),
            n_vals=jnp.zeros((self.config.max_nodes,), dtype=jnp.float32),
            w_vals=jnp.zeros((self.config.max_nodes,), dtype=jnp.float32),
            visits=self.init_visits(),
            depth=jnp.ones((1,), dtype=jnp.int32),
            max_depth=jnp.zeros((1,), dtype=jnp.int32),
            cur_node=jnp.ones((1,), dtype=jnp.int32),
            next_empty=jnp.full((1,), 2, dtype=jnp.int32),
            reward_indices=self.reward_indices,
            subtrees=jnp.zeros((self.config.max_nodes,), dtype=jnp.int32),
            parents=jnp.zeros((self.config.max_nodes,), dtype=jnp.int32),
        )
    
    def reset_search(self, state: MCTSState) -> MCTSState:
        return state.replace(
            depth=jnp.ones((1,), dtype=jnp.int32),
            cur_node=jnp.ones((1,), dtype=jnp.int32),
            visits=self.init_visits(),
        )
    
    def choose_with_puct(self, state: MCTSState, legal_actions: jnp.ndarray) -> jnp.ndarray:
        # get visit counts for each child node
        # visits = state.n_vals[state.cur_node]
        visits = jnp.where(
            state.idx_action_map[state.cur_node] > 0,
            state.n_vals[state.idx_action_map[state.cur_node]],
            0
        )

        visits_nozero = jnp.clip(visits, a_min=1)

        w_vals = jnp.where(
            state.idx_action_map[state.cur_node] > 0,
            state.w_vals[state.idx_action_map[state.cur_node]],
            0
        )

        q_vals = w_vals / visits_nozero
        
        p_vals = state.p_vals[state.cur_node]

        puct_score = q_vals + (self.config.puct_coeff * p_vals * jnp.sqrt(1 + visits.sum()) / (1 + visits))
        puct_score = jnp.where(
            legal_actions,
            puct_score,
            -jnp.inf
        )
        return jnp.argmax(puct_score)
    
    def traverse(self, state: MCTSState, action: jnp.ndarray) -> Tuple[MCTSState, jnp.ndarray]:
        master_action_idx = state.idx_action_map[state.cur_node, action].reshape(-1)

        unvisited = jnp.equal(master_action_idx, 0)

        in_bounds = ~jnp.logical_and(
            jnp.greater_equal(state.next_empty, self.config.max_nodes),
            unvisited
        )

        master_action_idx = jnp.where(
            in_bounds & unvisited,
            state.next_empty,
            master_action_idx
        )

        return state.replace(
            next_empty=jnp.where(
                in_bounds & unvisited,
                state.next_empty + 1,
                state.next_empty
            ),
            idx_action_map=state.idx_action_map.at[state.cur_node, action].set(master_action_idx),
            visits=state.visits.at[state.depth, None].set(master_action_idx),
            parents=state.parents.at[master_action_idx].set(state.cur_node),
            cur_node=master_action_idx
        ), unvisited
    

    def iterate(self,
        iter_state: IterationState, 
        carry: any,
        **kwargs
    ) -> Tuple[IterationState, any]:
        state, env_state, root_state = iter_state.state, iter_state.env_state, iter_state.root_state

        legal_actions = env_state.legal_action_mask.flatten()
        
        action = self.choose_with_puct(state, legal_actions)
    
        env_state, terminated = self.env.step(env_state, action)
        
        state, unvisited = self.traverse(state, action)

        state, policy_logits, evaluation = self.evaluate_leaf(state, env_state._observation, **kwargs)
        
        legal_actions = env_state.legal_action_mask.flatten()
        
        policy_logits = jnp.where(
            legal_actions,
            policy_logits,
            -jnp.inf
        )

        policy = jax.nn.softmax(policy_logits)

        value = jnp.where(
            terminated,
            env_state.reward[root_state.cur_player_id],
            evaluation
        )

        is_leaf = jnp.logical_or(
            terminated,
            unvisited
        )

        leaf_inc = jnp.where(
            is_leaf,
            state.visits > 0,
            0
        ).reshape(-1)

        backprop_rewards = leaf_inc * (
            (value * state.reward_indices) + 
            ((-value) * (1-state.reward_indices))
        )

        return (iter_state.replace(
            state = state.replace(
                p_vals = state.p_vals.at[state.cur_node].set(policy),
                n_vals = state.n_vals.at[state.visits].add(leaf_inc),
                w_vals = state.w_vals.at[state.visits].add(backprop_rewards),
                depth = jnp.where(
                    is_leaf,
                    1,
                    state.depth + 1
                ),
                max_depth = jnp.maximum(state.depth, state.max_depth),
                visits = state.visits.at[1:].set(jnp.where(
                    is_leaf,
                    0,
                    state.visits[1:]
                )),
                cur_node = jnp.where(
                    is_leaf,
                    1,
                    state.cur_node
                )
            ), 
            # env_state = root_state when is_leaf else env_state
            env_state = jax.lax.cond(
                is_leaf.squeeze(),
                lambda: root_state,
                lambda: env_state
            )
        ), None)
        
    def evaluate_leaf(self,
        state: MCTSState,
        observation: struct.PyTreeNode,
        **kwargs
    ) -> Tuple[MCTSState, jnp.ndarray, jnp.ndarray]:
        raise NotImplementedError()

    def evaluate(self, 
        state: MCTSState,
        env_state: EnvState,
        num_iters: int,
        **kwargs
    ) -> MCTSState:
        
        state = self.reset_search(state)
    
        # initialize root node p_vals
        state, policy_logits, _ = self.evaluate_leaf(state, env_state._observation, **kwargs)
        legal_actions = env_state.legal_action_mask.flatten()
        policy_logits = jnp.where(
            legal_actions,
            policy_logits,
            -jnp.inf
        )
        policy = jax.nn.softmax(policy_logits)

        dir_key, new_key = jax.random.split(state.key)
        dir_noise = jax.random.dirichlet(
            dir_key,
            alpha=jnp.full(
                [self.env.num_actions], 
                fill_value=self.config.dirichlet_alpha
            )
        )

        noisy_policy = (
            ((1-self.config.dirichlet_epsilon) * policy) +
            (self.config.dirichlet_epsilon * dir_noise)
        )

        state = state.replace(
            key = new_key,
            p_vals = state.p_vals.at[1].set(noisy_policy)
        )

        iteration_state = IterationState(
            state = state,
            env_state = env_state,
            root_state = env_state
        )

        iterate_fn = partial(self.iterate, **kwargs)

        iteration_state, _ = jax.lax.scan(
            f=iterate_fn,
            init=iteration_state,
            xs=jnp.arange(num_iters)
        )

        return iteration_state.state.replace(
            cur_node = jnp.ones_like(state.cur_node),
        )
    
    def propagate_root_subtrees(self, state: MCTSState) -> MCTSState:
        subtrees = jnp.arange(self.config.max_nodes)
        parents = state.parents.at[0].set(0)

        def propagate_subtrees(subtrees, _):
            parent_subtrees = subtrees[parents]
            return jnp.where(
                jnp.greater(parent_subtrees, 1),
                parent_subtrees,
                subtrees
            ), None
        
        subtrees, _ = jax.lax.scan(
            propagate_subtrees, 
            subtrees,
            jnp.arange(self.config.max_nodes),
        )

        return state.replace(
            parents = parents,
            subtrees = subtrees
        )
    
    def load_subtree(self, state: MCTSState, action: jnp.ndarray) -> MCTSState:
        state = self.propagate_root_subtrees(state)
        slots_aranged = jnp.arange(self.config.max_nodes)
        subtree_master_idx = state.idx_action_map[1, action]
        populated = subtree_master_idx > 1
        new_nodes = state.subtrees == subtree_master_idx

        translation = jnp.where(
            populated,
            new_nodes * new_nodes.cumsum(),
            0
        )

        old_subtree_idxs = new_nodes * slots_aranged

        next_empty = jnp.amax(translation, keepdims=True) + 1
        erase = jnp.where(
            slots_aranged < next_empty,
            0,
            slots_aranged
        )

        next_empty = jnp.clip(next_empty, a_min=2)

        new_w_vals = state.w_vals.at[translation].set(
            state.w_vals[old_subtree_idxs]
        ).at[erase].set(0)
        new_n_vals = state.n_vals.at[translation].set(
            state.n_vals[old_subtree_idxs]
        ).at[erase].set(0)
        new_p_vals = state.p_vals.at[translation].set(
            state.p_vals[old_subtree_idxs]
        ).at[erase].set(0)

        new_idx_action_map = state.idx_action_map.at[translation].set(
            translation[state.idx_action_map]
        ).at[erase].set(0)

        new_parents = state.parents.at[translation].set(
            translation[state.parents]
        ).at[erase].set(0)  

        return state.replace(
            w_vals = new_w_vals,
            n_vals = new_n_vals,
            p_vals = new_p_vals,
            idx_action_map = new_idx_action_map,
            parents = new_parents,
            next_empty = next_empty,
            max_depth = jnp.clip(state.max_depth - 1, a_min=1)
        )
    
    def step_evaluator(self, 
        evaluator_state: EvaluatorState, 
        actions: jnp.ndarray, 
        terminated: jnp.ndarray
    ) -> EvaluatorState:
        evaluator_state = self.load_subtree(evaluator_state, actions)
        return self.reset_if_terminated(evaluator_state, terminated)

    def get_policy(self, evaluator_state: EvaluatorState) -> jnp.ndarray:
        action_vists = evaluator_state.n_vals[1]
        return action_vists / action_vists.sum()
