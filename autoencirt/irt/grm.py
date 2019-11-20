#!/usr/bin/env python3

import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability.python import util as tfp_util
import numpy as np
import pandas as pd

from autoencirt.irt import IRTModel
from autoencirt.tools.tf import (
    clip_gradients, run_chain
)


import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability.python import util as tfp_util

from tensorflow_probability.python import util as tfp_util
from tensorflow_probability.python.mcmc.transformed_kernel import (
    make_transform_fn, make_transformed_log_prob, make_log_det_jacobian_fn)
from tensorflow_probability.python.experimental.vi.surrogate_posteriors import (
    build_factored_surrogate_posterior)

tfd = tfp.distributions

tfd = tfp.distributions
tfb = tfp.bijectors


class GRModel(IRTModel):
    """Implement and store the graded response model for IRT

    Arguments:
        IRTModel {[type]} -- [description]

    Returns:
        [type] -- [description]
    """
    data = None
    response_type = "polytomous"
    dimensional_decay = 0.1

    def __init__(self, auxiliary_parameterization=True):
        super().__init__()
        self.auxiliary_parameterization = auxiliary_parameterization

    def grm_model_prob(self, abilities, discriminations, difficulties):
        if len(difficulties.shape) == 3:
            offsets = (difficulties[tf.newaxis, :, :, :]
                       - abilities[:, :, tf.newaxis, tf.newaxis])
            scaled = offsets*discriminations[tf.newaxis, :, :, tf.newaxis]
            logits = 1.0/(1+tf.exp(scaled))
            logits = tf.pad(logits, paddings=(
                (0, 0), (0, 0), (0, 0), (1, 0)),
                mode='constant', constant_values=1)
            logits = tf.pad(logits, paddings=(
                (0, 0), (0, 0), (0, 0), (0, 1)),
                mode='constant', constant_values=0)
            probs = logits[:, :, :, :-1] - logits[:, :, :, 1:]
            # weight by discrimination
            # \begin{align}
            #   w_{id} &= \frac{\lambda_{i}^{(d)}}{\sum_d \lambda_{i}^{(d)}}.
            # \end{align}
            weights = (discriminations
                       / tf.reduce_sum(discriminations, axis=0)[tf.newaxis, :]
                       )
            probs = tf.reduce_sum(
                probs*weights[tf.newaxis, :, :, tf.newaxis], axis=1)
        else:
            offsets = (difficulties[:, tf.newaxis, ...]
                       - abilities[..., tf.newaxis, tf.newaxis])
            scaled = offsets*discriminations[:, tf.newaxis, :, :, tf.newaxis]
            logits = 1.0/(1+tf.exp(scaled))
            logits = tf.pad(logits, paddings=(
                (0, 0), (0, 0), (0, 0), (0, 0), (1, 0)),
                mode='constant', constant_values=1)
            logits = tf.pad(logits, paddings=(
                (0, 0), (0, 0), (0, 0), (0, 0), (0, 1)),
                mode='constant', constant_values=0)
            probs = logits[..., :-1] - logits[..., 1:]
            # weight by discrimination
            # \begin{align}
            #   w_{id} &= \frac{\lambda_{i}^{(d)}}{\sum_d \lambda_{i}^{(d)}}.
            # \end{align}
            weights = (
                discriminations
                / tf.reduce_sum(discriminations, axis=1)[:, tf.newaxis, :])
            probs = tf.reduce_sum(
                probs*weights[:, tf.newaxis, :, :, tf.newaxis], axis=2)
        return probs

    @tf.function
    def grm_model_prob_d(self,
                         abilities,
                         discriminations,
                         difficulties0,
                         ddifficulties
                         ):
        d0 = tf.concat(
            [difficulties0[..., tf.newaxis], ddifficulties], axis=-1)
        difficulties = tf.cumsum(d0, axis=-1)
        return self.grm_model_prob(abilities, discriminations, difficulties)

    @tf.function
    def compute_likelihood_distribution(self, abilities,
                                        discriminations, difficulties0,
                                        ddifficulties):
        return tfd.Independent(
            tfd.Categorical(
                self.grm_model_prob_d(
                    abilities,
                    discriminations,
                    difficulties0,
                    ddifficulties
                ),
                validate_args=True),
            reinterpreted_batch_ndims=2
        )

    def joint_prior_distribution(self):
        """Joint probability with measure over observations

        Returns:
            tf.distributions.JointDistributionNamed -- Joint distribution
        """
        K = self.response_cardinality
        grm_joint_distribution_dict = dict(
            mu=tfd.Independent(
                tfd.Normal(
                    loc=tf.zeros((self.dimensions, self.num_items)),
                    scale=1.
                ),
                reinterpreted_batch_ndims=2
            ),  # mu
            eta=tfd.Independent(
                tfd.HalfCauchy(
                    loc=tf.zeros((self.num_items)), scale=1.
                ),
                reinterpreted_batch_ndims=1
            ),  # eta
            xi=tfd.Independent(
                tfd.HalfCauchy(
                    loc=tf.zeros((self.dimensions, self.num_items)),
                    scale=(self.dimensional_decay**(
                        -tf.cast(tf.range(self.dimensions)+2, tf.float32)
                    ))[..., :, tf.newaxis]
                ),
                reinterpreted_batch_ndims=2
            ),  # xi
            difficulties0=lambda mu: tfd.Independent(
                tfd.Normal(loc=mu, scale=1.),
                reinterpreted_batch_ndims=2
            ),  # difficulties0
            discriminations=lambda eta, xi: tfd.Independent(
                tfd.HalfNormal(scale=tf.sqrt(eta[..., tf.newaxis, :]*xi)),
                reinterpreted_batch_ndims=2
            ),  # discrimination
            ddifficulties=tfd.Independent(
                tfd.HalfNormal(
                    scale=tf.ones(
                        (self.dimensions,
                         self.num_items,
                         self.response_cardinality-2
                         )
                    )
                ),
                reinterpreted_batch_ndims=3
            ),
            abilities=tfd.Independent(
                tfd.Normal(
                    loc=tf.zeros((self.num_people, self.dimensions)),
                    scale=1.
                ),
                reinterpreted_batch_ndims=2
            ),
            x=lambda abilities, discriminations, difficulties0, ddifficulties:
            tfd.Independent(
                tfd.Categorical(
                    self.grm_model_prob_d(
                        abilities,
                        discriminations,
                        difficulties0,
                        ddifficulties
                    ),
                    validate_args=True),
                reinterpreted_batch_ndims=2
            )
        )
        if self.auxiliary_parameterization:
            grm_joint_distribution_dict["xi_a"] = tfd.Independent(
                tfd.InverseGamma(
                    0.5*tf.ones((self.dimensions, self.num_items)),
                    (self.dimensional_decay**(
                        -tf.cast(tf.range(self.dimensions)+2, tf.float32)
                    ))[..., :, tf.newaxis]
                ),
                reinterpreted_batch_ndims=2
            )
            grm_joint_distribution_dict["xi"] = lambda xi_a: tfd.Independent(
                tfd.InverseGamma(
                    0.5*tf.ones((self.dimensions, self.num_items)),
                    1.0/xi_a
                ),
                reinterpreted_batch_ndims=2
            )
            grm_joint_distribution_dict["eta_a"] = tfd.Independent(
                tfd.InverseGamma(
                    0.5*tf.ones((self.num_items)),
                    tf.ones((self.num_items))
                ),
                reinterpreted_batch_ndims=1
            )
            grm_joint_distribution_dict["eta"] = lambda eta_a: tfd.Independent(
                tfd.InverseGamma(
                    0.5*tf.ones((self.num_items)),
                    1.0/eta_a
                ),
                reinterpreted_batch_ndims=1
            )

        return tfd.JointDistributionNamed(grm_joint_distribution_dict)

    def create_distributions(self):
        """Create the relevant prior distributions 
        and the surrogate distribution
        """
        self.weighted_likelihood = self.joint_prior_distribution()

        self.bijectors = {
            k: tfp.bijectors.Identity()
            for
            k
            in
            self.weighted_likelihood.parameters['model'].keys()}

        del self.bijectors['x']
        self.bijectors['eta'] = tfp.bijectors.Softplus()
        self.bijectors['xi'] = tfp.bijectors.Softplus()
        self.bijectors['discriminations'] = tfp.bijectors.Softplus()
        self.bijectors['ddifficulties'] = tfp.bijectors.Softplus()
        if self.auxiliary_parameterization:
            self.bijectors['eta_a'] = tfp.bijectors.Softplus()
            self.bijectors['xi_a'] = tfp.bijectors.Softplus()

        event_shape = self.weighted_likelihood.event_shape_tensor()
        del event_shape['x']
        variable_names = event_shape.keys()

        self.surrogate_posterior = build_factored_surrogate_posterior(
            event_shape=event_shape,
            constraining_bijectors=self.bijectors
        )

        posterior_samples = self.surrogate_posterior.sample(1000)
        self.calibrated_traits = tf.reduce_mean(
            posterior_samples['abilities'], axis=0
        )
        self.calibrated_traits_sd = tf.math.reduce_std(
            posterior_samples['abilities'], axis=0
        )
        self.calibrated_discriminations = tf.reduce_mean(
            posterior_samples['discriminations'], axis=0
        )
        self.calibrated_discriminations_sd = tf.math.reduce_std(
            posterior_samples['discriminations'], axis=0
        )
        self.calibrated_difficulties0 = tf.reduce_mean(
            posterior_samples['difficulties0'], axis=0
        )

        self.calibrated_ddifficulties = tf.reduce_mean(
            posterior_samples['ddifficulties'], axis=0
        )

        difficulty_samples = None
        self.calibrated_difficulties = tf.cumsum(
            tf.concat([
                tf.reduce_mean(
                    posterior_samples['difficulties0'], axis=0
                )[..., tf.newaxis],
                tf.reduce_mean(
                    posterior_samples['ddifficulties'], axis=0
                )
            ], axis=-1), axis=-1
        )
        self.calibrated_likelihood_distribution = tfd.JointDistributionNamed({
            'x': tfd.Independent(
                tfd.Categorical(
                    self.grm_model_prob(
                        self.calibrated_traits,
                        self.calibrated_discriminations,
                        self.calibrated_difficulties
                    ), validate_args=True
                ),
                reinterpreted_batch_ndims=2
            )
        })

    @tf.function
    def unormalized_log_prob(self, **x):
        x['x'] = self.calibration_data
        return self.weighted_likelihood.log_prob(x)

    def calibrate_advi(
            self, num_steps=10, initial_learning_rate=5e-2,
            decay_steps=10, decay_rate=0.99):

        learning_rate = tf.keras.optimizers.schedules.ExponentialDecay(
            initial_learning_rate=initial_learning_rate,
            decay_steps=decay_steps,
            decay_rate=decay_rate,
            staircase=True)
        opt = tf.optimizers.Adam(
            learning_rate=learning_rate,
            clipvalue=1.)

        @tf.function
        def run_approximation(num_steps):
            losses = tfp.vi.fit_surrogate_posterior(
                target_log_prob_fn=clip_gradients(
                    self.unormalized_log_prob, 10.),
                surrogate_posterior=self.surrogate_posterior,
                optimizer=opt,
                num_steps=num_steps,
                sample_size=10
            )
            return(losses)

        losses = run_approximation(num_steps)
        self.surrogate_samnple = self.surrogate_posterior.sample(1000)
        self.calibrated_traits = tf.reduce_mean(
            self.surrogate_samnple['abilities'], axis=0
        )
        self.calibrated_traits_sd = tf.math.reduce_std(
            self.surrogate_samnple['abilities'], axis=0
        )
        self.calibrated_discriminations = tf.reduce_mean(
            self.surrogate_samnple['discriminations'], axis=0
        )
        self.calibrated_discriminations_sd = tf.math.reduce_std(
            self.surrogate_samnple['discriminations'], axis=0
        )

        self.calibrated_difficulties0 = tf.reduce_mean(
            self.surrogate_samnple['difficulties0'], axis=0
        )

        self.calibrated_ddifficulties = tf.reduce_mean(
            self.surrogate_samnple['ddifficulties'], axis=0
        )

        difficulty_samples = None
        self.calibrated_difficulties = tf.cumsum(
            tf.concat([
                self.calibrated_difficulties0[..., tf.newaxis],
                self.calibrated_ddifficulties
            ], axis=-1), axis=-1
        )
        self.calibrated_likelihood_distribution = tfd.JointDistributionNamed({
            'x': tfd.Independent(
                tfd.Categorical(
                    self.grm_model_prob(
                        self.calibrated_traits,
                        self.calibrated_discriminations,
                        self.calibrated_difficulties
                    ), validate_args=True
                ),
                reinterpreted_batch_ndims=2
            )
        })
        print(losses)
        return

    @tf.function
    def unormalized_log_prob_list(self, *x):
        return self.unormalized_log_prob(
            **tf.nest.pack_sequence_as(
                self.surrogate_sample,
                x
            ))
        
    def calibrate_mcmc(self, init_state=None, step_size=1e-3,
                       num_steps=1000, burnin=500):
        """Calibrate using HMC/NUT

        Keyword Arguments:
            num_chains {int} -- [description] (default: {1})
        """
        if init_state is None:
            surrogate_expectations = {
                k: tf.reduce_mean(v, axis=0)
                for k, v in self.surrogate_samnple.items()}
            init_state = surrogate_expectations

        samples, sampler_stat = run_chain(
            init_state=tf.nest.flatten(init_state),
            step_size=step_size,
            target_log_prob_fn=self.unormalized_log_prob_list,
            unconstraining_bijectors=self.bijectors,
            num_steps=num_steps,
            burnin=burnin
        )
        return samples, sampler_stat

    def score(self, responses):
        num_people = responses.shape[0]

        pass

    def loss(self, responses, scores):
        pass
