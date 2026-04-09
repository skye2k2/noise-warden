"""
Shared test fixtures for noise-warden.

Provides reusable config dicts, temp-dir Storage instances, and StateStore
objects so every test gets a fresh, isolated environment with no disk or
hardware dependencies.

This pytest_configure hook is the key trick — it writes a temp YAML config and sets NOISE_WARDEN_CONFIG before web.py's module-level load_yaml() runs during collection. This avoids needing to refactor web.py's module-level imports.
"""
import os
import tempfile

import pytest
import yaml

from noise_warden.storage import Storage
from noise_warden.state import StateStore


# ---------------------------------------------------------------------------
# Session-scoped bootstrap — sets NOISE_WARDEN_CONFIG before web.py import
# ---------------------------------------------------------------------------
# web.py calls load_yaml() at module scope on first import, reading from
# NOISE_WARDEN_CONFIG (default: /opt/noise-warden/...). We need a valid
# config file on disk BEFORE that import happens. This session fixture
# writes one to a temp dir and sets the env var so all tests can import
# noise_warden.web without FileNotFoundError.
# ---------------------------------------------------------------------------

def _minimal_cfg_dict(tmp_dir):
    """Build a minimal valid config dict with paths in tmp_dir."""
    shared = os.path.join(tmp_dir, "shared")
    os.makedirs(shared, exist_ok=True)
    return {
        "app": {
            "base_dir": tmp_dir,
            "shared_dir": shared,
            "static_dir": os.path.join(tmp_dir, "static"),
            "host": "0.0.0.0",
            "port": 8787,
            "auth_token": "",
        },
        "audio": {
            "sample_rate": 22050,
            "block_seconds": 1.0,
            "input_device": None,
            "input_channels": 1,
            "recording_enabled": True,
            "snippet_pre_seconds": 2,
            "snippet_post_seconds": 8,
            "chunk_flush_seconds": 30,
            "max_incident_record_hours": 6,
            "retention_days": 30,
        },
        "detection": {
            "zone": "residential_agricultural",
            "mode": "continuous_music_focus",
            "noise_floor_db": 50.0,
            "calibration_offset_db": 88.0,
            "night_start_hour": 22,
            "night_end_hour": 7,
            "song_gap_merge_sec": 12,
            "min_music_like_score": 0.62,
            "min_beat_confidence": 0.38,
            "impulse_peak_delta_db": 14.0,
            "thunder_peak_delta_db": 18.0,
            "rain_flatness_threshold": 0.72,
            "rain_low_variance_db": 2.5,
            "mower_flatness_threshold": 0.25,
            "mower_centroid_min_hz": 300,
            "mower_centroid_max_hz": 4000,
            "mower_env_std_max": 4.5,
            "holdover_min_run": 5,
            "holdover_max_gap": 12,
            "allow_response_night": False,
        },
        "response": {
            "enable_daytime_response": False,
            "relay_gpio_pin": 18,
            "relay_active_high": True,
            "amp_power_on_delay_sec": 0.0,
            "response_cooldown_sec": 5.0,
            "player_command": "/usr/bin/cvlc --play-and-exit --no-video",
            "playlist_dir": os.path.join(tmp_dir, "playlist"),
        },
        "home_assistant": {
            "enabled": False,
            "mode": "mqtt",
            "mqtt_host": "127.0.0.1",
            "mqtt_port": 1883,
            "mqtt_topic_prefix": "noise_warden",
            "mqtt_username": "",
            "mqtt_password": "",
        },
        "plugins": {
            "enable_reference_subtraction": False,
            "enable_dual_mic_diff": False,
        },
    }


# This must run before ANY test imports noise_warden.web.
# pytest_configure runs before collection, so it's early enough.
_session_tmp_dir = None

