
from copy import deepcopy
from pathlib import Path
from typing import List, Optional
import torch
import logging
from core.evaluation.evaluator import Evaluator
from core.evaluation.random import RandomBaseline
from core.utils.history import Metric, TrainingMetrics
from core.training.training_hypers import TurboZeroHypers
from core.resnet import TurboZeroResnet
from core.utils.memory import GameReplayMemory
from core.training.trainer import Trainer
from envs.othello.collector import OthelloCollector
from envs.othello.evaluator import OTHELLO_EVALUATORS
from core.resnet import reset_model_weights


class OthelloTrainer(Trainer):
    def __init__(self,
        evaluator_train: OTHELLO_EVALUATORS,
        evaluator_test: OTHELLO_EVALUATORS,
        num_parallel_envs: int,
        device: torch.device,
        episode_memory_device: torch.device,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        hypers: TurboZeroHypers,
        baselines: List[str] = [],
        history: Optional[TrainingMetrics] = None,
        log_results: bool = True,
        interactive: bool = True,
        run_tag: str = 'othello'
    ):
        train_collector = OthelloCollector(
            evaluator_train,
            episode_memory_device,
            hypers.temperature_train
        )
        test_collector = OthelloCollector(
            evaluator_test,
            episode_memory_device,
            hypers.temperature_test
        )
        super().__init__(
            train_collector = train_collector,
            test_collector = test_collector,
            num_parallel_envs = num_parallel_envs,
            model = model,
            optimizer = optimizer,
            hypers = hypers,
            device = device,
            history = history,
            log_results = log_results,
            interactive = interactive,
            run_tag = run_tag
        )
        self.baselines = baselines
        self.best_model = deepcopy(model)
        self.random_baseline = deepcopy(model)
        self.random_baseline.apply(reset_model_weights)
        self.best_model_optimizer_state_dict = deepcopy(optimizer.state_dict())

    def save_checkpoint(self, custom_name: Optional[str] = None) -> None:
        directory = f'./checkpoints/{self.run_tag}/'
        Path(directory).mkdir(parents=True, exist_ok=True)
        filename = custom_name if custom_name is not None else str(self.history.cur_epoch)
        filepath = directory + f'{filename}.pt'
        torch.save({
            'model_arch_params': self.model.arch_params,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'best_model_state_dict': self.best_model.state_dict(),
            'best_model_optimizer_state_dict': self.best_model_optimizer_state_dict,
            'hypers': self.hypers,
            'history': self.history,
            'run_tag': self.run_tag,
            'train_collector': self.train_collector.get_details(),
            'test_collector': self.test_collector.get_details(),
            'baselines': self.baselines,
        }, filepath)

    def init_history(self):
        return TrainingMetrics(
            train_metrics=[
                Metric(name='loss', xlabel='Step', ylabel='Loss', addons={'running_mean': 100}, maximize=False, alert_on_best=self.log_results),
                Metric(name='value_loss', xlabel='Step', ylabel='Loss', addons={'running_mean': 100}, maximize=False, alert_on_best=self.log_results, proper_name='Value Loss'),
                Metric(name='policy_loss', xlabel='Step', ylabel='Loss', addons={'running_mean': 100}, maximize=False, alert_on_best=self.log_results, proper_name='Policy Loss'),
                Metric(name='policy_accuracy', xlabel='Step', ylabel='Accuracy (%)', addons={'running_mean': 100}, maximize=True, alert_on_best=self.log_results, proper_name='Policy Accuracy'),
            ],
            episode_metrics=[],
            eval_metrics=[],
            epoch_metrics=[
                Metric(name='win_margin_vs_best', xlabel='Epoch', ylabel='Margin (+/- games)', maximize=True, alert_on_best=False, proper_name='Win Margin (Current Model vs. Best Model)'),
                Metric(name='win_margin_vs_random', xlabel='Epoch', ylabel='Margin (+/- games)', maximize=True, alert_on_best=False, proper_name='Win Margin (Current Model vs. Random Model)'),
            ]
        )
    
    def add_collection_metrics(self, episodes):
        for _ in episodes:
            self.history.add_episode_data({}, log=self.log_results)
    def add_evaluation_metrics(self, episodes):
        for _ in episodes:
            self.history.add_evaluation_data({}, log=self.log_results)
    def add_epoch_metrics(self):
        pass

    def evaluate_against_best(self, num_episodes):
        self.test_collector.reset()
        split = num_episodes // 2
        completed_episodes = torch.zeros(num_episodes, dtype=torch.bool, device=self.device, requires_grad=False)
        scores = torch.zeros(num_episodes, dtype=torch.float32, device=self.device, requires_grad=False)
        self.test_collector.collect_step(self.model)
        # hacky way to split the episodes into two sets (this environment cannot terminate on the first step)
        self.test_collector.evaluator.env.terminated[:split] = True
        self.test_collector.evaluator.env.reset_terminated_states()
        
        new_model_is_p1 = torch.ones(num_episodes, dtype=torch.bool, device=self.device, requires_grad=False)
        new_model_is_p1[:split] = False

        use_other_model = True
        while not completed_episodes.all():
            model = self.best_model if use_other_model else self.model
            # we don't need to collect the episodes into episode memory/replay buffer, so we can call collect_step directly
            terminated = self.test_collector.collect_step(model)
            rewards = self.test_collector.evaluator.env.get_rewards()
            if use_other_model:
                scores += rewards * terminated * ~completed_episodes
            else:
                scores += (1 - rewards) * terminated * ~completed_episodes
            completed_episodes |= terminated
            use_other_model = not use_other_model

        wins = (scores == 1).sum().cpu().clone()
        draws = (scores == 0.5).sum().cpu().clone()
        losses = (scores == 0).sum().cpu().clone()

        return wins, draws, losses
    
    def evaluate_against_baseline(self, num_episodes, baseline: Evaluator):
        self.test_collector.reset()
        split = num_episodes // 2
        completed_episodes = torch.zeros(num_episodes, dtype=torch.bool, device=self.device, requires_grad=False)
        scores = torch.zeros(num_episodes, dtype=torch.float32, device=self.device, requires_grad=False)
        self.test_collector.collect_step(self.model)
        # hacky way to split the episodes into two sets (this environment cannot terminate on the first step)
        self.test_collector.evaluator.env.terminated[:split] = True
        self.test_collector.evaluator.env.reset_terminated_states()
        
        new_model_is_p1 = torch.ones(num_episodes, dtype=torch.bool, device=self.device, requires_grad=False)
        new_model_is_p1[:split] = False

        use_other_evaluator = True
        while not completed_episodes.all():
            if use_other_evaluator:
                actions = baseline.evaluate()
                terminated = baseline.env.step(actions)
            else:
                terminated = self.test_collector.collect_step(self.model)
            rewards = self.test_collector.evaluator.env.get_rewards()
            if use_other_evaluator:
                scores += rewards * terminated * ~completed_episodes
            else:
                scores += (1 - rewards) * terminated * ~completed_episodes
            completed_episodes |= terminated
            use_other_evaluator = not use_other_evaluator

        wins = (scores == 1).sum().cpu().clone()
        draws = (scores == 0.5).sum().cpu().clone()
        losses = (scores == 0).sum().cpu().clone()

        return wins, draws, losses


    def test_n_episodes(self, num_episodes, test_against_best = True):
        if test_against_best:
            wins, draws, losses = self.evaluate_against_best(num_episodes)
            win_margin_vs_best = wins - losses
            new_best = win_margin_vs_best >= self.hypers.test_improvement_threshold
            logging.info(f'Epoch {self.history.cur_epoch} Current vs. Best:')
            if new_best:
                self.best_model.load_state_dict(self.model.state_dict())
                self.best_model_optimizer_state_dict = deepcopy(self.optimizer.state_dict())
                logging.info('************ NEW BEST MODEL ************')
            logging.info(f'W/L/D: {wins}/{losses}/{draws}')
            self.history.add_epoch_data({
                'win_margin_vs_best': win_margin_vs_best,
            }, log=self.log_results)
        



        for baseline in self.baselines:
            if baseline == 'random':
                random_evaluator = RandomBaseline(self.test_collector.evaluator.env, self.device, None)
        
                wins, draws, losses = self.evaluate_against_baseline(num_episodes, random_evaluator)
                win_margin_vs_random = wins - losses
                logging.info(f'Epoch {self.history.cur_epoch} Current vs. Random:')
                logging.info(f'W/L/D: {wins}/{losses}/{draws}')

                if test_against_best:
                    self.history.add_epoch_data({
                        'win_margin_vs_random': win_margin_vs_random,
                    }, log=self.log_results)
            else:
                raise NotImplementedError('unrecognized baseline', baseline)

    def training_loop(self, epochs: Optional[int] = None):
        self.test_n_episodes(self.hypers.test_episodes_per_epoch, test_against_best=False)
        while self.history.cur_epoch < epochs if epochs is not None else True:
            while self.history.cur_train_step < self.hypers.train_episodes_per_epoch * (self.history.cur_epoch+1):
                self.selfplay_step()
            self.history.start_new_epoch()
            self.test_n_episodes(self.hypers.test_episodes_per_epoch)
            self.add_epoch_metrics()
            if self.interactive:
                self.history.generate_plots()
            self.save_checkpoint()



