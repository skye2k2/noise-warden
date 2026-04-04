from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from collections import deque
import numpy as np

from app.models import AudioFeatures, ClassificationResult
from app.utils.time_utils import is_day


@dataclass
class Thresholds:
    day_continuous_db: float
    night_continuous_db: float
    day_intermittent_db: float
    night_intermittent_db: float


class NoiseClassifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.thresholds = Thresholds(
            day_continuous_db=cfg.day_continuous_db,
            night_continuous_db=cfg.night_continuous_db,
            day_intermittent_db=cfg.day_intermittent_db,
            night_intermittent_db=cfg.night_intermittent_db,
        )
        self.history = deque(maxlen=256)

    def classify(self, feat: AudioFeatures, now: datetime) -> ClassificationResult:
        day = is_day(now)
        continuous_threshold = self.thresholds.day_continuous_db if day else self.thresholds.night_continuous_db
        intermittent_threshold = self.thresholds.day_intermittent_db if day else self.thresholds.night_intermittent_db

        # Impulse heuristic: fast significantly above slow and very short behavior
        is_impulse = (feat.fast_db - feat.slow_db) >= 6.0

        # Mower/weedwhacker heuristic:
        # strong energy in low-mid band + relatively tonal + low flatness + narrow-ish bandwidth
        is_mower_like = (
            feat.tonal_ratio >= 0.35
            and feat.spectral_flatness <= self.cfg.mower_max_flatness
            and feat.spectral_bandwidth_hz < 900
        )

        # Music-like heuristic:
        is_music_like = (
            feat.spectral_bandwidth_hz >= self.cfg.music_min_bandwidth_hz
            and feat.spectral_flux >= self.cfg.music_min_spectral_flux
            and feat.bass_energy_ratio >= 0.10
        )

        # Bass-pulse-like heuristic (simple deterministic proxy)
        is_bass_pulse_like = (
            feat.bass_energy_ratio >= 0.20
            and feat.spectral_centroid_hz < 350
            and feat.spectral_flux >= self.cfg.music_min_spectral_flux * 0.5
        )

        # Intermittent-like heuristic:
        # above intermittent threshold but not sustained as continuous yet, and music-like false
        is_above_cont = feat.slow_db >= continuous_threshold
        is_above_int = feat.slow_db >= intermittent_threshold
        is_intermittent_like = is_above_int and not is_above_cont and not is_music_like

        # Policy:
        # - ignore impulse
        # - suppress mower-like
        # - suppress likely intermittent passers
        # - prefer continuous or music/bass-like
        should_trigger = is_above_cont and (is_music_like or is_bass_pulse_like or not is_intermittent_like)

        if self.cfg.ignore_impulse_noise and is_impulse:
            should_trigger = False

        if is_mower_like:
            should_trigger = False

        # Strong suppression of likely drive-by/intermittent non-music
        if is_intermittent_like and not (is_music_like or is_bass_pulse_like):
            should_trigger = False

        mode = "continuous" if is_above_cont else ("intermittent" if is_above_int else "below")

        return ClassificationResult(
            is_above_threshold=is_above_cont or is_above_int,
            is_impulse=is_impulse,
            is_intermittent_like=is_intermittent_like,
            is_mower_like=is_mower_like,
            is_music_like=is_music_like,
            is_bass_pulse_like=is_bass_pulse_like,
            should_trigger=should_trigger,
            threshold_db=continuous_threshold if is_above_cont else intermittent_threshold,
            mode=mode,
            notes={
                "continuous_threshold": continuous_threshold,
                "intermittent_threshold": intermittent_threshold,
            },
        )
