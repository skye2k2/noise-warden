import numpy as np
from collections import deque

class DeterministicClassifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.on_window = deque(maxlen=max(1, int(cfg["thresholds"]["intermittent_window_seconds"] / max(0.1, cfg["audio"]["frame_ms"]/1000))))

    def _band_energy_ratio(self, spectrum, low_bin, high_bin):
        total = np.sum(spectrum) + 1e-9
        band = np.sum(spectrum[low_bin:high_bin])
        return float(band / total)

    def classify(self, metrics: dict, active_thresholds: dict):
        db_slow = metrics["db_slow"]
        db_fast = metrics["db_fast"]
        spectrum = metrics["spectrum"]

        above_cont = db_slow >= active_thresholds["continuous"]
        self.on_window.append(1 if above_cont else 0)
        on_cycle = float(sum(self.on_window) / len(self.on_window)) if self.on_window else 0.0

        is_impulse = db_fast >= active_thresholds["impulse"] and db_slow < active_thresholds["continuous"]
        if self.cfg["classification"]["suppress_impulse"] and is_impulse:
            return {"label": "suppressed_impulse", "confidence": 0.9, "trigger": False}

        if self.cfg["classification"]["suppress_intermittent_vehicle_like"]:
            if db_slow >= active_thresholds["intermittent"] and on_cycle <= self.cfg["thresholds"]["intermittent_on_cycle_threshold"]:
                return {"label": "suppressed_driveby_like", "confidence": 0.7, "trigger": False}

        ratio_weed = self._band_energy_ratio(
            spectrum,
            self.cfg["classification"]["suppress_weedwhacker_hz_low"] // 10,
            self.cfg["classification"]["suppress_weedwhacker_hz_high"] // 10
        )
        if ratio_weed >= self.cfg["classification"]["suppress_weedwhacker_tonality_ratio"]:
            return {"label": "suppressed_tool_like", "confidence": ratio_weed, "trigger": False}

        music_band_ratio = self._band_energy_ratio(spectrum, 4, 120)
        if db_slow >= active_thresholds["continuous"] and music_band_ratio >= self.cfg["classification"]["min_music_band_ratio"]:
            return {"label": "music_like_continuous", "confidence": music_band_ratio, "trigger": True}

        if db_slow >= active_thresholds["continuous"]:
            return {"label": "continuous_noise", "confidence": 0.6, "trigger": True}

        return {"label": "below_threshold", "confidence": 0.2, "trigger": False}
