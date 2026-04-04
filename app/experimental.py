import numpy as np

class AdaptiveSubtraction:
    def __init__(self, learning_rate: float = 0.01):
        self.learning_rate = learning_rate
        self.coeff = 0.0
    def process(self, primary, reference):
        if reference is None or len(reference) == 0:
            return primary
        ref = reference[:len(primary)]
        self.coeff = (1 - self.learning_rate) * self.coeff + self.learning_rate * float(
            np.dot(primary, ref) / (np.dot(ref, ref) + 1e-9)
        )
        return primary - self.coeff * ref

class DualMicRejector:
    def __init__(self, directionality_bias: float = 0.25):
        self.directionality_bias = directionality_bias
    def process(self, primary, secondary):
        if secondary is None or len(secondary) == 0:
            return primary
        sec = secondary[:len(primary)]
        return primary - (1.0 - self.directionality_bias) * sec
