import sounddevice as sd
import numpy as np
from queue import Queue
class AudioInput:
    def __init__(self, device, channels, samplerate, blocksize):
        self.device, self.channels, self.samplerate, self.blocksize = device, channels, samplerate, blocksize
        self.q = Queue(); self.stream = None
    def _callback(self, indata, frames, time, status):
        mono = np.mean(indata[:, :self.channels], axis=1).astype(np.float32)
        self.q.put(mono.copy())
    def start(self):
        self.stream = sd.InputStream(device=self.device, channels=self.channels, samplerate=self.samplerate, blocksize=self.blocksize, callback=self._callback)
        self.stream.start()
    def read(self, timeout=2.0): return self.q.get(timeout=timeout)
    def stop(self):
        if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
