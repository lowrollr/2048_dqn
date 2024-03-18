
from functools import partial
from chex import dataclass
import chex
import jax
from core.evaluators.evaluator import Evaluator

from core.types import EnvInitFn, EnvStepFn

@dataclass(frozen=True)
class TestState:
    key: jax.random.PRNGKey

class BaseTester:
    def __init__(self, epochs_per_test: int = 1):
        self.epochs_per_test = epochs_per_test

    def init(self, key: jax.random.PRNGKey, **kwargs) -> TestState:
        return TestState(key=key)

    def run(self, epoch_num: int, *args) -> TestState:
        if epoch_num % self.epochs_per_test == 0:
            return self.test(*args)
    
    @partial(jax.pmap, static_broadcasted_argnums=(0, 1, 2, 3))
    def test(self, 
        env_step_fn: EnvStepFn, 
        env_init_fn: EnvInitFn,
        evaluator: Evaluator,
        state: TestState, 
        params: chex.ArrayTree
    ) -> TestState:
        raise NotImplementedError()
