from __future__ import annotations
import os
import yaml
from dataclasses import dataclass
from typing import Any, Dict

_OPT_CONFIG_PATH = "/opt/noise-warden/current/config/noise_warden.yaml"

# pyproject.toml is the single source of truth for the release version (the
# Python analog of package.json). It lives at the project root, one level above
# this package directory — true for both local dev and the Pi deploy
# (/opt/noise-warden/current/pyproject.toml).
_PYPROJECT_PATH = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
_cached_version: str | None = None


def get_app_version() -> str:
    """Return the release version from pyproject.toml (kept in sync with the
    CHANGELOG). Read directly from the file rather than importlib.metadata so a
    version bump is reflected immediately without reinstalling the package
    (editable installs and the Pi deploy don't refresh installed metadata).
    Cached after first read. Falls back to importlib.metadata, then '?', so a
    missing/unreadable pyproject never breaks page rendering."""
    global _cached_version
    if _cached_version is not None:
        return _cached_version

    version = None
    try:
        import tomllib  # Python 3.11+ (matches requires-python)
        with open(_PYPROJECT_PATH, "rb") as f:
            version = tomllib.load(f).get("project", {}).get("version")
    except (OSError, ValueError, KeyError):
        version = None

    if not version:
        try:
            from importlib.metadata import version as _meta_version
            version = _meta_version("noise-warden")
        except Exception:
            version = None

    _cached_version = version or "?"
    return _cached_version


# When running locally (no NOISE_WARDEN_CONFIG env var and /opt path absent),
# fall back to config/noise_warden_local.yaml relative to the project root so
# `uvicorn noise_warden.web:app` works without any extra setup.
_LOCAL_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "noise_warden_local.yaml"
)

def _default_config_path() -> str:
    """Resolve the config path: env var → /opt deploy path → local dev fallback."""
    env = os.environ.get("NOISE_WARDEN_CONFIG")
    if env:
        return env
    if os.path.exists(_OPT_CONFIG_PATH):
        return _OPT_CONFIG_PATH
    return _LOCAL_CONFIG_PATH

def resolve_snippet_path(stored_path: str | None, snippets_dir: str) -> str | None:
    """Resolve a stored snippet_path to an actual file in the current environment.

    The DB stores absolute snippet paths from whatever machine recorded the
    incident (typically the Pi's /opt/noise-warden/shared/snippets/...). When
    the same database is opened on a different machine — e.g. a developer copy
    using ./local_data/snippets/ — those absolute paths no longer exist. The
    *filename* is the stable identity; the directory is environment-specific.

    Resolution order:
      1. The stored path itself, if it exists (deployed Pi / matching machine).
      2. {snippets_dir}/{basename} (the configured snippets directory).
      3. {snippets_dir}/autodismissed/{basename} (quarantined snippets).

    Returns the first existing path, or None if the file cannot be found
    anywhere. Callers should treat None as "no playable snippet".
    """
    if not stored_path:
        return None
    if os.path.exists(stored_path):
        return stored_path
    basename = os.path.basename(stored_path)
    for candidate in (
        os.path.join(snippets_dir, basename),
        os.path.join(snippets_dir, "autodismissed", basename),
    ):
        if os.path.exists(candidate):
            return candidate
    return None

class ConfigError(Exception):
    pass

def load_yaml(path: str | None = None) -> Dict[str, Any]:
    if path is None:
        path = _default_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"Config file not found: {path}\n\n"
            "For local development, create a local config override:\n"
            "  cp config/noise_warden.yaml config/noise_warden_local.yaml\n"
            "  # Edit app.shared_dir to ./local_data, etc.\n"
            "Or point directly to any config file:\n"
            "  export NOISE_WARDEN_CONFIG=/path/to/noise_warden.yaml"
        ) from None
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
