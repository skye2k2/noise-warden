import numpy as np
from collections import deque
from pathlib import Path
import soundfile as sf
import os
from datetime import datetime

class AudioProcessor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sample_rate = cfg["audio"]["sample_rate"]
        self.pre_trigger_seconds = cfg["storage"]["pre_trigger_seconds"]
        self.ring = deque(maxlen=int(self.sample_rate * self.pre_trigger_seconds))
        self.alpha_slow = 0.2
        self.alpha_fast = 0.6
        self.slow = 0.0
        self.fast = 0.0

    def analyze_frame(self, frame):
        mono = frame.astype(np.float32).flatten()
        if mono.size == 0:
            return {"db_slow": 0.0, "db_fast": 0.0, "spectrum": np.zeros(16), "zcr": 0.0}
        self.ring.extend(mono.tolist())
        rms = max(np.sqrt(np.mean(mono**2)), 1e-8)
        db = 20 * np.log10(rms) + 94.0 + self.cfg["audio"]["mic_calibration_offset_db"]
        self.slow = (1 - self.alpha_slow) * self.slow + self.alpha_slow * db
        self.fast = (1 - self.alpha_fast) * self.fast + self.alpha_fast * db
        zc = np.mean(np.abs(np.diff(np.sign(mono)))) / 2.0 if mono.size > 2 else 0.0
        fft = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
        spectrum = fft[:256] if fft.size >= 256 else np.pad(fft, (0, max(0, 256 - fft.size)))
        return {
            "db_instant": float(db),
            "db_slow": float(self.slow),
            "db_fast": float(self.fast),
            "spectrum": spectrum,
            "zcr": float(zc),
        }

    def save_snippet(self, post_audio, snippet_dir: str):
        os.makedirs(snippet_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(Path(snippet_dir) / f"incident_{ts}.wav")
        pre = np.array(self.ring, dtype=np.float32)
        clip = np.concatenate([pre, post_audio.astype(np.float32).flatten()])
        sf.write(path, clip, self.sample_rate)
        return path
