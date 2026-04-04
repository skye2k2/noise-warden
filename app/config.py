import yaml
class AppConfig:
    def __init__(self, raw): self.raw = raw
    @classmethod
    def load(cls, path):
        with open(path, 'r', encoding='utf-8') as f:
            return cls(yaml.safe_load(f))
    def __getitem__(self, k): return self.raw[k]
    @property
    def paths(self): return self.raw['paths']
