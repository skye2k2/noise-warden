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
        # Capture a fingerprint of the initial device for drift detection
        self._device_fingerprint = self._get_device_fingerprint()

    def _get_device_fingerprint(self):
        """Return a string identifying the current input device (name + channels + sample rate).
        Used to detect when a microphone is swapped or the system default changes."""
        try:
            info = sd.query_devices(self.device, kind="input")
            return f"{info['name']}|{info['max_input_channels']}|{info['default_samplerate']}"
        except (sd.PortAudioError, ValueError):
            return None

    def validate_device(self):
        """Check that the current audio input device matches the one present at startup.
        Returns (ok, message) tuple. A mismatch likely means the mic was replaced or
        unplugged and a different device picked up as system default."""
        current = self._get_device_fingerprint()
        if self._device_fingerprint is None:
            # No baseline — can't validate, but capture a new one if available
            if current:
                self._device_fingerprint = current
            return (True, "no baseline")

        if current is None:
            return (False, "audio device not found")

        if current != self._device_fingerprint:
            return (False, f"device changed: was [{self._device_fingerprint}], now [{current}]")

        return (True, "ok")

    def reinitialize(self):
        """Re-query the audio subsystem after a device error. Clears pre-roll buffer
        and updates the device fingerprint. Call this after a transient USB disconnect."""
        self.pre_blocks.clear()
        self._device_fingerprint = self._get_device_fingerprint()

    def read_block(self):
        data = sd.rec(self.frames, samplerate=self.sr, channels=self.channels, dtype="float32", device=self.device)
        sd.wait()
        arr = np.squeeze(data)
        self.pre_blocks.append(arr.copy())
        return arr

    def get_preroll(self, seconds: float):
        blocks = int(seconds / self.block_seconds)
        return list(self.pre_blocks)[-blocks:]
