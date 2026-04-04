from datetime import datetime
class OrdinanceRules:
    def __init__(self, cfg: dict):
        self.cfg = cfg
    def is_night(self, now: datetime) -> bool:
        hhmm = now.strftime("%H:%M")
        return hhmm >= self.cfg["ordinance"]["night_start"] or hhmm < self.cfg["ordinance"]["day_start"]
    def thresholds_for_now(self, now: datetime) -> dict:
        res = self.cfg["ordinance"]["residential"]
        night = self.is_night(now)
        return {
            "continuous": res["continuous_night_dba"] if night else res["continuous_day_dba"],
            "intermittent": res["intermittent_night_dba"] if night else res["intermittent_day_dba"],
            "impulse": res["impulse_night_dba"] if night else res["impulse_day_dba"],
            "is_night": night,
        }
