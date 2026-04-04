from collections import deque
import numpy as np
class AudioRingBuffer:
    def __init__(self, max_seconds, sr):
        self.buf = deque(maxlen=max_seconds * sr)
    def push(self, samples):
        for s in samples.astype(float).tolist(): self.buf.append(s)
    def get(self):
        return np.array(self.buf, dtype=np.float32) if self.buf else np.zeros(0, dtype=np.float32)
