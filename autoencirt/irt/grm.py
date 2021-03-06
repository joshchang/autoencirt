#!/usr/bin/env python3
import itertools

import numpy as np
import pandas as pd
import tensorflow as tf
import tensorflow_probability as tfp

from factor_analyzer import (
    FactorAnalyzer)

from autoencirt.irt import IRTModel
from bayesianquilts.util import (
    build_trainable_InverseGamma_dist,
    build_trainable_normal_dist, build_surrogate_posterior,
    run_chain)

from bayesianquilts.distributions import SqrtInverseGamma, AbsHorseshoe

from tensorflow_probability.python import util as tfp_util
from tensorflow_probability.python.bijectors import softplus as softplus_lib
from tensorflow_probability.python.mcmc.transformed_kernel import (
    make_log_det_jacobian_fn, make_transform_fn, make_transformed_log_prob)

tfd = tfp.distributions

tfd = tfp.distributions
tfb = tfp.bijectors

LogNormal = tfd.LogNormal


class GRModel(IRTModel):
    """Implement and store the graded response model for IRT

    Arguments:
        IRTModel {[type]} -- [description]

    Returns:
        [type] -- [description]
    """
    response_type = "polytomous"

    def __init__(self, *args, **kwargs):
        super(GRModel, self).__init__(*args, **kwargs)
        if self.data is not None:
            print("Computing a factor analysis")
            _batch_size = min(self.data_cardinality, 100)
            df = next(iter(self.data.shuffle(200).batch(self.data_cardinality)))
            df = {
                k: v.numpy() for k, v in df.items() if k in self.item_keys
            }
            df = pd.DataFrame(df)
            df[df < 0] = np.nan
            df = df.dropna()
            if len(df) > 5:
                fa = FactorAnalyzer(n_factors=self.dimensions)
                fa.fit(df)
                self.factor_loadings = fa.loadings_
                
            else:
                print("Not doing a factor analysis because we have too much missingness")
        self.create_distributions()

    def grm_model_prob(self, abilities, discriminations, difficulties):
        offsets = difficulties - abilities  # N x D x I x K-1
        scaled = offsets*discriminations
        logits = 1.0/(1+tf.exp(scaled))
        logits = tf.pad(
            logits,
            paddings=(
                [(0, 0)]*(len(logits.shape) - 1) + [(1, 0)]),
            mode='constant',
            constant_values=1)
        logits = tf.pad(
            logits,
            paddings=(
                [(0, 0)]*(len(logits.shape) - 1) + [(0, 1)]),
            mode='constant', constant_values=0)
        probs = logits[..., :-1] - logits[..., 1:]

        # weight by discrimination
        # \begin{align}
        #   w_{id} &= \frac{\lambda_{i}^{(d)}}{\sum_d \lambda_{i}^{(d)}}.
        # \end{align}
        weights = (
            tf.math.abs(discriminations)**self.weight_exponent
            / tf.reduce_sum(
                tf.math.abs(discriminations)**self.weight_exponent,
                axis=-3)[..., tf.newaxis, :, :])
        probs = tf.reduce_sum(probs*weights, axis=-3)
        return probs

    def grm_model_prob_d(self,
                         abilities,
                         discriminations,
                         difficulties0,
                         ddifficulties
                         ):
        d0 = tf.concat(
            [difficulties0, ddifficulties], axis=-1)
        difficulties = tf.cumsum(d0, axis=-1)
        return self.grm_model_prob(abilities, discriminations, difficulties)

    def log_likelihood(
            self, responses, discriminations,
            difficulties0, ddifficulties,
            abilities, *args, **kwargs):
        # check if responses are a batch
        difficulties = tf.concat(
            [difficulties0, ddifficulties], axis=-1)
        difficulties = tf.cumsum(difficulties, axis=-1)

        # gather abilities and item parameters corresponding to responses

        rank = len(abilities._shape_as_list())
        batch_shape = abilities._shape_as_list()[:(rank-4)]
        batch_ndims = len(batch_shape)

        people = tf.cast(
            responses[self.person_key], tf.int32)
        choices = tf.concat(
            [responses[i][:, tf.newaxis] for i in self.item_keys],
            axis=-1)

        bad_choices = tf.less(choices, 0)

        choices = tf.where(
            bad_choices, tf.zeros_like(choices), choices)

        for _ in range(batch_ndims):
            choices = choices[tf.newaxis, ...]

        transpose1 = (
            [batch_ndims] + list(range(batch_ndims)) +
            list(range(batch_ndims+1, rank))
        )
        abilities = tf.gather_nd(
            tf.transpose(
                abilities, transpose1
            ), people[..., tf.newaxis])
        abilities = tf.transpose(
            abilities, transpose1
        )

        response_probs = self.grm_model_prob(
            abilities, discriminations, difficulties)

        rv_responses = tfd.Categorical(
            probs=response_probs)

        log_probs = rv_responses.log_prob(choices)
        log_probs = tf.where(
            bad_choices[tf.newaxis, ...],
            tf.zeros_like(log_probs),
            log_probs
        )

        log_probs = tf.reduce_sum(log_probs, axis=-1)
        log_probs = tf.reduce_sum(log_probs, axis=-1)

        return log_probs

    def create_distributions(self):
        """Joint probability with measure over observations

        Returns:
            tf.distributions.JointDistributionNamed -- Joint distribution
        """
        self.bijectors = {
            k: tfp.bijectors.Identity()
            for
            k
            in
            ['abilities', 'mu', 'difficulties0']
        }

        self.bijectors['eta'] = tfp.bijectors.Softplus()
        self.bijectors['xi'] = tfp.bijectors.Softplus()

        self.bijectors['discriminations'] = tfp.bijectors.Softplus()
        self.bijectors['ddifficulties'] = tfp.bijectors.Softplus()

        self.bijectors['eta_a'] = tfp.bijectors.Softplus()
        self.bijectors['xi_a'] = tfp.bijectors.Softplus()

        K = self.response_cardinality
        difficulties0 = np.sort(
            np.random.normal(
                size=(1,
                      self.dimensions,
                      self.num_items,
                      K-1)
            ),
            axis=-1)

        grm_joint_distribution_dict = dict(
            mu=tfd.Independent(
                tfd.Normal(
                    loc=tf.zeros(
                        (1, self.dimensions, self.num_items, 1),
                        dtype=self.dtype),
                    scale=tf.ones(
                        (1, self.dimensions, self.num_items, 1),
                        dtype=self.dtype)
                ),
                reinterpreted_batch_ndims=4
            ),  # mu
            difficulties0=lambda mu: tfd.Independent(
                tfd.Normal(
                    loc=mu,
                    scale=tf.ones(
                        (1, self.dimensions, self.num_items, 1),
                        dtype=self.dtype)),
                reinterpreted_batch_ndims=4
            ),  # difficulties0
            discriminations=(
                lambda eta, xi: tfd.Independent(
                    AbsHorseshoe(scale=eta*xi),
                    reinterpreted_batch_ndims=4
                )),
            ddifficulties=tfd.Independent(
                tfd.HalfNormal(
                    scale=tf.ones(
                        (1, self.dimensions, self.num_items,
                         self.response_cardinality-2), dtype=self.dtype
                    )
                ),
                reinterpreted_batch_ndims=4
            ),
            abilities=tfd.Independent(
                tfd.Normal(
                    loc=tf.zeros(
                        (self.num_people, self.dimensions, 1, 1),
                        dtype=self.dtype),
                    scale=tf.ones(
                        (self.num_people, self.dimensions, 1, 1),
                        dtype=self.dtype)
                ),
                reinterpreted_batch_ndims=4
            ),
            xi_a=tfd.Independent(
                tfd.InverseGamma(
                    0.5*tf.ones(
                        (1, self.dimensions, 1, 1),
                        dtype=self.dtype),
                    tf.ones(
                        (1, self.dimensions, 1, 1),
                        dtype=self.dtype)/self.xi_scale**2
                ),
                reinterpreted_batch_ndims=4
            ),
            xi=lambda xi_a: tfd.Independent(
                SqrtInverseGamma(
                    0.5*tf.ones(
                        (1, self.dimensions, 1, 1),
                        dtype=self.dtype),
                    1.0/xi_a
                ),
                reinterpreted_batch_ndims=4
            ),
            eta=lambda eta_a: tfd.Independent(
                SqrtInverseGamma(
                    0.5*tf.ones(
                        (1, 1, self.num_items, 1), dtype=self.dtype),
                    1.0/eta_a
                ),
                reinterpreted_batch_ndims=4
            ),
            eta_a=tfd.Independent(
                tfd.InverseGamma(
                    0.5*tf.ones(
                        (1, 1, self.num_items, 1),
                        dtype=self.dtype),
                    tf.ones(
                        (1, 1, self.num_items, 1),
                        dtype=self.dtype)/self.eta_scale**2
                ),
                reinterpreted_batch_ndims=4
            )
        )

        """

            x=lambda abilities, discriminations, difficulties0, ddifficulties:
            tfd.Independent(
                tfd.Categorical(
                    probs=self.grm_model_prob_d(
                        abilities,
                        discriminations,
                        difficulties0,
                        ddifficulties
                    ),
                    validate_args=True),
                reinterpreted_batch_ndims=2
            )
        """

        surrogate_distribution_dict = {
            'abilities': build_trainable_normal_dist(
                tf.zeros(
                    (self.num_people, self.dimensions, 1, 1),
                    dtype=self.dtype),
                1e-1*tf.ones(
                    (self.num_people, self.dimensions, 1, 1),
                    dtype=self.dtype),
                4
            ),
            'ddifficulties': self.bijectors['ddifficulties'](
                build_trainable_normal_dist(
                    tf.cast(
                        difficulties0[..., 1:]-difficulties0[..., :-1],
                        dtype=self.dtype),
                    1e-2*tf.ones(
                        (1,
                         self.dimensions,
                         self.num_items,
                         self.response_cardinality-2
                         ), dtype=self.dtype),
                    4
                )
            ),
            'discriminations': self.bijectors['discriminations'](
                build_trainable_normal_dist(
                    #tf.cast(
                    #    (1.+np.abs(self.factor_loadings.T)),
                    #    self.dtype)[tf.newaxis, ..., tf.newaxis],
                    -4.*tf.ones(
                        (1, self.dimensions, self.num_items, 1),
                        dtype=self.dtype),
                    1e-1*tf.ones(
                        (1, self.dimensions, self.num_items, 1),
                        dtype=self.dtype),
                    4
                )
            ),
            'mu': build_trainable_normal_dist(
                tf.ones(
                    (1, self.dimensions, self.num_items, 1),
                    dtype=self.dtype),
                1e-2*tf.ones(
                    (1, self.dimensions, self.num_items, 1),
                    dtype=self.dtype),
                4
            )
        }
        surrogate_distribution_dict = {
            **surrogate_distribution_dict,
            'eta': self.bijectors['eta'](
                build_trainable_InverseGamma_dist(
                    0.5*tf.ones(
                        (1, 1, self.num_items, 1),
                        dtype=self.dtype),
                    tf.ones(
                        (1, 1, self.num_items, 1),
                        dtype=self.dtype),
                    4
                )
            ),
            'xi': self.bijectors['xi'](
                build_trainable_InverseGamma_dist(
                    0.5*tf.ones(
                        (1, self.dimensions, 1, 1),
                        dtype=self.dtype),
                    tf.ones(
                        (1, self.dimensions, 1, 1),
                        dtype=self.dtype),
                    4
                )
            ),
            'difficulties0': build_trainable_normal_dist(
                tf.ones(
                    (1, self.dimensions, self.num_items, 1),
                    dtype=self.dtype),
                1e-2*tf.ones(
                    (1, self.dimensions, self.num_items, 1),
                    dtype=self.dtype),
                4
            )
        }

        surrogate_distribution_dict["xi_a"] = self.bijectors['xi_a'](
            build_trainable_InverseGamma_dist(
                2*tf.ones(
                    (1, self.dimensions, 1, 1),
                    dtype=self.dtype),
                tf.ones(
                    (1, self.dimensions, 1, 1),
                    dtype=self.dtype),
                4
            )
        )

        surrogate_distribution_dict["eta_a"] = self.bijectors['eta_a'](
            build_trainable_InverseGamma_dist(
                2.0*tf.ones(
                    (1, 1, self.num_items, 1),
                    dtype=self.dtype),
                tf.ones(
                    (1, 1, self.num_items, 1),
                    dtype=self.dtype),
                4
            )
        )

        surrogate_distribution_dict["eta"] = self.bijectors['eta'](
            build_trainable_InverseGamma_dist(
                2.0*tf.ones((1, 1, self.num_items, 1), dtype=self.dtype),
                tf.ones((1, 1, self.num_items, 1), dtype=self.dtype),
                4
            )
        )

        self.joint_prior_distribution = tfd.JointDistributionNamed(
            grm_joint_distribution_dict)
        self.surrogate_distribution = tfd.JointDistributionNamed(
            surrogate_distribution_dict)

        self.surrogate_vars = self.surrogate_distribution.variables
        self.var_list = list(surrogate_distribution_dict.keys())
        self.set_calibration_expectations()

    def score(self, responses, samples=400):
        responses = tf.cast(responses, tf.int32)
        """Compute expections by importance sampling

        Arguments:
            responses {[type]} -- [description]

        Keyword Arguments:
            samples {int} -- Number of samples to use (default: {1000})
        """
        sampling_rv = tfd.Independent(
            tfd.Normal(
                loc=tf.reduce_mean(
                    self.calibrated_expectations['abilities'],
                    axis=0),
                scale=tf.math.reduce_std(
                    self.calibrated_expectations['abilities'],
                    axis=0)
            ),
            reinterpreted_batch_ndims=2
        )
        trait_samples = sampling_rv.sample(samples)

        sample_log_p = sampling_rv.log_prob(trait_samples)

        response_probs = self.grm_model_prob_d(
            abilities=trait_samples[..., tf.newaxis, tf.newaxis, :, :, :],
            discriminations=tf.expand_dims(
                self.surrogate_sample[
                    'discriminations'
                ],
                0),
            difficulties0=tf.expand_dims(
                self.surrogate_sample[
                    'difficulties0'
                ],
                0),
            ddifficulties=tf.expand_dims(
                self.surrogate_sample[
                    'ddifficulties'
                ],
                0)
        )

        response_probs = tf.reduce_mean(response_probs, axis=-4)

        response_rv = tfd.Independent(
            tfd.Categorical(
                probs=response_probs),
            reinterpreted_batch_ndims=1
        )
        lp = response_rv.log_prob(responses)
        l_w = lp[..., tf.newaxis] - sample_log_p[:, tf.newaxis, :]
        # l_w = l_w - tf.reduce_max(l_w, axis=0, keepdims=True)
        w = tf.math.exp(l_w)/tf.reduce_sum(
            tf.math.exp(l_w), axis=0, keepdims=True)
        mean = tf.reduce_sum(
            w*trait_samples[:, tf.newaxis, :, 0, 0],
            axis=0)
        mean2 = tf.math.reduce_sum(
            w*trait_samples[:, tf.newaxis, :, 0, 0]**2,
            axis=0)
        std = tf.sqrt(mean2-mean**2)
        return mean, std, w, trait_samples

    def loss(self, responses, scores):
        pass

    def unormalized_log_prob(self, data, **params):
        log_prior = self.joint_prior_distribution.log_prob(params)
        log_likelihood = self.log_likelihood(data, **params)
        return log_prior + log_likelihood


def main():
    pass


if __name__ == "__main__":
    main()
