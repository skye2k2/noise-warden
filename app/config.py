from pathlib import Path
import yaml
CONFIG_PATH = Path("config.yaml")
EXAMPLE_PATH = Path("config.example.yaml")
def load_config():
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
def save_config(payload: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
