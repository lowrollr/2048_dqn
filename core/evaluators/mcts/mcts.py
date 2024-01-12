
from typing import Any, Callable, Optional, Tuple
import jax
import chex
from chex import dataclass
import jax.numpy as jnp
from core.evaluators.mcts.action_selection import MCTSActionSelector
from core.evaluators.mcts.data import BackpropState, MCTSNode, MCTSTree, TraversalState, MCTSOutput
from core.trees.tree import Tree, add_node, get_child_data, get_rng, set_root, update_node

class MCTS:
    def __init__(self,
        step_fn: Callable[[chex.ArrayTree, Any], Tuple[chex.ArrayTree, float, bool]],
        eval_fn: Callable[[chex.ArrayTree], Tuple[chex.ArrayTree, float]],
        action_selection_fn: MCTSActionSelector,
        action_mask_fn: Callable[[chex.ArrayTree], chex.Array] = lambda _: jnp.array([True]),
        discount: float = -1.0,
        temperature: float = 1.0
    ):
        self.step_fn = step_fn
        self.eval_fn = eval_fn
        self.action_mask_fn = action_mask_fn
        self.action_selection_fn = action_selection_fn
        self.discount = discount
        self.temperature = temperature

    def search(self, tree: MCTSTree, root_embedding: chex.ArrayTree, num_iterations: int) -> MCTSOutput:   
        tree = self.update_root(tree, root_embedding)
        tree = jax.lax.fori_loop(0, num_iterations, lambda _, t: self.iterate(t), tree)
        tree, action, action_weights = self.sample_root_action(tree)
        root_node = tree.at(tree.ROOT_INDEX)
        return MCTSOutput(
            tree=tree,
            sampled_action=action,
            root_value=root_node.w / root_node.n,
            action_weights=action_weights
        )

    
    def update_root(self, tree: MCTSTree, root_embedding: chex.ArrayTree) -> MCTSTree:
        root_policy_logits, root_value = self.evaluate_root(root_embedding)
        root_policy = jax.nn.softmax(root_policy_logits)
        root_node = tree.at(tree.ROOT_INDEX)
        visited = root_node.n > 0
        root_node = root_node.replace(
            p=root_policy,
            w=jnp.where(visited, root_node.w, root_value),
            n=jnp.where(visited, root_node.n, 1),
            embedding=root_embedding
        )
        return set_root(tree, root_node)
    
    def evaluate_root(self, root_embedding: chex.ArrayTree) -> Tuple[chex.ArrayTree, float]:
        return self.eval_fn(root_embedding)
    
    def iterate(self, tree: MCTSTree) -> MCTSTree:
        # traverse from root -> leaf
        traversal_state = self.traverse(tree)
        parent, action = traversal_state.parent, traversal_state.action
        # evaluate and expand leaf
        embedding = tree.at(parent).embedding
        new_embedding, reward, terminated = self.step(embedding, action)
        policy_logits, value = self.eval_fn(new_embedding)
        policy_mask = self.action_mask_fn(new_embedding)
        policy_logits = jnp.where(policy_mask, policy_logits, -jnp.inf)
        policy = jax.nn.softmax(policy_logits)
        value = jnp.where(terminated, reward, value)
        node_exists = tree.is_edge(parent, action)
        node_id = tree.edge_map[parent, action]
        node = tree.at(node_id)
        tree = jax.lax.cond(
            node_exists,
            lambda _: update_node(tree, node_id,
                node.replace(
                    n = node.n + 1,
                    w = node.w + value,
                    p = policy,
                    terminal = terminated,
                    embedding = new_embedding        
                )),
            lambda _: add_node(tree, parent, action, 
                MCTSNode(n=1, p=policy, w=value, terminal=terminated, embedding=new_embedding)),
            None
        )
        # backpropagate
        tree = self.backpropagate(tree, parent, value)
        # jax.debug.print("{x}", x=tree)
        return tree
    
    def choose_root_action(self, tree: MCTSTree) -> int:
        return self.action_selection_fn(tree, tree.ROOT_INDEX, self.discount)
    
    def step(self, embedding: chex.ArrayTree, action: int) -> Tuple[chex.ArrayTree, float, bool]:
        return self.step_fn(embedding, action)

    def traverse(self, tree: MCTSTree) -> TraversalState:
        def cond_fn(state: TraversalState) -> bool:
            return jnp.logical_and(
                tree.is_edge(state.parent, state.action),
                ~(tree.at(tree.edge_map[state.parent, state.action]).terminal)
                # TODO: maximum depth
            )
        
        def body_fn(state: TraversalState) -> TraversalState:
            node_idx = tree.edge_map[state.parent, state.action]
            action = self.action_selection_fn(tree, node_idx, self.discount)
            return TraversalState(parent=node_idx, action=action)
        
        root_action = self.choose_root_action(tree)
        return jax.lax.while_loop(
            cond_fn, body_fn, 
            TraversalState(parent=tree.ROOT_INDEX, action=root_action)
        )
    
    def backpropagate(self, tree: MCTSTree, parent: int, value: float) -> MCTSTree:
        def body_fn(state: BackpropState) -> Tuple[int, MCTSTree]:
            node_idx, value, tree = state.node_idx, state.value, state.tree
            value *= self.discount
            node = tree.at(node_idx)
            new_node = node.replace(
                n=node.n + 1,
                w=node.w + value,
            )
            tree = update_node(tree, node_idx, new_node)
            return BackpropState(node_idx=tree.parents[node_idx], value=value, tree=tree)
        
        state = jax.lax.while_loop(
            lambda s: s.node_idx != s.tree.NULL_INDEX, body_fn, 
            BackpropState(node_idx=parent, value=value, tree=tree)
        )
        return state.tree

    def sample_root_action(self, tree: MCTSTree) -> Tuple[MCTSTree, int, chex.Array]:
        action_visits = get_child_data(tree, tree.data.n, tree.ROOT_INDEX)
        action_weights = action_visits / action_visits.sum()
        rand_key, tree = get_rng(tree)
        if self.temperature == 0:
            return tree, jnp.argmax(action_weights), action_weights
        
        action_weights = action_weights ** (1/self.temperature)
        action_weights /= action_weights.sum()
        action = jax.random.choice(rand_key, action_weights.shape[-1], p=action_weights)
        return tree, action, action_weights