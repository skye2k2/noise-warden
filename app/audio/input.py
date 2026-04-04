from __future__ import annotations
import queue
import sounddevice as sd
import numpy as np


class AudioInput:
    def __init__(self, device_name: str, sample_rate: int, channels: int, block_seconds: float):
        self.device_name = device_name
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_seconds = block_seconds
        self.blocksize = int(sample_rate * block_seconds)
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self.stream = None

    def _resolve_device(self):
        devices = sd.query_devices()
        for idx, dev in enumerate(devices):
            if self.device_name.lower() in dev["name"].lower() and dev["max_input_channels"] >= self.channels:
                return idx
        raise RuntimeError(f"Input device containing '{self.device_name}' not found")

    def _callback(self, indata, frames, time_info, status):
        if status:
            pass
        mono = indata[:, 0].copy()
        try:
            self.q.put_nowait(mono)
        except queue.Full:
            # drop oldest by one
            try:
                _ = self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(mono)
            except queue.Full:
                pass

    def start(self):
        device_idx = self._resolve_device()
        self.stream = sd.InputStream(
            device=device_idx,
            samplerate=self.sample_rate,
            channels=self.channels,
            blocksize=self.blocksize,
            dtype="float32",
            callback=self._callback,
        )
        self.stream.start()

    def read_block(self, timeout: float = 1.0):
        return self.q.get(timeout=timeout)

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
