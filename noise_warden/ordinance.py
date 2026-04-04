from __future__ import annotations
from datetime import datetime

ORDINANCE = {
    "city": "Pleasant Grove, UT",
    "section": "5-2B-1 Public Disturbances",
    "measurement": {
        "day_start": 7,
        "night_start": 22,
        "mic": "Property line or 50 ft from source; >=5 ft from wall (or +5 dB variance if impossible); >=3 ft above ground",
        "weighting": "A-weighting",
        "response_continuous": "slow",
        "response_impulse": "fast",
    },
    "residential_agricultural": {
        "continuous_A2_A3": {"day": 65, "night": 55},
        "intermittent_A2_A3": {"day": 70, "night": 60},
        "impulse_A1_A3": {"day": 75, "night": 60},
        "commerce_industry_A1": {"day": 85, "night": 55},
    },
    "notes": [
        "Sound level measurements are not required if other evidence/testimony establishes disturbance.",
        "Amplified music is listed under public disturbance noises (A2.c).",
        "Emergency activities/vehicles are exempt.",
    ]
}

def is_night(now: datetime, night_start=22, night_end=7) -> bool:
    h = now.hour
    return h >= night_start or h < night_end

def applicable_threshold(cfg: dict, now: datetime) -> tuple[str, float]:
    zone = cfg["detection"].get("zone", "residential_agricultural")
    mode = cfg["detection"].get("mode", "continuous_music_focus")
    night = is_night(now, cfg["detection"]["night_start_hour"], cfg["detection"]["night_end_hour"])
    z = ORDINANCE.get(zone, ORDINANCE["residential_agricultural"])
    period = "night" if night else "day"
    if mode == "intermittent":
        return ("intermittent_A2_A3", float(z["intermittent_A2_A3"][period]))
    return ("continuous_A2_A3", float(z["continuous_A2_A3"][period]))