def load_checkpoint(
    num_parallel_envs: int,
    checkpoint_path: str,
    device: torch.device,
    episode_memory_device: torch.device = torch.device('cpu'),
    log_results = True,
    interactive = True,
    debug = False,
) -> OthelloTrainer:
    checkpoint = torch.load(checkpoint_path, map_location=device)
    hypers: TurboZeroHypers = checkpoint['hypers']
    model = TurboZeroResnet(checkpoint['model_arch_params']).to(device)
    model.load_state_dict(checkpoint['model_state_dict'])

    optimizer = torch.optim.AdamW(model.parameters(), lr=hypers.learning_rate)
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    baselines = checkpoint['baselines']
    history = checkpoint['history']
    history.reset_all_figs()
    run_tag = checkpoint['run_tag']
    train_hypers = checkpoint['train_collector']['hypers']
    train_evaluator: OTHELLO_EVALUATORS = checkpoint['train_collector']['type'](num_parallel_envs, device, 8, train_hypers, debug=debug)
    test_hypers = checkpoint['test_collector']['hypers']
    test_evaluator: OTHELLO_EVALUATORS = checkpoint['test_collector']['type'](hypers.test_episodes_per_epoch, device, 8, test_hypers, debug=debug)

    return OthelloTrainer(train_evaluator, test_evaluator, num_parallel_envs, device, episode_memory_device, model, optimizer, hypers, history, baselines, log_results, interactive, run_tag)


 
