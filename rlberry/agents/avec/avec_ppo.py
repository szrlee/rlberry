import numpy as np
import torch
import logging
import torch.nn as nn

import gym.spaces as spaces
from rlberry.agents import IncrementalAgent
from rlberry.agents.utils.memories import Memory
from rlberry.agents.utils.torch_training import optimizer_factory
from rlberry.agents.utils.torch_models import default_policy_net_fn
from rlberry.agents.utils.torch_models import default_value_net_fn
from rlberry.utils.writers import PeriodicWriter

logger = logging.getLogger(__name__)
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class AVECPPOAgent(IncrementalAgent):
    """
    AVEC uses a modification of the training objective for the critic in
    actor-critic algorithms to better approximate the value function (critic).
    The new state-value function approximation learns the *relative* value of
    the states rather than their *absolute* value as in conventional
    actor-critic. This modification is:
    - well-motivated by recent studies [1,2];
    - theoretically sound;
    - intuitively supported by the need to improve the approximation error
    of the critic.

    The application of Actor with Variance Estimated Critic (AVEC) to
    state-of-the-art policy gradient methods produces considerable
    gains in performance (on average +26% for SAC and +40% for PPO)
    over the standard actor-critic training.

    Parameters
    ----------
    env : Model
        model with continuous (Box) state space and discrete actions
    n_episodes : int
        Number of episodes
    batch_size : int
        Number of episodes to wait before updating the policy.
    horizon : int
        Horizon of the objective function. If None and gamma<1,
        set to 1/(1-gamma).
    gamma : double
        Discount factor in [0, 1]. If gamma is 1.0, the problem is set
        to be finite-horizon.
    entr_coef : double
        Entropy coefficient.
    vf_coef : double
        Value function loss coefficient.
    learning_rate : double
        Learning rate.
    optimizer_type: str
        Type of optimizer. 'ADAM' by defaut.
    eps_clip : double
        PPO clipping range (epsilon).
    k_epochs : int
        Number of epochs per update.
    policy_net_fn : function
        Function that returns an instance of a policy network (pytorch).
        If None, a default net is used.
    value_net_fn : function
        Function that returns an instance of a value network (pytorch).
        If None, a default net is used.
    use_bonus_if_available : bool, default = False
        If true, check if environment info has entry 'exploration_bonus'
        and add it to the reward. See also UncertaintyEstimatorWrapper.

    References
    ----------
    Flet-Berliac, Y., Ouhamma, R., Maillard, O. A., & Preux, P. (2020).
    "Is Standard Deviation the New Standard? Revisiting the Critic in Deep
    Policy Gradients."
    arXiv preprint arXiv:2010.04440.

    [1] Ilyas, A., Engstrom, L., Santurkar, S., Tsipras, D., Janoos, F.,
    Rudolph, L. & Madry, A. (2020).
    "A closer look at deep policy gradients."
    In International Conference on Learning Representations.

    [2] Tucker, G., Bhupatiraju, S., Gu, S., Turner, R., Ghahramani, Z. &
    Levine, S. (2018).
    "The mirage of action-dependent baselines in reinforcement learning."
    In International Conference on Machine Learning, pp. 5015–5024.
    """

    name = "AVECPPO"
    fit_info = ("n_episodes", "episode_rewards")

    def __init__(self, env,
                 n_episodes=4000,
                 batch_size=8,
                 horizon=256,
                 gamma=0.99,
                 entr_coef=0.01,
                 vf_coef=0.,
                 avec_coef=1.,
                 learning_rate=0.0003,
                 optimizer_type='ADAM',
                 eps_clip=0.2,
                 k_epochs=10,
                 policy_net_fn=None,
                 value_net_fn=None,
                 use_bonus_if_available=False,
                 **kwargs):
        IncrementalAgent.__init__(self, env, **kwargs)

        self.learning_rate = learning_rate
        self.gamma = gamma
        self.entr_coef = entr_coef
        self.vf_coef = vf_coef
        self.avec_coef = avec_coef
        self.eps_clip = eps_clip
        self.k_epochs = k_epochs
        self.horizon = horizon
        self.n_episodes = n_episodes
        self.batch_size = batch_size
        self.use_bonus_if_available = use_bonus_if_available

        self.state_dim = self.env.observation_space.shape[0]
        self.action_dim = self.env.action_space.n

        #
        self.policy_net_fn = policy_net_fn \
            or (lambda: default_policy_net_fn(self.env))

        self.value_net_fn = value_net_fn \
            or (lambda: default_value_net_fn(self.env))

        self.optimizer_kwargs = {'optimizer_type': optimizer_type,
                                 'lr': learning_rate}

        # check environment
        assert isinstance(self.env.observation_space, spaces.Box)
        assert isinstance(self.env.action_space, spaces.Discrete)

        self.cat_policy = None  # categorical policy function

        # initialize
        self.reset()

    def reset(self, **kwargs):
        self.cat_policy = self.policy_net_fn().to(device)
        self.policy_optimizer = optimizer_factory(
                                    self.cat_policy.parameters(),
                                    **self.optimizer_kwargs)

        self.value_net = self.value_net_fn().to(device)
        self.value_optimizer = optimizer_factory(
                                    self.value_net.parameters(),
                                    **self.optimizer_kwargs)

        self.cat_policy_old = self.policy_net_fn().to(device)
        self.cat_policy_old.load_state_dict(self.cat_policy.state_dict())

        self.MseLoss = nn.MSELoss()

        self.memory = Memory()

        self.episode = 0

        # useful data
        self._rewards = np.zeros(self.n_episodes)
        self._cumul_rewards = np.zeros(self.n_episodes)

        # default writer
        self.writer = PeriodicWriter(self.name,
                                     log_every=5*logger.getEffectiveLevel())

    def policy(self, state, **kwargs):
        assert self.cat_policy is not None
        state = torch.from_numpy(state).float().to(device)
        action_dist = self.cat_policy_old(state)
        action = action_dist.sample().item()

        return action

    def partial_fit(self, fraction: float, **kwargs):
        assert 0.0 < fraction <= 1.0
        n_episodes_to_run = int(np.ceil(fraction * self.n_episodes))
        count = 0
        while count < n_episodes_to_run and self.episode < self.n_episodes:
            self._run_episode()
            count += 1

        info = {"n_episodes": self.episode,
                "episode_rewards": self._rewards[:self.episode]}
        return info

    def _select_action(self, state):
        state = torch.from_numpy(state).float().to(device)
        action_dist = self.cat_policy_old(state)
        action = action_dist.sample()
        action_logprob = action_dist.log_prob(action)

        self.memory.states.append(state)
        self.memory.actions.append(action)
        self.memory.logprobs.append(action_logprob)

        return action.item()

    def _run_episode(self):
        # interact for H steps
        episode_rewards = 0
        state = self.env.reset()
        for _ in range(self.horizon):
            # running policy_old
            action = self._select_action(state)
            next_state, reward, done, info = self.env.step(action)

            # check whether to use bonus
            bonus = 0.0
            if self.use_bonus_if_available:
                if info is not None and 'exploration_bonus' in info:
                    bonus = info['exploration_bonus']

            # save in batch
            self.memory.rewards.append(reward+bonus)  # add bonus here
            self.memory.is_terminals.append(done)
            episode_rewards += reward

            if done:
                break

            # update state
            state = next_state

        # update
        ep = self.episode
        self._rewards[ep] = episode_rewards
        self._cumul_rewards[ep] = episode_rewards \
            + self._cumul_rewards[max(0, ep - 1)]
        self.episode += 1

        #
        if self.writer is not None:
            self.writer.add_scalar("episode", self.episode, None)
            self.writer.add_scalar("ep reward", episode_rewards)

        #
        if self.episode % self.batch_size == 0:
            self._update()
            self.memory.clear_memory()

        return episode_rewards

    def _update(self):
        # monte carlo estimate of rewards
        rewards = []
        discounted_reward = 0
        for reward, is_terminal in zip(reversed(self.memory.rewards),
                                       reversed(self.memory.is_terminals)):
            if is_terminal:
                discounted_reward = 0
            discounted_reward = reward + (self.gamma * discounted_reward)
            rewards.insert(0, discounted_reward)

        # normalizing the rewards
        rewards = torch.tensor(rewards).to(device).float()
        rewards = (rewards - rewards.mean()) / (rewards.std() + 1e-5)

        # convert list to tensor
        old_states = torch.stack(self.memory.states).to(device).detach()
        old_actions = torch.stack(self.memory.actions).to(device).detach()
        old_logprobs = torch.stack(self.memory.logprobs).to(device).detach()

        # optimize policy for K epochs
        for _ in range(self.k_epochs):
            # evaluate old actions and values
            action_dist = self.cat_policy(old_states)
            logprobs = action_dist.log_prob(old_actions)
            state_values = torch.squeeze(self.value_net(old_states))
            dist_entropy = action_dist.entropy()

            # find ratio (pi_theta / pi_theta__old)
            ratios = torch.exp(logprobs - old_logprobs.detach())

            # normalize the advantages
            advantages = rewards - state_values.detach()
            advantages = (advantages - advantages.mean()) / \
                         (advantages.std() + 1e-8)
            # find surrogate loss
            surr1 = ratios * advantages
            surr2 = torch.clamp(ratios, 1 - self.eps_clip, 1
                                + self.eps_clip) * advantages
            loss = -torch.min(surr1, surr2) \
                + self.avec_coef * self._avec_loss(state_values, rewards) \
                + self.vf_coef * self.MseLoss(state_values, rewards) \
                - self.entr_coef * dist_entropy

            # take gradient step
            self.policy_optimizer.zero_grad()
            self.value_optimizer.zero_grad()

            loss.mean().backward()

            self.policy_optimizer.step()
            self.value_optimizer.step()

        # copy new weights into old policy
        self.cat_policy_old.load_state_dict(self.cat_policy.state_dict())

    def _avec_loss(self, y_pred, y_true):
        """
        Computes the objective function used in AVEC for the learning
        of the value function:
        the residual variance between the state-values and the
        empirical returns.

        Returns Var[y-ypred]
        :param y_pred: (np.ndarray) the prediction
        :param y_true: (np.ndarray) the expected value
        :return: (float) residual variance of ypred and y
        """
        assert y_true.ndim == 1 and y_pred.ndim == 1

        return torch.var(y_true - y_pred)

    #
    # For hyperparameter optimization
    #
    @classmethod
    def sample_parameters(cls, trial):
        batch_size = trial.suggest_categorical('batch_size',
                                               [1, 4, 8, 16, 32])
        gamma = trial.suggest_categorical('gamma',
                                          [0.9, 0.95, 0.99])
        learning_rate = trial.suggest_loguniform('learning_rate', 1e-5, 1)

        entr_coef = trial.suggest_loguniform('entr_coef', 1e-8, 0.1)

        eps_clip = trial.suggest_categorical('eps_clip',
                                             [0.1, 0.2, 0.3])

        k_epochs = trial.suggest_categorical('k_epochs',
                                             [1, 5, 10, 20])

        return {
                'batch_size': batch_size,
                'gamma': gamma,
                'learning_rate': learning_rate,
                'entr_coef': entr_coef,
                'eps_clip': eps_clip,
                'k_epochs': k_epochs,
                }