def pytest_configure(config):
    """Write a valid YAML config to a temp file and set NOISE_WARDEN_CONFIG.

    This runs before test collection, so when noise_warden.web gets imported
    (triggering load_yaml() at module scope), it finds a valid config file.
    """
    global _session_tmp_dir
    _session_tmp_dir = tempfile.mkdtemp(prefix="nw_test_")
    cfg = _minimal_cfg_dict(_session_tmp_dir)
    cfg_path = os.path.join(_session_tmp_dir, "noise_warden.yaml")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    os.environ["NOISE_WARDEN_CONFIG"] = cfg_path

def pytest_unconfigure(config):
    os.environ.pop("NOISE_WARDEN_CONFIG", None)


# ---------------------------------------------------------------------------
# Minimal valid config dict — mirrors config/noise_warden.yaml structure
# ---------------------------------------------------------------------------

@pytest.fixture
def base_cfg(tmp_path):
    """
    Return a minimal valid config dict that satisfies validate_config().
    Paths point into pytest's tmp_path so nothing touches real disk.
    """
    shared = str(tmp_path / "shared")
    os.makedirs(shared, exist_ok=True)
    return {
        "app": {
            "base_dir": str(tmp_path),
            "shared_dir": shared,
            "static_dir": str(tmp_path / "static"),
            "host": "0.0.0.0",
            "port": 8787,
            "auth_token": "",
        },
        "audio": {
            "sample_rate": 22050,
            "block_seconds": 1.0,
            "input_device": None,
            "input_channels": 1,
            "recording_enabled": True,
            "snippet_pre_seconds": 2,
            "snippet_post_seconds": 8,
            "chunk_flush_seconds": 30,
            "max_incident_record_hours": 6,
            "retention_days": 30,
        },
        "detection": {
            "zone": "residential_agricultural",
            "mode": "continuous_music_focus",
            "noise_floor_db": 50.0,
            "calibration_offset_db": 88.0,
            "night_start_hour": 22,
            "night_end_hour": 7,
            "song_gap_merge_sec": 12,
            "min_music_like_score": 0.62,
            "min_beat_confidence": 0.38,
            "impulse_peak_delta_db": 14.0,
            "thunder_peak_delta_db": 18.0,
            "rain_flatness_threshold": 0.72,
            "rain_low_variance_db": 2.5,
            "mower_flatness_threshold": 0.25,
            "mower_centroid_min_hz": 300,
            "mower_centroid_max_hz": 4000,
            "mower_env_std_max": 4.5,
            "holdover_min_run": 5,
            "holdover_max_gap": 12,
            "allow_response_night": False,
        },
        "response": {
            "enable_daytime_response": False,
            "relay_gpio_pin": 18,
            "relay_active_high": True,
            "amp_power_on_delay_sec": 0.0,
            "response_cooldown_sec": 5.0,
            "player_command": "/usr/bin/cvlc --play-and-exit --no-video",
            "playlist_dir": str(tmp_path / "playlist"),
        },
        "home_assistant": {
            "enabled": False,
            "mode": "mqtt",
            "mqtt_host": "127.0.0.1",
            "mqtt_port": 1883,
            "mqtt_topic_prefix": "noise_warden",
            "mqtt_username": "",
            "mqtt_password": "",
        },
        "plugins": {
            "enable_reference_subtraction": False,
            "enable_dual_mic_diff": False,
        },
    }


@pytest.fixture
def tmp_storage(tmp_path):
    """Provide a fresh Storage backed by a throwaway SQLite DB."""
    db_path = str(tmp_path / "test.db")
    return Storage(db_path)


@pytest.fixture
def tmp_state():
    """Provide a fresh StateStore."""
    return StateStore()


# ---------------------------------------------------------------------------
# Sample incident dict — satisfies Storage.create_incident() columns
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_incident():
    """Return a dict suitable for Storage.create_incident()."""
    return {
        "start_ts": "2026-04-01T12:00:00+00:00",
        "start_db": 72.5,
        "peak_db": 72.5,
        "avg_db": 72.5,
        "threshold_db": 65.0,
        "music_like_score": 0.78,
        "beat_confidence": 0.45,
        "classification": "music_like",
        "mode": "respond",
        "responded": 0,
        "merge_count": 0,
        "snippet_path": None,
        "notes": "",
    }
