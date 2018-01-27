# Copyright 2017 reinforce.io. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from tensorforce import util
from tensorforce.models import DistributionModel
from tensorforce.core.networks import Network
from tensorforce.core.optimizers import Synchronization


class QModel(DistributionModel):
    """
    Q-value model.
    """

    def __init__(
        self,
        states,
        actions,
        scope,
        device,
        saver,
        summarizer,
        distributed,
        batching_capacity,
        variable_noise,
        states_preprocessing,
        actions_exploration,
        reward_preprocessing,
        update_mode,
        memory,
        optimizer,
        discount,
        network,
        distributions,
        entropy_regularization,
        target_sync_frequency,
        target_update_weight,
        double_q_model,
        huber_loss
    ):
        self.target_network_spec = network
        self.target_sync_frequency = target_sync_frequency
        self.target_update_weight = target_update_weight
        self.double_q_model = double_q_model

        # Huber loss
        assert huber_loss is None or huber_loss > 0.0
        self.huber_loss = huber_loss

        self.target_network = None
        self.target_optimizer = None
        self.target_distributions = None

        super(QModel, self).__init__(
            states=states,
            actions=actions,
            scope=scope,
            device=device,
            saver=saver,
            summarizer=summarizer,
            distributed=distributed,
            batching_capacity=batching_capacity,
            variable_noise=variable_noise,
            states_preprocessing=states_preprocessing,
            actions_exploration=actions_exploration,
            reward_preprocessing=reward_preprocessing,
            update_mode=update_mode,
            memory=memory,
            optimizer=optimizer,
            discount=discount,
            network=network,
            distributions=distributions,
            entropy_regularization=entropy_regularization,
            requires_deterministic=True
        )

    def initialize(self, custom_getter):
        super(QModel, self).initialize(custom_getter)

        # # TEMP: Random sampling fix
        # if self.random_sampling_fix:
        #     self.next_states_input = dict()
        #     for name, state in self.states_spec.items():
        #         self.next_states_input[name] = tf.placeholder(
        #             dtype=util.tf_dtype(state['type']),
        #             shape=(None,) + tuple(state['shape']),
        #             name=('next-' + name)
        #         )

        # Target network
        self.target_network = Network.from_spec(
            spec=self.target_network_spec,
            kwargs=dict(scope='target', summary_labels=self.summary_labels)
        )

        # Target network optimizer
        self.target_optimizer = Synchronization(
            sync_frequency=self.target_sync_frequency,
            update_weight=self.target_update_weight
        )

        # Target network distributions
        self.target_distributions = self.create_distributions()

    def tf_q_value(self, embedding, distr_params, action, name):
        # Mainly for NAF.
        return self.distributions[name].state_action_value(distr_params=distr_params, action=action)

    def tf_q_delta(self, q_value, next_q_value, terminal, reward):
        """
        Creates the deltas (or advantage) of the Q values.

        :return: A list of deltas per action
        """
        for _ in range(util.rank(q_value) - 1):
            terminal = tf.expand_dims(input=terminal, axis=1)
            reward = tf.expand_dims(input=reward, axis=1)

        multiples = (1,) + util.shape(q_value)[1:]
        terminal = tf.tile(input=terminal, multiples=multiples)
        reward = tf.tile(input=reward, multiples=multiples)

        zeros = tf.zeros_like(tensor=next_q_value)
        next_q_value = tf.where(condition=terminal, x=zeros, y=(self.discount * next_q_value))

        return reward + next_q_value - q_value  # tf.stop_gradient(q_target)

    def tf_loss_per_instance(self, states, internals, actions, terminal, reward, next_states, next_internals, update):
        embedding = self.network.apply(x=states, internals=internals, update=update)

        # fix
        if self.double_q_model:
            next_embedding = self.network.apply(
                x=next_states,
                internals=next_internals,
                update=update
            )

        # Both networks can use the same internals, could that be a problem?
        # Otherwise need to handle internals indices correctly everywhere
        target_embedding = self.target_network.apply(
            x=next_states,
            internals=next_internals,
            update=update
        )

        deltas = list()
        for name, distribution in self.distributions.items():
            target_distribution = self.target_distributions[name]

            distr_params = distribution.parameterize(x=embedding)
            target_distr_params = target_distribution.parameterize(x=target_embedding)

            q_value = self.tf_q_value(embedding=embedding, distr_params=distr_params, action=actions[name], name=name)

            if self.double_q_model:
                # fix
                next_distr_params = distribution.parameterize(x=next_embedding)
                action_taken = distribution.sample(distr_params=next_distr_params, deterministic=True)
            else:
                action_taken = target_distribution.sample(distr_params=target_distr_params, deterministic=True)

            next_q_value = target_distribution.state_action_value(distr_params=target_distr_params, action=action_taken)

            delta = self.tf_q_delta(q_value=q_value, next_q_value=next_q_value, terminal=terminal, reward=reward)

            collapsed_size = util.prod(util.shape(delta)[1:])
            delta = tf.reshape(tensor=delta, shape=(-1, collapsed_size))

            deltas.append(delta)

        # Surrogate loss as the mean squared error between actual observed rewards and expected rewards
        loss_per_instance = tf.reduce_mean(input_tensor=tf.concat(values=deltas, axis=1), axis=1)

        # Optional Huber loss
        if self.huber_loss is not None and self.huber_loss > 0.0:
            return tf.where(
                condition=(tf.abs(x=loss_per_instance) <= self.huber_loss),
                x=(0.5 * tf.square(x=loss_per_instance)),
                y=(self.huber_loss * (tf.abs(x=loss_per_instance) - 0.5 * self.huber_loss))
            )
        else:
            return tf.square(x=loss_per_instance)

    def tf_optimization(self, states, internals, actions, terminal, reward, next_states=None, next_internals=None):
        optimization = super(QModel, self).tf_optimization(
            states=states,
            internals=internals,
            actions=actions,
            terminal=terminal,
            reward=reward,
            next_states=next_states,
            next_internals=next_internals
        )

        network_distributions_variables = self.get_distributions_variables(self.distributions)
        target_distributions_variables = self.get_distributions_variables(self.target_distributions)

        target_optimization = self.target_optimizer.minimize(
            time=self.timestep,
            variables=self.target_network.get_variables() + target_distributions_variables,
            source_variables=self.network.get_variables() + network_distributions_variables
        )

        return tf.group(optimization, target_optimization)

    def get_variables(self, include_non_trainable=False):
        model_variables = super(QModel, self).get_variables(include_non_trainable=include_non_trainable)

        if include_non_trainable:
            # Target network and optimizer variables only included if 'include_non_trainable' set
            target_variables = self.target_network.get_variables(include_non_trainable=include_non_trainable)
            target_distributions_variables = self.get_distributions_variables(self.target_distributions)
            target_optimizer_variables = self.target_optimizer.get_variables()

            return model_variables + target_variables + target_optimizer_variables + target_distributions_variables

        else:
            return model_variables

    def get_summaries(self):
        target_distributions_summaries = self.get_distributions_summaries(self.target_distributions)
        return super(QModel, self).get_summaries() + self.target_network.get_summaries() + target_distributions_summaries

    # # TEMP: Random sampling fix
    # def update(self, states, internals, actions, terminal, reward, return_loss_per_instance=False):
    #     fetches = [self.optimization]

    #     # Optionally fetch loss per instance
    #     if return_loss_per_instance:
    #         fetches.append(self.loss_per_instance)

    #     terminal = np.asarray(terminal)
    #     batched = (terminal.ndim == 1)
    #     if batched:
    #         # TEMP: Random sampling fix
    #         if self.random_sampling_fix:
    #             feed_dict = {state_input: states[name][0] for name, state_input in self.states_input.items()}
    #             feed_dict.update({state_input: states[name][1] for name, state_input in self.next_states_input.items()})
    #         else:
    #             feed_dict = {state_input: states[name] for name, state_input in self.states_input.items()}
    #         feed_dict.update(
    #             {internal_input: internals[n]
    #                 for n, internal_input in enumerate(self.internals_input)}
    #         )
    #         feed_dict.update(
    #             {action_input: actions[name]
    #                 for name, action_input in self.actions_input.items()}
    #         )
    #         feed_dict[self.terminal_input] = terminal
    #         feed_dict[self.reward_input] = reward
    #     else:
    #         # TEMP: Random sampling fix
    #         if self.random_sampling_fix:
    #             raise TensorForceError("Unbatched version not covered by fix.")
    #         else:
    #             feed_dict = {state_input: (states[name],) for name, state_input in self.states_input.items()}
    #         feed_dict.update(
    #             {internal_input: (internals[n],)
    #                 for n, internal_input in enumerate(self.internals_input)}
    #         )
    #         feed_dict.update(
    #             {action_input: (actions[name],)
    #                 for name, action_input in self.actions_input.items()}
    #         )
    #         feed_dict[self.terminal_input] = (terminal,)
    #         feed_dict[self.reward_input] = (reward,)

    #     feed_dict[self.deterministic_input] = True
    #     feed_dict[self.update_input] = True

    #     fetched = self.monitored_session.run(fetches=fetches, feed_dict=feed_dict)

    #     if return_loss_per_instance:
    #         return fetched[1]
