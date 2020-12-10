from rlberry.envs import Wrapper
import logging

logger = logging.getLogger(__name__)


class UncertaintyEstimatorWrapper(Wrapper):
    """
    Adds exploration bonuses to the info output of env.step(), according to an
    instance of UncertaintyEstimator.

    Example
    -------

    ```
    observation, reward, done, info = env.step(action)
    bonus = info['exploration_bonus']
    ```

    Parameters
    ----------
    uncertainty_estimator_fn : function
            Function that gives an instance of UncertaintyEstimator,
            used to compute bonus.
    bonus_scale_factor : double
            Scale factor for the bonus.
    """

    def __init__(self, env, uncertainty_estimator_fn, bonus_scale_factor=1.0):
        Wrapper.__init__(self, env)

        self.bonus_scale_factor = bonus_scale_factor
        self.uncertainty_estimator = uncertainty_estimator_fn()
        self.previous_obs = None

    def reset(self):
        self.previous_obs = self.env.reset()
        return self.previous_obs

    def _update_and_get_bonus(self, state, action, next_state, reward):
        if self.previous_obs is None:
            return 0.0
        #
        self.uncertainty_estimator.update(state,
                                          action,
                                          next_state,
                                          reward)
        bonus = self.uncertainty_estimator.measure(state, action)
        bonus = self.bonus_scale_factor*bonus
        return bonus

    def step(self, action):
        observation, reward, done, info = self.env.step(action)

        # update uncertainty and compute bonus
        bonus = self._update_and_get_bonus(self.previous_obs,
                                           action,
                                           observation,
                                           reward)
        #
        self.previous_obs = observation

        # add bonus to info
        if info is None:
            info = {}
        else:
            if 'exploration_bonus' in info:
                logger.error("UncertaintyEstimatorWrapper Error: info has" +
                             "  already a key named exploration_bonus!")

        info['exploration_bonus'] = bonus

        return observation, reward, done, info

    def sample(self, state, action):
        logger.warning(
            '[UncertaintyEstimatorWrapper]: sample()'
            + ' method does not consider nor update bonuses.')
        return self.env.sample(state, action)
