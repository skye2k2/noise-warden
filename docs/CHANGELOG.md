# noise-warden CHANGELOG

## v2 - 2026-04-03 — "Live Audio + GPIO Integration Edition"

Semi-production-ready release with live audio capture, GPIO relay control, and 5 false-positive exclusion filters. Deployable on a Pi, but operators need hands-on calibration and local ordinance knowledge to set thresholds correctly.

<details>

<summary>Key details</summary>

### Architecture

Major restructure into proper Python packages:
- `app/audio/` — capture, metrics, exclusion filters, ring buffer, adaptive subtraction
- `app/core/` — config loader, SQLite logging, time utilities
- `app/engine/` — state machine controller, runtime state
- `app/hardware/` — GPIO relay control, audio playback
- `app/web/` — FastAPI server, static assets, templates

### Added

- **Live audio capture** via `sounddevice` — actual mic input replaces the fake/stub frames from v1
- **5 false-positive exclusion filters**, each with configurable parameters:
  - Impulse detection (sudden peak > 18dB delta)
  - Thunder-like (high-bass impulse with specific envelope shape)
  - Rain-like (high spectral flatness + low energy spread)
  - Mower-like (300–3000 Hz stable envelope)
  - Drive-by (specific decaying envelope pattern)
- **GPIO relay control** now functional via `gpiozero` (configurable pin, active-high/low)
- **Build documentation page** — photo upload + annotation textarea for documenting physical setup
- **Base template** (`base.html`) — shared dark navigation bar across all pages
- **Hysteresis-based state machine** — prevents chatter at threshold boundary (configurable `hysteresis_db`)
- **Minimum event duration enforcement** (15 sec default) — prevents trivially short incidents
- **Config hot-edit via web UI** — YAML config saved and reloaded without SSH
- `scipy` added back for FFT and signal processing
- `requests` library added (for future HA integration)
- `site` config block with `city_name` and `ordinance_reference` fields
- `filters` config block for all exclusion filter tuning
- `response` config block separating playback behavior from classification

### Changed

- **Audio playback switched from VLC to ffplay** — simpler subprocess model, no VLC dependency
- **Ring buffer** refactored into dedicated `ringbuffer.py` with cleaner pre/post-event capture
- **Metrics computation** separated from capture (dedicated `metrics.py`)
- **Install script** (`install_pi.sh`) now creates additional directories: `data/snippets/`, `data/uploads/`, `media/playlist/`, `logs/`
- **Configuration structure** reorganized: `rules` block replaces `classification`/`thresholds` combination from v1
- Ordinance thresholds lowered to 60 dBA day / 55 dBA night (from 65/55) for residential continuous
- Evaluation uses 1-second audio blocks (up from 100ms frames in v1)

### Removed

- VLC dependency (`python-vlc`) — replaced by `ffplay`
- `pydantic` / `pydantic-settings` — config validation now handled by custom YAML loader
- MQTT publisher scaffolding (removed entirely)
- Pydantic `ControlRequest` schema model
- `experimental.py` module — adaptive subtraction moved to `app/audio/subtraction.py`

### Known issues & technical debt

- Some filter parameters declared in config but unused in code (`driveby_max_duration_sec`, `thunder_taper_window_sec`)
- Secondary mic differential is naive (hardcoded 0.7× subtraction, no adaptive gain or phase alignment)
- Config hot-reload accepts invalid YAML without validation — bad input crashes the app
- Home Assistant integration still stubbed (`ha_status` field in state exists but is never written to)
- No timestamp sync / NTP validation
- dB readings use a `+ 100.0` offset to normalize to ~0–100 range (undocumented assumption)
- Relay control has no debounce; rapid arm/disarm could toggle GPIO unpredictably
- Build info is singleton in DB (only one photo/notes record allowed)
- Audio input failures silently skip frames (`except Exception: sleep; continue`)

</details>

---

## v1 - 2026-04-02 — "Hardened Deployment Edition"

Major architectural rewrite with multi-page web UI, WAV snippet capture, incident merging, and experimental noise rejection scaffolds. HOWEVER, comma, the core audio capture was regressed to stub/fake data, making this version non-functional for actual monitoring.

<details>

<summary>Key details</summary>

### Architecture

Flattened from v0's subpackage structure to a single `app/` directory with dedicated modules:
- `audio.py` — AudioProcessor with ring buffer concept
- `classifier.py` — DeterministicClassifier with heuristic rules
- `config.py` — YAML loader/saver
- `db.py` — SQLite incident CRUD (9 functions)
- `engine.py` — ~300-line core event loop
- `experimental.py` — Adaptive subtraction + dual-mic rejection (feature-flagged)
- `integrations.py` — Home Assistant monitor + MQTT publisher stubs
- `ordinance.py` — Day/night threshold lookup
- `playback.py` — VLC subprocess wrapper
- `schemas.py` — Pydantic ControlRequest model
- `state.py` — RuntimeState dataclass

### Added

