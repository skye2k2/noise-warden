from __future__ import annotations
from datetime import datetime, time


DAY_START = time(hour=7, minute=0)
NIGHT_START = time(hour=22, minute=0)


def is_day(now: datetime) -> bool:
    t = now.time()
    return DAY_START <= t < NIGHT_START


def day_or_night(now: datetime) -> str:
    return "day" if is_day(now) else "night"
