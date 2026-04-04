from pathlib import Path
import yaml
CONFIG_PATH = Path("config/noise_warden.yaml")
class Config:
    def __init__(self, raw): self.raw = raw
    @classmethod
    def load(cls):
        return cls(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")))
    def get(self, *keys, default=None):
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur: return default
            cur = cur[k]
        return cur
