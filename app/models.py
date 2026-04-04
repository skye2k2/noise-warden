from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any


@dataclass
class AudioFeatures:
    rms_db: float
    slow_db: float
    fast_db: float
    spectral_centroid_hz: float
    spectral_bandwidth_hz: float
    spectral_flatness: float
    spectral_flux: float
    bass_energy_ratio: float
    tonal_ratio: float


@dataclass
class ClassificationResult:
    is_above_threshold: bool
    is_impulse: bool
    is_intermittent_like: bool
    is_mower_like: bool
    is_music_like: bool
    is_bass_pulse_like: bool
    should_trigger: bool
    threshold_db: float
    mode: str
    notes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Incident:
    id: Optional[int]
    started_at: datetime
    ended_at: Optional[datetime]
    day_or_night: str
    peak_db: float
    threshold_db: float
    mode: str
    retaliated: bool
    notes_json: str
