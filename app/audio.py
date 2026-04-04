import numpy as np
import sounddevice as sd
from collections import deque
class AudioCapture:
    def __init__(self, sample_rate, channels, block_sec, prebuffer_sec):
        self.sample_rate = sample_rate; self.channels = channels; self.block_sec = block_sec
        self.frames = int(sample_rate * block_sec)
        self.prebuffer = deque(maxlen=max(1, int(prebuffer_sec / block_sec)))
    def read_block(self):
        data = sd.rec(self.frames, samplerate=self.sample_rate, channels=self.channels, dtype='float32')
        sd.wait()
        mono = data[:,0] if data.ndim > 1 else data
        self.prebuffer.append(mono.copy())
        return mono
    def get_prebuffer(self):
        return np.concatenate(list(self.prebuffer)) if self.prebuffer else np.array([], dtype=np.float32)