- **WAV snippet capture** — pre-trigger (8 sec ring buffer) + post-trigger audio saved per incident
- **Incident merging** — events separated by ≤10 sec (configurable `merge_gap_seconds`) bundled into single incident (handles music with song gaps)
- **5-page web UI** (up from single-page):
  - Dashboard — live status + incident table + control buttons
  - Timeline — day/week/month incident viewer with filter cards
  - Thresholds — JSON display of current thresholds vs. city ordinance limits
  - Config editor — editable JSON textarea with save (requires service restart)
  - Calibration — step-by-step SPL meter calibration instructions
- **Experimental adaptive subtraction** — LMS filter learning a reference signal (disabled by default)
- **Experimental dual-mic differential rejection** — weighted subtraction with directionality bias (disabled by default)
- **MQTT publisher scaffolding** (instantiated but `publish_status()` is a no-op)
- **HARDWARE_BOM.md** — component shopping list
- **LocalStorage caching** in web UI — dashboard shows stale data if controller temporarily unreachable
- **Expanded configuration** — ~140+ YAML options covering audio, GPIO, playback, classification, ordinance, thresholds, integrations, and experimental feature flags
- **Install script** now deploys to `/opt/noise-warden-v2` with full rsync, venv creation, and systemd service installation
- Dual-mode dB metering (slow α=0.2, fast α=0.6 exponential smoothing)
- FFT-based spectrum analysis (256-bin resolution)
- Event classification labels: continuous, intermittent, impulse, music, vehicle/tool suppression
- Per-incident audio playback in web UI (`<audio controls>`)
- Playback suppression window (`post_playback_suppress_seconds: 12`) to prevent false positives from own audio
- Night record-only mode with separate ordinance thresholds

### Changed

- **Config structure** massively expanded from v0's ~40 options to 140+
- **Web UI** upgraded from single-page to multi-page with dedicated routes
- **Install path** changed from user-relative to system-level (`/opt/noise-warden-v2`)
- **Audio processing** changed from 125ms blocks to 100ms frames
- **State machine** incident lifecycle: hold ≥20s + gap below release threshold (4dB margin, ≥12s)
- Systemd service now targets `/opt` installation path

### Removed

- `app/audio/` subpackage (a_weighting.py, features.py, input.py) — consolidated into single `audio.py`
- `app/api.py` — API routes moved into `main.py`
- `app/models.py` — replaced by `schemas.py` (Pydantic)
- `app/storage.py` — replaced by `db.py`
- `app/webapp.py` — web serving merged into `main.py`
- `app/utils/time_utils.py` — inlined
- `docs/ARCHITECTURE.md` — removed (architecture now implicit in code structure)
- `docs/CHECKLIST.md` — removed (setup steps moved to README / calibration page)
- A-weighting IIR bilinear filter — replaced by simpler EMA dB smoothing
- CSV state log endpoint — removed (state transitions no longer logged separately)
- Bearer token authentication — removed from API

### Known issues & technical debt

- **Audio capture is STUBBED** — `_fake_audio_frame()` generates random noise; real `sounddevice` imported but unused in core loop
- Home Assistant integration is hollow (`poll()` only sets state to UNKNOWN)
- MQTT publisher is a complete no-op
- GPIO relay config flag exists but relay never triggered by engine
- Threading without locks on shared `RuntimeState` (race condition risk between engine thread and API endpoints)
- Config reload requires full service restart
- CSV export loads ALL incidents every time (no pagination)
- Spectrum analysis hardcoded to 256-bin FFT (~187 Hz/bin resolution)
- No rate limiting on API endpoints

</details>

---

## v0 - 2026-04-01 — "Ordinance-Aware Skeleton"

Initial proof-of-concept with a fully-planned architecture and working basic pipeline: audio capture, A-weighted measurement, spectral classification, incident logging, GPIO relay + VLC playback, REST API, single-page web UI, and Home Assistant integration.

<details>

<summary>Key details</summary>

### Architecture

Modular pipeline design:
```
Microphone → USB Audio → A-Weighting (IIR) → Feature Extraction (spectral)
  → Deterministic Classifier → Incident State Machine
  → SQLite Log ↔ REST API ↔ Web UI + Home Assistant
  → GPIO Relay + VLC Playback
```

Processing: 125ms blocks at 48 kHz (~6,144 samples per iteration).

### Core modules

- `app/audio/a_weighting.py` — IIR bilinear transform A-weighting approximation
- `app/audio/features.py` — spectral centroid, bandwidth, flatness, flux, bass energy isolation (30–180 Hz)
- `app/audio/input.py` — `sounddevice` capture with device enumeration
- `app/classifier.py` — Deterministic heuristic classifier (mower/weedwhacker tonal detection, music/bass-pulse classification, intermittent event suppression, impulse rejection)
- `app/engine.py` — Core state machine with trigger accumulator, day/night policy enforcement
- `app/state.py` — RuntimeState dataclass (armed, manual_kill, response_active, dB readings)
- `app/storage.py` — SQLite with dual tables: incidents + state transitions
- `app/api.py` — FastAPI REST endpoints
- `app/webapp.py` — Web UI routing
- `app/playback.py` — VLC playlist playback + GPIO relay control
- `app/models.py` — Data models
- `app/config.py` — YAML config loader

