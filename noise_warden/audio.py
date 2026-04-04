from __future__ import annotations
import sounddevice as sd
import numpy as np
from collections import deque

class AudioCapture:
    def __init__(self, sample_rate=16000, block_seconds=0.5, channels=1, device=None):
        self.sr = sample_rate
        self.block_seconds = block_seconds
        self.frames = int(sample_rate * block_seconds)
        self.channels = channels
        self.device = device
        self.pre_blocks = deque(maxlen=64)

    def read_block(self):
        data = sd.rec(self.frames, samplerate=self.sr, channels=self.channels, dtype="float32", device=self.device)
        sd.wait()
        arr = np.squeeze(data)
        self.pre_blocks.append(arr.copy())
        return arr

    def get_preroll(self, seconds: float):
        blocks = int(seconds / self.block_seconds)
        return list(self.pre_blocks)[-blocks:]
