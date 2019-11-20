import tensorflow as tf
import numpy as np
from autoencirt.nn import DenseNetwork, DenseHorseshoeNetwork


class IRTModel(object):
    response_type = None
    calibration_data = None
    num_people = None
    num_items = None
    response_data = None
    response_cardinality = None
    dimensions = 1
    weighted_likelihood = None
    calibrated_traits = None
    calibrated_traits_sd = None
    calibrated_discriminations = None
    calibrated_discriminations_sd = None
    calibrated_difficulties = None
    calibrated_likelihood_distribution = None
    bijectors = None

    scoring_network = None

    def __init__(self):
        pass

    def set_dimension(self, dim):
        self.dimensions = dim

    def load_data(self, response_data):
        self.response_data = response_data
        self.num_people = response_data.shape[0]
        self.num_items = response_data.shape[1]
        self.response_cardinality = int(max(response_data.max())) + 1
        if int(min(response_data.min())) == 1:
            print("Warning: responses do not appear to be from zero")
        self.calibration_data = tf.cast(response_data.to_numpy(), tf.int32)

    def create_distributions(self):
        pass

    def calibrate(self):
        pass

    def score(self, responses):
        pass

    def loss(self, responses):
        pass

    def obtain_scoring_nn(self, hidden_layers=None):
        if calibrated_traits is None:
            print("Please calibrate the IRT model first")
            return
        if hidden_layers is None:
            hidden_layers = [self.num_items*2, self.num_items*2]
        nn = DenseNetwork(
            self.num_items,
            [self.num_items] + hidden_layers + [self.dimensions]
            )