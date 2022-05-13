# coding=utf-8
# Copyright 2020 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Ranking Python Bandit environment with items as per-arm features.

The observations are drawn with the help of the arguments `global_sampling_fn`
and `item_sampling_fn`.

The user is modeled the following way: the score of an item is calculated as a
weighted inner product of the global feature and the item feature. These scores
for all elements of a recommendation are treated as unnormalized logits for a
categorical distribution.

To model diversity and no-click, one can choose one from the following options:
  --Do the following trick: every action (a list of recommended items) gets
    `item_dim` many extra "ghost actions", represented with unit vectors as item
    features. If, based on inner products and all the items in the
    recommendation, one of these ghost items is chosen by the environment's user
    model, it means there was no suitable candidate `in the neighborhood`, and
    thus it means that the user did not click on any of the real items. This
    somewhat relates to diversity, as if the item feature space had been covered
    better, the ghost items would have been selected with very low probability.
  --Calculate the scores of all items, and if none of them exceeds a given
    threshold, no item is selected by the user.

"""
from typing import Optional, Callable, Text

import numpy as np
import tensorflow as tf

from tf_agents.bandits.environments import bandit_py_environment
from tf_agents.bandits.specs import utils as bandit_spec_utils
from tf_agents.specs import array_spec
from tf_agents.trajectories import time_step as ts
from tf_agents.typing import types

GLOBAL_KEY = bandit_spec_utils.GLOBAL_FEATURE_KEY
PER_ARM_KEY = bandit_spec_utils.PER_ARM_FEATURE_KEY


class FeedbackModel(object):
  """Enumeration of feedback models."""
  # No feedback model specified.
  UNKNOWN = 0
  # Cascading feedback model: A tuple of the chosen index and its value.
  CASCADING = 1


class ClickModel(object):
  """Enumeration of user click models."""
  # No feedback model specified.
  UNKNOWN = 0
  # For every dimension of the item space, a unit vector is added to the list of
  # available items. If one of these unit-vector items gets selected, it results
  # in a `no-click`.
  GHOST_ACTIONS = 1
  # Inner-product scores are calculated, and if none of the scores exceed a
  # given parameter, no item is clicked.
  DISTANCE_BASED = 2


class RankingPyEnvironment(
    bandit_py_environment.BanditPyEnvironment):
  """Stationary Stochastic Bandit environment with per-arm features."""
  _observation: types.NestedArray

  def __init__(self,
               global_sampling_fn: Callable[[], types.Array],
               item_sampling_fn: Callable[[], types.Array],
               num_items: int,
               num_slots: int,
               scores_weight_matrix: types.Float,
               feedback_model: int = FeedbackModel.CASCADING,
               click_model: int = ClickModel.GHOST_ACTIONS,
               distance_threshold: Optional[float] = None,
               batch_size: Optional[int] = 1,
               name: Optional[Text] = 'ranking_environment'):
    """Initializes the environment.

    In each round, global context is generated by global_sampling_fn, item
    contexts are generated by item_sampling_fn. The score matrix is of shape
    `[item_dim, global_dim]`, and plays the role of the weight matrix in the
    inner product of item and global features. This inner product gives scores
    for items, based on which the modelled user chooses items.

    In veery round, an extra all-zero item is mixed in the recommendation. If
    the modelled user chooses this ghost item, it will count as a no-click.

    Args:
      global_sampling_fn: A function that outputs a random 1d array or
        list of ints or floats. This output is the global context. Its shape and
        type must be consistent across calls.
      item_sampling_fn: A function that outputs a random 1 array or list
        of ints or floats (same type as the output of
        `global_context_sampling_fn`). This output is the per-arm context. Its
        shape must be consistent across calls.
      num_items: (int) the number of items in every sample.
      num_slots: (int) the number of items recommended in every round.
      scores_weight_matrix: A tensor of shape `[item_dim, global_dim]`. The
        score of an item is calculated as `global * M * item + noise`.
      feedback_model: The type of feedback model. Currently only implemented is
        -- `cascading`: the feedback is a tuple `(k, v)`, where `k` is the
           index of the chosen item, and `v` is the value of the choice.
      click_model: The way the environment models that diversity is desired.
        -- `ghost_actions`: For every dimension of the item space, a unit vector
           is added to the list of available items. If one of these unit-vector
           items gets selected, it results in a `no-click`.
        -- `distance_based`: Inner-product scores are calculated, and if none of
           the scores exceed a given parameter, no item is clicked.
      distance_threshold: (float) In case the diversity model is distance based,
        this threshold governs if the user actually clicked on any of the items.
      batch_size: The batch size.
      name: The name of this environment instance.
    """
    self._global_sampling_fn = global_sampling_fn
    self._item_sampling_fn = item_sampling_fn
    self._num_items = num_items
    self._num_slots = num_slots
    self._scores_weight_matrix = scores_weight_matrix
    self._feedback_model = feedback_model
    self._batch_size = batch_size
    self._click_model = click_model
    if click_model == ClickModel.DISTANCE_BASED:
      assert distance_threshold is not None, (
          'If the diversity model is `DISTANCE_BASED`, '
          'the distance threshold must be set.')
    self._distance_threshold = distance_threshold

    global_spec = array_spec.ArraySpec.from_array(global_sampling_fn())
    item_spec = array_spec.add_outer_dims_nest(
                array_spec.ArraySpec.from_array(item_sampling_fn()),
                (num_items,))
    observation_spec = {GLOBAL_KEY: global_spec, PER_ARM_KEY: item_spec}
    self._global_dim = global_spec.shape[0]
    self._item_dim = item_spec.shape[-1]

    action_spec = array_spec.BoundedArraySpec(
        shape=(num_slots,),
        dtype=np.int32,
        minimum=0,
        maximum=num_items - 1,
        name='action')

    if feedback_model == FeedbackModel.CASCADING:
      # `chosen_index == num_slots` means no recommended item was clicked.
      reward_spec = {
          'chosen_index':
              array_spec.BoundedArraySpec(
                  shape=[],
                  minimum=0,
                  maximum=num_slots,
                  dtype=np.int32,
                  name='chosen_index'),
          'chosen_value':
              array_spec.ArraySpec(
                  shape=[], dtype=np.float32, name='chosen_value')
      }
    else:
      raise NotImplementedError(
          'Feedback model {} not implemented'.format(feedback_model))

    super(RankingPyEnvironment, self).__init__(
        observation_spec, action_spec, reward_spec, name=name)

  def batched(self) -> bool:
    return True

  @property
  def batch_size(self) -> int:
    return self._batch_size

  def _observe(self) -> types.NestedArray:
    global_obs = np.stack(
        [self._global_sampling_fn() for _ in range(self._batch_size)])
    item_obs = np.reshape([
        self._item_sampling_fn()
        for _ in range(self._batch_size * self._num_items)
    ], (self._batch_size, self._num_items, -1))
    self._observation = {GLOBAL_KEY: global_obs, PER_ARM_KEY: item_obs}
    return self._observation

  def _apply_action(self, action: np.ndarray) -> types.Array:
    if action.shape[0] != self.batch_size:
      raise ValueError('Number of actions must match batch size.')
    global_obs = self._observation[GLOBAL_KEY]
    item_obs = self._observation[PER_ARM_KEY]
    batch_size_range = range(self.batch_size)
    slotted_items = item_obs[np.expand_dims(batch_size_range, axis=1), action]
    if self._click_model == ClickModel.GHOST_ACTIONS:
      chosen_items = self._choose_items_ghost_actions(global_obs, slotted_items)
    elif self._click_model == ClickModel.DISTANCE_BASED:
      chosen_items = self._choose_items_distance_based(global_obs,
                                                       slotted_items)
    else:
      raise NotImplementedError('Diversity model {} not implemented'.format(
          self._click_model))

    if self._feedback_model == FeedbackModel.CASCADING:
      chosen_items = np.array(
          chosen_items, dtype=self._reward_spec['chosen_index'].dtype)
      chosen_values = (chosen_items < self._num_slots).astype(
          self._reward_spec['chosen_value'].dtype)
      return {'chosen_index': chosen_items, 'chosen_value': chosen_values}

  def _step(self, action):
    """We need to override this function because the reward dtype can be int."""
    # TODO(b/199824775): The trajectory module assumes all reward is float32.
    # Sort this out with TF-Agents.
    output = super(RankingPyEnvironment, self)._step(action)
    reward = output.reward
    new_reward = tf.nest.map_structure(lambda x, t: x.astype(t), reward,
                                       self.reward_spec())
    return ts.TimeStep(
        step_type=output.step_type,
        reward=new_reward,
        discount=output.discount,
        observation=output.observation)

  def _batched_inner_product(self, global_obs, item_obs):
    left = np.matmul(item_obs, self._scores_weight_matrix)
    expanded_left = np.expand_dims(left, axis=-2)
    expanded_globals = np.reshape(
        global_obs, newshape=[self._batch_size, 1, self._global_dim, 1])
    scores = np.reshape(
        np.matmul(expanded_left, expanded_globals),
        newshape=[self._batch_size, -1])
    return scores

  def _choose_items_ghost_actions(self, global_obs, slotted_items):
    # If one of the unit vectors gets chosen, it means no-click.
    slotted_items_with_units = np.concatenate([
        slotted_items,
        np.broadcast_to(
            np.identity(self._item_dim),
            [self._batch_size, self._item_dim, self._item_dim])
    ],
                                              axis=1)

    scores = self._batched_inner_product(global_obs, slotted_items_with_units)
    perturbed_scores = np.random.normal(loc=scores, scale=1)
    unnormalized_probabilities = 1 / (1 + np.exp(-perturbed_scores))
    probabilities = unnormalized_probabilities / np.expand_dims(
        np.linalg.norm(unnormalized_probabilities, ord=1, axis=-1), axis=1)

    return np.minimum([
        np.random.choice(np.arange(self._num_slots + self._item_dim), p=p)
        for p in probabilities
    ], self._num_slots)

  def _choose_items_distance_based(self, global_obs, slotted_items):
    scores = self._batched_inner_product(global_obs, slotted_items)
    scores = np.concatenate(
        [scores,
         np.ones([self._batch_size, 1]) * self._distance_threshold], axis=1)
    return np.argmax(scores, axis=1)