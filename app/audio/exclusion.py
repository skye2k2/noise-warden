from collections import deque
from dataclasses import dataclass
from app.audio.metrics import spectral_features
@dataclass
class ExclusionDecision:
    excluded: bool
    label: str | None
class ExclusionEngine:
    def __init__(self, cfg, sr):
        self.cfg, self.sr = cfg, sr
        self.history_db = deque(maxlen=8)
    def decide(self, frame, db):
        f = spectral_features(frame, self.sr)
        self.history_db.append(db)
        if self.cfg.get("exclude_impulse", True) and len(self.history_db) >= 2:
            if db - list(self.history_db)[-2] >= self.cfg.get("impulse_peak_delta_db", 18.0):
                return ExclusionDecision(True, "impulse")
        if self.cfg.get("exclude_thunder_like", True) and len(self.history_db) >= 4:
            seq = list(self.history_db)
            if db - min(seq[:-1]) >= self.cfg.get("thunder_impulse_delta_db", 22.0) and f["low_ratio"] > 0.45:
                if seq[-1] <= seq[-2] + 2 and seq[-2] <= seq[-3] + 4:
                    return ExclusionDecision(True, "thunder_like")
        if self.cfg.get("exclude_rain_like", True):
            if f["flatness"] >= self.cfg.get("rain_flatness_threshold", 0.65) and f["spread"] <= self.cfg.get("rain_band_energy_spread_threshold", 0.75):
                return ExclusionDecision(True, "rain_like")
        if self.cfg.get("exclude_mower_like", True) and len(self.history_db) >= 4:
            recent = list(self.history_db)[-4:]
            if 300 <= f["centroid"] <= 3000 and 0.2 <= f["flatness"] <= 0.8 and max(recent) - min(recent) < 8:
                return ExclusionDecision(True, "mower_like")
        if self.cfg.get("exclude_driveby", True) and len(self.history_db) >= 3:
            r = list(self.history_db)[-3:]
            if r[-1] < r[-2] and r[-2] > r[-3] and (r[-2] - r[-1]) > 4:
                return ExclusionDecision(True, "driveby_like")
        return ExclusionDecision(False, None)
