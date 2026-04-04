from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import yaml


@dataclass
class AppConfig:
    host: str
    port: int
    log_level: str


@dataclass
class AudioConfig:
    input_device_name: str
    sample_rate: int
    channels: int
    block_seconds: float
    calibration_offset_db: float
    suppress_detection_while_playing: bool
    suppress_after_stop_seconds: float


@dataclass
class ClassificationConfig:
    day_continuous_db: float
    night_continuous_db: float
    day_intermittent_db: float
    night_intermittent_db: float
    ignore_impulse_noise: bool
    night_record_only: bool
    trigger_mode: str
    trigger_persist_seconds: float
    clear_below_seconds: float
    min_event_seconds: float
    intermittent_max_on_cycle: float
    intermittent_max_continuous_seconds: float
    music_min_spectral_flux: float
    music_min_bandwidth_hz: float
    bass_pulse_min_rate_hz: float
    bass_pulse_max_rate_hz: float
    bass_band_low_hz: float
    bass_band_high_hz: float
    mower_tonal_band_low_hz: float
    mower_tonal_band_high_hz: float
    mower_max_flatness: float
    mower_min_tonal_ratio: float


@dataclass
class PlaybackConfig:
    enabled: bool
    player: str
    playlist_path: str
    amp_gpio_pin: int
    amp_power_on_delay_ms: int
    amp_power_off_delay_ms: int
    max_play_minutes: int


@dataclass
class LoggingConfig:
    db_path: str
    retention_days: int


@dataclass
class WebConfig:
    static_dir: str
    templates_dir: str


@dataclass
class HAConfig:
    enabled: bool
    api_token: str


@dataclass
class Settings:
    app: AppConfig
    audio: AudioConfig
    classification: ClassificationConfig
    playback: PlaybackConfig
    logging: LoggingConfig
    web: WebConfig
    home_assistant: HAConfig


def load_settings(path: str = "config.yaml") -> Settings:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {path}. Copy config.example.yaml to config.yaml")

    raw = yaml.safe_load(p.read_text(encoding="utf-8"))

    return Settings(
        app=AppConfig(**raw["app"]),
        audio=AudioConfig(**raw["audio"]),
        classification=ClassificationConfig(**raw["classification"]),
        playback=PlaybackConfig(**raw["playback"]),
        logging=LoggingConfig(**raw["logging"]),
        web=WebConfig(**raw["web"]),
        home_assistant=HAConfig(**raw["home_assistant"]),
    )
