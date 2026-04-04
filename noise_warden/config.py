from __future__ import annotations
import os
import yaml
from dataclasses import dataclass
from typing import Any, Dict

DEFAULT_CONFIG_PATH = "/opt/noise-warden/current/config/noise_warden.yaml"

class ConfigError(Exception):
    pass

def load_yaml(path: str | None = None) -> Dict[str, Any]:
    if path is None:
        path = os.environ.get("NOISE_WARDEN_CONFIG", DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError("Top-level YAML must be a mapping")
    validate_config(data)
    return data

def validate_config(cfg: Dict[str, Any]) -> None:
    required_sections = ["app", "audio", "detection", "response", "home_assistant", "plugins"]
    for s in required_sections:
        if s not in cfg or not isinstance(cfg[s], dict):
            raise ConfigError(f"Missing or invalid section: {s}")

    audio = cfg["audio"]
    detection = cfg["detection"]
    response = cfg["response"]

    def pos_num(v, name):
        try:
            if float(v) <= 0:
                raise ValueError()
        except Exception:
            raise ConfigError(f"{name} must be > 0")

    pos_num(audio.get("sample_rate"), "audio.sample_rate")
    pos_num(audio.get("block_seconds"), "audio.block_seconds")
    pos_num(audio.get("chunk_flush_seconds"), "audio.chunk_flush_seconds")

    for k in ["night_start_hour", "night_end_hour"]:
        v = detection.get(k)
        if not isinstance(v, int) or not (0 <= v <= 23):
            raise ConfigError(f"detection.{k} must be integer 0..23")

    if detection.get("mode") not in ["continuous", "intermittent", "continuous_music_focus"]:
        raise ConfigError("detection.mode invalid")

    if not isinstance(response.get("enable_daytime_response"), bool):
        raise ConfigError("response.enable_daytime_response must be bool")

def save_yaml_text_validated(path: str, raw_text: str) -> None:
    data = yaml.safe_load(raw_text)
    if not isinstance(data, dict):
        raise ConfigError("Config must parse to a YAML mapping")
    validate_config(data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw_text)
