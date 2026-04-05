# noise-warden CHANGELOG

## v8 - 2026-04-05 — "Stop Hitting Yourself Retaliation Edition"

Response system overhaul with real GPIO relay control, self-noise suppression (don't log your own retaliation as a noise incident), non-blocking callback audio streams for future dual-mic support, ambient noise floor gate to skip DSP on white noise, and a batch of UI/operational polish.

<details>

<summary>Key details</summary>

### Noise floor gate

- **Configurable ambient noise floor** — new `detection.noise_floor_db` config key (default 50 dBA). Signals below this threshold skip the entire DSP pipeline (spectrum analysis, beat confidence, music classification, all exclusion filters). Reduces CPU load on the Pi and avoids processing blocks of data that could never approach an incident threshold. Set to 0 to disable. Configurable via calibration page UI or YAML; takes effect immediately (no restart needed)

### Response system

- **GPIO relay control re-integrated** — `RelayController` now drives a physical GPIO pin via `gpiozero.OutputDevice` when `_RELAY_HW_ENABLED = True` in `response.py`. Configurable `active_high` polarity and `amp_power_on_delay_sec` (default 1s) for amplifier PSU stabilization. Graceful fallback to boolean-only on non-Pi systems or when gpiozero is not installed
- **Self-noise suppression** — new `_start_response()` / `_stop_response()` / `_in_response_cooldown()` methods in the engine. While the system is playing a response (or within the `response_cooldown_sec` window afterward), incident detection is suppressed to prevent logging our own retaliation as a noise violation. State key `responding` added to StateStore for dashboard visibility
- **Centralized response lifecycle** — all relay.on/player.start/relay.off/player.stop calls consolidated into `_start_response()` and `_stop_response()` (was duplicated in 3+ call sites). `relay.cleanup()` releases GPIO on shutdown

### Audio capture

- **Non-blocking callback stream support** — `AudioCapture` now supports `sd.InputStream` with a callback that pushes blocks into a `queue.Queue`, selectable via `_CALLBACK_STREAMS_ENABLED = True` in `audio.py`. Same `read_block()` API — no caller changes needed. Enables concurrent multi-device capture for future dual-mic reference subtraction. Disabled by default; blocking `sd.rec()` remains the default mode
- **`AudioCapture.close()`** — new method releases the InputStream on shutdown
- **`reinitialize()` drains callback queue** — prevents stale blocks from a previous device error from polluting the next capture session

### Dual-microphone plugins

- **`ReferenceSubtractor`** — NLMS (Normalized Least Mean Squares) adaptive filter for echo cancellation between the reference mic (our speaker output) and the primary mic. 256-tap filter, mu=0.1, documented algorithm in `plugins.py`
- **`DualMicDifferential`** — spectral subtraction for directional noise rejection. Attenuates frequencies that are louder on the secondary mic (our side) than the primary mic (neighbor's side). Configurable aggressiveness via `alpha` parameter
- **Strong dual-mic recommendation** — README hardware section now prominently recommends two separate USB audio interfaces with two microphones for any deployment using the response feature. Single-mic setups work but rely solely on time-based cooldown for self-noise suppression

### Maintenance

- **StateStore consistent schema** — `disk_free_mb`, `disk_warning`, and `responding` now initialized in `StateStore._state` so `/api/state` always returns a complete shape
- **Dashboard disk warning pill** — low-disk situations now surface visually as a red "Disk" pill on the dashboard, instead of only logging to stdout
- **Dashboard visibility-pause polling** — `setInterval` polling now pauses via `document.visibilitychange` when the tab is backgrounded and resumes with an immediate fetch when foregrounded (minor Pi resource courtesy)
- **Timeline summary bar** — the pinned summary above the calendar now shows both incident count and total duration (e.g., "3 incidents · 12m 34s total")
- **`_looks_like_driveby` docstring cleaned up** — removed misleading "monotonically" wording and corrected inline comment to say "Count upticks (dB increases)"
- Test suite expanded to 221 tests (37 new: GPIO relay with mocked gpiozero, self-noise suppression lifecycle/cooldown, AudioCapture blocking/callback modes, NLMS adaptive filter convergence, spectral subtraction, noise floor gate, noise floor web route, plus prior session tests)

</details>

## v7 - 2026-04-04 — "Show Me the Evidence Edition"

Visual timeline redesign with offline-first architecture, plus a comprehensive deployment-hardening pass addressing silent failure modes, data integrity, and operational resilience. The primary use case is presenting cached noise incident data — including audio snippets — to authorities without network access, and having the system survive real-world Pi deployment conditions.

<details>

<summary>Key details</summary>

### Timeline

- **Visual calendar views** — day (single tall column, 40px/hr), week (7 columns), and month (compact colored pips). All view switching is client-side with zero network requests after initial page load
- **Severity-colored incident blocks** — amber (< 5 dB over), orange (5–10), red (10–15), deep red (> 15 dB over threshold)
- **Click-to-detail popup** — prominent excess dB badge (the hero element), ordinance threshold comparison, peak/avg readings, duration, classification, notes, and inline audio playback
- **All incident data embedded as JSON** — 30 days of incidents serialized into the page at render time. Detail popups, view switches, and navigation all read from this local dataset (zero extra database calls)
- **Server-side path scrubbing** — `snippet_path` is never sent to the client; only a `has_snippet` boolean is exposed

### Offline-first architecture

- **Service Worker** (`static/sw.js`) — network-first for the timeline page (normalized cache key strips query params so `?view=day` and `?view=week` share one entry), cache-first for audio snippets, pass-through for everything else
- **Snippet pre-caching** — on page load, fetches all snippets from the most recent 24 hours through the SW, which intercepts and caches each response
- **Offline status indicator** — `navigator.onLine` events update a status line; cached data remains fully navigable
- **Root-scoped `/sw.js` route** — SW is served from the application root (not `/static/`) so it can intercept `/timeline` and `/snippets/*` requests

### Database hardening

- **WAL mode enabled** — `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` in `Storage.__init__()`. Prevents blocking and corruption from concurrent engine writes + web reads on Pi
- **Schema migration framework** — `PRAGMA user_version` tracks the current schema version. `_run_migrations()` runs versioned migration blocks exactly once. Future column additions won't break existing databases
- **Query performance indexes** — `idx_incidents_start_ts` (DESC) and `idx_incidents_deleted` added to the schema. Timeline and incident list queries no longer full-scan on large datasets

### Audio resilience

- **Recording quality configurable** — default sample rate raised from 16,000 Hz to 22,050 Hz (wideband). Three options available via calibration page UI or config YAML: 22,050 Hz (wideband, ~2.6 MB/min), 44,100 Hz (CD quality, ~5.3 MB/min), 48,000 Hz (studio, ~5.8 MB/min). All DSP functions work at any rate — only playback fidelity changes. Server-side validation rejects invalid rates
- **Audio loop reconnection** — `PortAudioError` and `OSError` exceptions in the audio loop now reinitialize `AudioCapture` instead of spinning on a dead handle. 2-second backoff between retries
- **Mic device validation** — `AudioCapture.validate_device()` fingerprints the input device at startup (name + channels + sample rate). Periodic validation (daily) detects silent mic replacement or system default changes. Mismatch sets `mic_ok=False` with a descriptive error
- **Disk full graceful degradation** — `_append_audio()` catches write failures and disables recording for the active incident (dB monitoring continues). `_check_disk_quota()` proactively stops recording at 50 MB free (hard floor), before writes start failing. Partial WAVs are preserved
- **Day/night boundary split** — if an active incident crosses a day/night boundary, the engine finalizes the current segment and immediately begins a new incident with the updated threshold. Each segment carries its own period-specific threshold for accurate timeline display. If the noise drops below the new period's threshold, the old incident simply finalizes with no continuation
- **Max duration split** — `max_incident_record_hours` config (default 6) is now enforced. Long incidents are automatically split at the configured boundary, capping WAV file size and in-memory dB array growth. A new segment starts immediately if noise is still above threshold. Previously this config key was dead code

### Drive-by evidence safety

- **Quarantine, not delete** — `_finalize_incident()` now moves auto-dismissed drive-by snippets to `snippets/autodismissed/` instead of permanently deleting them. Files are preserved for manual review
- **Quarantine cleanup** — `cleanup_old_snippets()` purges `autodismissed/` files older than `retention_days`, same as regular snippets

### Operational improvements

- **Systemd unit hardened** — `TimeoutStopSec=15` (prevents hanging on shutdown), `Restart=on-failure` (not `always`), `RestartSec=10` (avoids tight crash loops), `StartLimitBurst=5` / `StartLimitIntervalSec=120` (rate-limits restarts)
- **Timezone validation** — new `detection.expected_timezone` config key (e.g., `America/Denver`). Engine startup checks the Pi's system timezone against the expected value and warns if they differ, since day/night threshold selection uses local time
- **DB backup guidance** — README now includes a recommended backup script and crontab entry

### Maintenance

- Test suite expanded to 184 tests (13 new: WAL mode, schema versioning, index existence, autodismissed cleanup, drive-by quarantine, disk-full recording stop, device validation, day/night boundary split, max duration split)

</details>

## v6 - 2026-04-04 — "Self-Aware Housekeeping Edition"

Security hardening, operational polish, and closing the loop on several long-standing paper cuts. The engine now cleans up after itself on startup (stale incidents, expired snippets, DB vacuum, disk quota), the dashboard refreshes itself, drive-bys get auto-dismissed, calibration profiles can be applied with one click, and recording can be toggled from the UI.

<details>

<summary>Key details</summary>

### Security

- **Player command injection fixed** — `PlaylistPlayer.start()` uses `shlex.split()` instead of `.split()` for safe tokenization of `player_command`
- **Snippet file handle leak fixed** — `FileResponse` replaces `StreamingResponse(open(...))` in the snippet serving route

### Engine improvements

- **Dashboard auto-refresh** — JS polls `/api/state` every 5 seconds and updates pill text in-place; zero database reads (reads from in-memory `StateStore`)
- **Disk quota warnings** — `_check_disk_quota()` runs at startup and daily; publishes `disk_free_mb` and `disk_warning` to state. Configurable via `disk_quota_warn_mb` (default 500 MB)
- **Exponentially-weighted avg dB** — `_finalize_incident()` uses decay factor 0.95 so sustained readings carry more weight than the onset ramp
- **Drive-by auto-dismiss** — `_looks_like_driveby()` checks: duration < `driveby_max_duration_sec` (default 30) AND tail portion shows fade-out pattern. Auto-soft-deletes matches and removes the snippet file to prevent orphaning
- **Stale incident repair** — `repair_stale_incidents()` runs at startup, finalizing any incidents left without an `end_ts` after a crash
- **DB vacuum** — `VACUUM` runs at startup to reclaim space from soft-deleted rows and fragmentation
- **Force-finalize skips drive-by** — `_finalize_incident(force=True)` (called during `engine.stop()`) no longer runs the drive-by filter, preventing false dismissal of active incidents during shutdown

### Web UI

- **Thresholds page restored** — `/thresholds` shows ordinance limits vs. active config, measurement notes, and ordinance footnotes; nav link added
- **Calibration "Apply" button** — each saved profile row has an Apply button that updates the running config and saves to YAML in one click
- **No-record mode toggle** — dashboard button toggles `recording_enabled` at runtime without a config file edit or restart

### Config

- New detection keys: `driveby_max_duration_sec`, `driveby_fade_tail_fraction`
- New audio key: `disk_quota_warn_mb`

### Maintenance

- Removed unused `snippets_dir` parameter from `cleanup_old_snippets()`
- Removed unused `rule_name` variable in engine loop
- Static analysis warnings resolved across all source and test files (unused variables, float equality in tests, etc.)
- Test suite expanded to 163 tests covering drive-by detection, disk quota, weighted avg dB, thresholds page, and calibration apply
- README deployment instructions rewritten: deployment architecture diagram, first-time install, iterative upgrade workflow, and rollback procedure

</details>

## v5 - 2026-04-04 — "Operational Cleanup Edition"

Bug fix round addressing 9 issues identified during v4 code review. Removed dead config keys, fixed PlaylistPlayer file selection, wired snippet cleanup schedule, adopted LAN-trust auth model, throttled MQTT publishing, fixed get_snippet query, restored Clear All Incidents, restored calibration wizard alongside manual instructions.

<details>

<summary>Key details</summary>

### Fixed

- **Dead config keys removed** — `sustain_blocks_required`, `release_blocks_required`, and `driveby_max_duration_sec` (which was dead code at the time) stripped from config
- **`PlaylistPlayer.start()` fixed** — now globs audio files from `playlist_dir` and passes a random selection to the player command
- **Snippet cleanup wired** — `cleanup_old_snippets()` called at engine startup and once daily via periodic timer
- **LAN-trust auth model** — GET pages are unauthenticated (browser-friendly); POST mutation endpoints require bearer token when configured
- **MQTT throttled** — `publish_state()` fires every ~5 seconds instead of every 0.5s loop (~12 msgs/min vs. ~120)
- **`get_snippet()` query fixed** — uses dedicated `Storage.get_incident(id)` instead of scanning the full incident table
- **Clear All Incidents restored** — `POST /incidents/clear` with `soft_delete_all_incidents()` and confirmation dialog
- **Calibration page restored** — wizard (compute form + saved profiles table) alongside manual calibration instructions

### Test infrastructure

- Full pytest test suite created: 142 tests across 8 test files
- `conftest.py` with `pytest_configure` hook for early `NOISE_WARDEN_CONFIG` bootstrapping (solving `web.py` module-level side effects)
- `config.py` `load_yaml()` reads `NOISE_WARDEN_CONFIG` env var at call time (not import time)
- `python-multipart` added as runtime dependency (required by FastAPI form parsing)

</details>

## v4 - 2026-04-04 — "Secure-er Ordinance-Anchored Evidence Edition"

Package renamed from `app` to `noise_warden`. Ordinance thresholds are now embedded and authoritative. Thread-safe state, config validation, MQTT Home Assistant integration, WAV chunk-to-disk recording (no unbounded RAM), `/api/health`, pause/resume controls, incident notes in UI, timeline date filtering, bearer token auth, dedicated `noisewarden` service user, and snippet retention cleanup.

<details>

<summary>Key details</summary>

### Architecture

- Python package renamed: `app/` → `noise_warden/`
- Entry point changed: `python -m app.main` → `uvicorn noise_warden.main:app`
- New modules:
  - `noise_warden/state.py` — `StateStore` with `threading.Lock` for thread-safe state snapshots
  - `noise_warden/ordinance.py` — Pleasant Grove ordinance thresholds embedded as structured data; `applicable_threshold()` returns rule name + dB limit based on zone, mode, and time of day
  - `noise_warden/ha.py` — `HAClient` with real MQTT publish via `paho-mqtt` (state + event topics)
  - `noise_warden/plugins.py` — `ReferenceSubtractor` and `DualMicDifferential` placeholder classes for future adaptive filter work
- `noise_warden/config.py` — `validate_config()` enforces required sections, positive numerics, valid detection mode, and boolean types at load time; `save_yaml_text_validated()` validates before writing
- Service file moved from `services/` to `deploy/`

### Added

- **Thread-safe `StateStore`** — `threading.Lock` protects all reads and writes; `snapshot()` returns a `deepcopy` for safe cross-thread access
- **Config validation** — `validate_config()` checks required sections, numeric ranges, detection mode enum, and boolean types. `save_yaml_text_validated()` validates YAML before writing to disk; invalid config returns an error message via query param redirect
- **`/api/health` endpoint** — returns engine thread liveness, mic status, last error, and full state snapshot
- **Pause / Resume controls** on dashboard — `POST /control/pause` and `POST /control/resume` toggle `state.armed`; engine loop skips processing when not armed
- **Incident notes in UI** — each incident row has an inline textarea + "Save Notes" button that POSTs to `/incidents/{id}/notes`
- **Timeline date filtering** — day/week/month links pass `?view=` query param; `since_for_view()` computes UTC cutoff; `list_incidents(since=...)` filters in SQL
- **Bearer token authentication** — `app.auth_token` config field; `must_auth()` checks `Authorization: Bearer` header on all mutation and page endpoints; blank token disables auth (LAN-only mode)
- **Photo upload file-type validation** — server-side extension check rejects files not ending in `.jpg`, `.jpeg`, `.png`, `.webp`
- **WAV chunk-to-disk recording** — incident audio written to `soundfile.SoundFile` in append mode during capture; no unbounded RAM accumulation. Pre-roll blocks written at incident start.
- **`recording_enabled` config toggle** — allows completely disabling snippet recording for limited-space scenarios
- **Snippet retention cleanup** — `Storage.cleanup_old_snippets()` removes WAV files older than `retention_days`
- **Home Assistant MQTT integration** — `HAClient` connects via `paho-mqtt`; publishes state (retained) and events (non-retained) to configurable topic prefix. Supports username/password auth.
- **`home_assistant` config section** — `enabled`, `mode` (mqtt/rest_stub), host, port, topic prefix, credentials
- **`plugins` config section** — feature flags for `enable_reference_subtraction` and `enable_dual_mic_diff`
- **Dedicated `noisewarden` service user** — install script creates system user with `audio` + `gpio` group membership; service file runs as `noisewarden:noisewarden`
- **Config path via environment variable** — `NOISE_WARDEN_CONFIG` env var overrides default path
- **`soundfile` dependency re-added** — used for chunk-to-disk WAV writing (more capable than stdlib `wave` for append mode)
- **`paho-mqtt` dependency added**
- **`python-multipart` dependency removed** (FastAPI handles form parsing)
- **Ordinance data structure** in `ordinance.py` — full Pleasant Grove residential/agricultural thresholds, measurement guidance (mic placement, A-weighting, slow/fast response), and legal notes embedded as a dict
- **Detection mode** — config supports `continuous`, `intermittent`, and `continuous_music_focus`; threshold lookup adapts per mode
- **Richer exclusion filter config** — `rain_low_variance_db`, `mower_centroid_min_hz`, `mower_centroid_max_hz` replace previous single-threshold approach
- **`dba_estimate()` function** — replaces raw `+ offset` in engine; named for clarity
- **Engine error handling** — `try/except` around entire loop body; on exception sets `state.mic_ok=False`, `last_error`, `mode="error"`, sleeps 1s and retries
- **`spectrum_features` returns 3-band breakdown** — `lowband_ratio` (30–180 Hz), `midband_ratio` (180–1200 Hz), `highband_ratio` (>1200 Hz) in addition to centroid and flatness
- **Pagination on incidents page** — `list_incidents(limit=50, offset=...)` with page navigation links
- **Incident count** — `Storage.count_incidents()` for pagination math
- **`static/` and `static/build/` directories** created by `web.py` at startup via `os.makedirs(..., exist_ok=True)`
- **Flash-style messages** — config save, pause/resume, delete actions pass `?msg=` query params rendered in templates
- **Mobile-friendly touch targets** — button padding increased to `12px 16px`
- **`viewport` meta tag** — `<meta name="viewport" content="width=device-width, initial-scale=1">` added to `base.html`

### Changed

- **`beat_confidence_from_history()`** rewritten — now uses autocorrelation across lag windows (2–8 blocks) instead of mean absolute dB delta. Still heuristic, but measures periodicity rather than just volatility.
- **Mower filter** — now uses configurable `centroid_min_hz` / `centroid_max_hz` (default 300–3000) and requires `env_std <= 3.5` over last 12 readings. Tighter than v3's `centroid > 500` single threshold.
- **Rain filter** — added `rain_low_variance_db` check (dB standard deviation ≤ threshold over recent history) in addition to flatness. More precise than flatness alone.
- **Thunder filter** — raised `lowband_ratio` threshold from 0.45 (implicit) to explicit config at 0.55; also checks `flatness > 0.45`
- **`music_like_score()`** — now accepts features dict instead of raw block + sample rate; formula documented inline as "strong low-band energy + not-too-flat spectrum"
- **Low-band analysis range** expanded from 20–120 Hz (v2) / 20–250 Hz (v3) to 30–180 Hz (aligns with typical bass music fundamentals)
- **`calibration_offset_db` default** changed from 100.0 to 88.0
- **Config structure** — `detection` section replaces combined `thresholds` + `filters` from v3; all filter params moved under `detection`
- **Storage `conn()`** now uses `contextmanager` with explicit `commit()` and `close()` pattern
- **`finalize_incident()`** — dedicated `Storage` method replaces raw SQL through `conn()` in engine
- **Snippet audio** served via `/snippets/{incident_id}` route (stream from path) instead of static file mount
- **`RelayController`** — `gpiozero` import removed entirely; now a simple boolean flag class (placeholder for real GPIO wiring)
- **`PlaylistPlayer`** — no longer passes filename separately; just runs the player command. Switches back to `cvlc` from `ffplay`.
- **Install script** — creates `noisewarden` user, sets ownership, uses `sudo cp` for service file
- **Deploy script** — simplified; no longer creates venv from scratch on upgrade (reuses existing)
- **Service file** — runs as `User=noisewarden`, uses `uvicorn` directly, sets `NOISE_WARDEN_CONFIG` env var
- **UI theme** — reverted from dark theme to light (white background, white cards, dark nav)
- **Thresholds page removed** — ordinance reference now displayed on dashboard directly

### Removed

- `app/` package directory (entire tree) — replaced by `noise_warden/`
- `services/` directory — service file moved to `deploy/`
- `templates/thresholds.html` — ordinance info moved to dashboard
- `gpiozero` dependency — relay is now a flag-only placeholder
- `scipy` dependency — FFT now uses `numpy.fft` exclusively
- Calibration profiles table and wizard computation — replaced by simpler manual calibration instructions
- `db_history` capping via list slice — now capped to 240 entries (was 20 in v3)
- `python-multipart` dependency

### Known issues & technical debt

- **`RelayController` is a no-op** — `on()` and `off()` just toggle a boolean flag. No actual GPIO control. Needs `gpiozero` (or equivalent) integration to function.
- **Snippet serving loads into memory via `StreamingResponse(open(..., "rb"))`** — for large WAV files (multi-hour incidents), this holds a file handle open for the duration of the HTTP response. Not a major risk at home-network scale, but not ideal.
- **`cleanup_old_snippets()` is defined but never called** — the retention cleanup method exists but no scheduled invocation (e.g., periodic task or startup sweep) triggers it.
- **`driveby_max_duration_sec` config key still dead** — declared in config but no drive-by filter exists in `dsp.py`
- **`sustain_blocks_required` and `release_blocks_required` config keys unused** — declared in detection config but never referenced in `engine.py`; incident start/end is controlled by single-block threshold crossing + `song_gap_merge_sec` gap timer
- **Auth is header-only (Bearer token)** — web UI forms don't send `Authorization` headers; browser page loads will fail auth if `auth_token` is set. Auth currently only works for API consumers, not the HTML UI. Need cookie/session-based auth for browser access, or bypass auth for GET pages.
- **`PlaylistPlayer.start()` runs player command without the playlist file** — `self.proc = subprocess.Popen(args)` runs `cvlc --play-and-exit --no-video` with no file argument. The player will start and immediately exit (or wait for stdin). The playlist directory is stored but never used to select a file.
- **No "Clear All Incidents" button** — v3 had it with confirmation; v4 removed it without replacement
- **MQTT publishes on every engine loop iteration** — `publish_state()` called every 0.5s block. At low QoS this floods the broker with ~120 msgs/min for no benefit. Should throttle to every N seconds.
- **Plugin classes are pure stubs** — `ReferenceSubtractor.process()` and `DualMicDifferential.process()` return the primary block unchanged and are never called from the engine

</details>

## v3 - 2026-04-03 — "Redeployable Edition"

Major architectural flattening, new DSP pipeline with music-likeness scoring and beat confidence, calibration wizard, symlink-based deployment model, and server-side rendering throughout. Sample rate dropped from 48kHz to 16kHz.

<details>

<summary>Key details</summary>

### Architecture

Flattened from nested packages to flat modules:
- `app/audio/` (5 files) → `app/audio.py` — blocking `sd.rec()` capture with block-level prebuffer deque
- `app/audio/metrics.py` + `app/audio/exclusion.py` → `app/dsp.py` — unified DSP: `calibrated_db()`, `spectral_features()`, `music_like_score()`, `beat_confidence()`, `classify_noise()`
- `app/core/` (3 files) → `app/config.py` + `app/storage.py`
- `app/engine/` (2 files) → `app/engine.py`
- `app/hardware/` (2 files) → `app/response.py`
- `app/web/server.py` → `app/web.py`
- `app/web/templates/` → top-level `templates/`
- `app/web/static/app.js` → inline JS in `templates/base.html`

### Deployment model

- Versions extracted under `/opt/noise-warden/` (e.g., `/opt/noise-warden/noise-warden-v2_2/`)
- Active version selected by symlink: `/opt/noise-warden/current → ./noise-warden-v2_2`
- Persistent data in `/opt/noise-warden/shared/` (DB, snippets, build info, playlist)
- Shared venv at `/opt/noise-warden/venv/`
- Upgrade helper: `/opt/noise-warden/deploy_noise_warden.sh <version-dir>`
- First install: `install_pi.sh` creates shared dirs, venv, symlink, systemd service
- Future upgrade: `deploy_noise_warden.sh` stops service, re-symlinks, reinstalls deps, restarts

### Added

- **Calibration wizard** — new `/calibration` page computes offset from reference SPL vs. observed dBFS; profiles stored in new `calibration_profiles` DB table
- **`music_like_score()` function** — weighted composite of low-band energy ratio and spectral flatness proximity to 0.35; incidents now require minimum score to trigger (configurable `min_music_like_score`)
- **`beat_confidence()` function** — mean absolute dB delta across recent history, normalized; incidents require minimum beat confidence (configurable `min_beat_confidence`)
- **`detection` config block** — `min_music_like_score` and `min_beat_confidence` thresholds
- **Dedicated incidents page** (`/incidents`) — full-detail table with start_db, peak_db, avg_db, threshold, music score, beat confidence, classification, mode, merge count, audio playback
- **Soft delete for incidents** — `deleted INTEGER DEFAULT 0` flag instead of hard `DELETE FROM`
- **Richer incident schema** — added `duration_sec`, `start_db`, `threshold_db`, `music_like_score`, `beat_confidence`, `responded`, `merge_count` columns
- **Ordinance excerpt storage** — separate text field on build page for pasting local ordinance reference
- **`engine.stop()` method** — joins thread, stops player, turns off relay
- **Graceful `gpiozero` import** — `RelayController` degrades to no-op if `gpiozero` unavailable (development on non-Pi)
- **`deploy_noise_warden.sh`** — upgrade helper script for symlink-based version switching
- **`paths` config block** — explicit paths for shared_root, db, snippets, build artifacts
- **Confirmation dialogs** on destructive actions (Clear All, Delete incident)
- **`calibrated_db()` function** — applies offset inside DSP module instead of raw `+ 100.0` in engine

### Changed

- **Audio capture** — from callback-based `sd.InputStream` (non-blocking) to blocking `sd.rec()` + `sd.wait()`; prebuffer is now a block-level deque instead of sample-level ring buffer
- **Sample rate** dropped from 48kHz to 16kHz; block size from 1.0s to 0.5s
- **All pages server-side rendered** — dashboard, incidents, timeline render via Jinja2 instead of client-side JS fetch + DOM manipulation; only live state uses JS polling
- **Build info storage** — from singleton SQLite row to flat files (`build_notes.txt`, `build_photo.jpg`, `ordinance_excerpt.txt`) in shared directory
- **Build photo serving** — copies to `static/build/build_photo.jpg` on upload for reliable static serving
- **Exclusion filters simplified** — from dedicated `ExclusionEngine` class with 8-frame deque history to inline `classify_noise()` using single-frame delta + spectral features
- **Mower filter** — changed from band-energy + multi-frame envelope stability to `flatness >= threshold AND centroid > 500`
- **Night mode detection** — from string time parsing (`"22:00"`) to integer hours (`night_start_hour: 22`)
- **Thresholds** — day 55 dB / night 50 dB (from 60/55 in v2)
- **Install script** — now enforces `/opt/noise-warden/` base path, creates shared directories, uses `sudo cp` for systemd service (fixes v2 `sudo sed` permission bug)
- **Service file** — uses fixed paths (`/opt/noise-warden/current`, `/opt/noise-warden/venv`); depends on `network-online.target`
- **CSV export** — now uses `StreamingResponse` instead of writing a temp file
- **Dashboard** — shows recent 20 incidents server-side; removed arm/disarm buttons (engine is always running)

### Removed

- `soundfile` dependency — WAV writing now uses stdlib `wave` module
- `requests` dependency (was unused)
- `app/web/static/app.js` — JS inlined in `base.html`
- `app/audio/subtraction.py` — secondary mic / reference input / adaptive subtraction scaffolding removed entirely
- `app/audio/ringbuffer.py` — replaced by block-level deque in `AudioCapture`
- `RuntimeState` dataclass — replaced by plain dict `engine.state`
- Arm/disarm controls — engine is always active; no manual arm/disarm toggle
- `ExclusionEngine` class — replaced by `classify_noise()` function
- `thunder_taper_window_sec` dead config key
- `snippet_post_seconds` dead config key
- Drive-by exclusion filter (code removed; `enable_driveby_reject` config key persists but is dead)

### Known issues & technical debt

- `engine.stop()` defined but never wired to FastAPI shutdown lifecycle
- Service runs as `User=root` — unnecessary privilege; should run as a dedicated user
- `driveby_max_duration_sec` config key is dead (filter code removed)
- Event audio frames accumulate unbounded in RAM for duration of incident
- `engine.py` directly calls `self.storage.conn()` to update snippet_path, bypassing `Storage` encapsulation
- `static/` and `static/build/` directories not created by install script — first build photo upload may fail
- `music_like_score` and `beat_confidence` use undocumented magic numbers with no validation methodology
- `beat_confidence` measures dB volatility, not rhythmic periodicity
- Mower filter thresholds are broader than v2; may cause more false rejections
- Blocking `sd.rec()` prevents future multi-mic support without architectural change

</details>

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