### Features implemented

- Continuous directional audio capture with USB device enumeration
- A-weighted sound level measurement (IIR bilinear filter)
- SLOW (0.15 EMA) and FAST (0.45 EMA) envelope tracking (mirrors ANSI SPL meter behavior)
- Spectral feature extraction: centroid, bandwidth, flatness, flux
- Bass energy isolation (30–180 Hz, configurable)
- Mower/weedwhacker tonal detection (80–500 Hz energy + flatness heuristics)
- Music/bass-pulse classification (spectral flux + centroid + bandwidth)
- Intermittent event suppression (filters transient noise sources)
- Impulse noise rejection (fast >6 dB above slow reading)
- Threshold-based incident logging (start, peak tracking, end)
- SQLite persistence with dual logging: incidents + state transitions
- GPIO relay control with power-on/off timing delays
- VLC playlist-based playback with self-playback suppression (mute while playing + cooldown)
- Bearer token authentication for REST modification endpoints
- CSV export of incident history
- Day/night behavioral split:
  - **DAY (7 AM–10 PM):** Full enforcement with configurable response activation
  - **NIGHT (10 PM–7 AM):** Recording only, no response
- Web UI — single-page, dark theme, 4-column responsive grid:
  - Status card (armed/kill/response toggles, live dB readouts, classification label)
  - Incidents card (table with 50 most recent, CSV export, clear log)
  - State log card (50 most recent state transitions)
  - Calibration notes card (step-by-step tuning instructions)
- Auto-refresh every 3 seconds via `setInterval`
- REST API endpoints: `/api/status`, `/api/incidents`, `/api/state-log`, `/api/arm`, `/api/disarm`, `/api/kill`, `/api/kill/clear`, `/api/incidents/export`
- Home Assistant REST integration (arm/disarm/kill commands + status sensor polling)
- Systemd service definition
- Install script (apt deps + venv + pip)

### Configuration (~40 options)

- `[app]` — host, port, log_level
- `[audio]` — device name, sample rate, channels, block size, calibration offset, suppress detection while playing, post-stop cooldown
- `[classification]` — day/night dB thresholds (continuous, intermittent, impulse), trigger mode, persistence seconds, clear-below seconds, intermittent duty cycle filtering, music detection (spectral flux, bandwidth), bass pulse rate detection, mower tonal detection (band, flatness, energy ratio)
- `[playback]` — enabled (default false), player, playlist path, GPIO pin, amp power delays, max play minutes
- `[logging]` — SQLite path, retention days
- `[web]` — static/template directories
- `[home_assistant]` — enabled, API token

### Ordinance alignment (my city, UT)

| Category | Day (7 AM–10 PM) | Night (10 PM–7 AM) |
|----------|-------------------|---------------------|
| Continuous | 65 dBA | 55 dBA |
| Intermittent | 70 dBA | 60 dBA |
| Impulse | 75 dBA (FAST) | 60 dBA (FAST) |

### Documentation included

- **ARCHITECTURE.md** — ASCII pipeline flowchart, feature extraction outputs, classifier decision tree, state machine flow
- **CHECKLIST.md** — 6-phase deployment guide (bench setup, install, calibration, false-positive elimination, safe activation, long-run hardening)
- **README.md** — Comprehensive project README with purpose, ordinance basis, policy choices, hardware BOM, software stack, fast-start guide, legal disclaimers

### Dependencies

`fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `numpy`, `scipy`, `sounddevice`, `soundfile`, `PyYAML`, `gpiozero`, `python-vlc`, `pydantic`, `pydantic-settings`

### Known issues & technical debt

- Trigger accumulator decay is asymmetric (ramps up at 1× but decays at 0.5×) — causes "stickiness"; undocumented whether intentional
- Bass band (30–180 Hz) and mower band (80–500 Hz) overlap without deduplication; can cause false triggers on low rumble
- Spectral flux uninitialized on first block (first 125ms always has 0 flux, muting music detection at startup)
- No thread safety on `RuntimeState` (GIL mitigates in practice for simple reads)
- A-weighting IIR has hardcoded frequencies, not validated against ANSI standard; error magnitude undocumented
- SQLite not in WAL mode (can block on concurrent reads during writes)
- VLC player object not cleaned up if VLC crashes (resource leak risk)
- No minimum block accumulation rearm delay (can cause rapid on/off cycles)
- `min_event_seconds` config parameter declared but never used in engine logic
- GPIO pin not validated at startup (errors only appear when playback attempts)
- No audio file recording (only metadata logged, no WAV dumps)
- No persistence of armed/disarmed state across reboots
- No adaptive calibration (offset is config-only, no auto-learning)
- Error handling light — audio/GPIO failures caught with bare `except:` or `try/pass`
- Logging sparse — minimal debug/info traces for troubleshooting

</details>
