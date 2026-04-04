# noise-warden

Pi-deployable nuisance noise incident logging, management, and reporting tool (web interface for both access and configuration)

<details>

<summary>Why?</summary>

Well, to make a very long story short, sometimes people choose to be loud and obnoxious. And everyone else just gets to suffer, despite there regularly being city noise ordinances, HOA codes of conduct, and apartment tenant agreements that are supposed to protect us all.

Now, let's be clear, I don't mean the occasional Super Bowl party. I mean people who care so little about how they impact others' lives that they will run air compressors and play rap music at 11pm or 6am or whatever suits them without a thought for the newborn their neighbor just got to sleep, the neighborhood children who have school the next day, or the graveyard shift you just finished.

Yes. All of these are examples from my life, each of them in different cities with different neighbors. Our current neighbor takes the cake, though. He stuck an amplified music system in his garage, and started playing frankly terrible music for _hours_ at a time, sending the bass vibrating through my house, night and day. We begged each to be reasonable humans, but each refused. We had begged each jerk in the past to be reasonable humans, but each refused, and this piece was no exception. Recently, after finally giving in and calling the city hotline to report a noise disturbance, only to have my neighbor turn his music back down to still-technically-violating-the-noise-ordinance-but-quiet-enough-no-one-can-reasonably-complain levels, I decided to get quantifiable irrefutable proof on my side. Something that I could show on my phone to a patrolman or take into city council and have them instantly see and understand the recent violations and impact. Especially because for me, I was able to program in the exact city noise ordinance as the trigger threshold.

</details>

> **Important:** This project is designed for **evidence collection and automation experiments**. It is **not a certified ANSI Type 1/2 sound level meter** and should **not** be relied upon as the sole proof of a legal violation. Your city code, like mine, may explicitly say sound level measurements are *desirable but not required* if other evidence/testimony establishes a disturbance. Use this as a logging/correlation tool, not a courtroom laser cannon.

## How it works

```
Microphone → USB Audio → Metric Computation → Exclusion Filters
  → Threshold Evaluation (day/night ordinance-aware)
  → Incident State Machine (hysteresis + song-gap merging)
  → SQLite Incident Log + WAV Snippet Capture
  → Web Dashboard ↔ REST API
  → (Optional) GPIO Relay + Audio Playback Retaliation
```

The system runs a continuous background thread that monitors audio in 1-second blocks against configurable ordinance-aligned thresholds. It distinguishes between genuine nuisance noise and common false positives (impulses, thunder, rain, lawn equipment, drive-bys), merges closely-spaced events into single incidents, and captures WAV snippet evidence with pre-trigger audio buffering.

During **daytime** (7 AM–10 PM), the system can optionally activate a GPIO relay to power an amplifier and play audio in response. During **nighttime** (10 PM–7 AM), it records incidents only — no response activation.

## Ordinance basis (my city, UT residential)

| Category | Day (7 AM–10 PM) | Night (10 PM–7 AM) |
|----------|-------------------|---------------------|
| Continuous | 65 dBA (SLOW) | 55 dBA (SLOW) |
| Intermittent | 70 dBA (SLOW) | 60 dBA (SLOW) |
| Impulse | 75 dBA (FAST) | 60 dBA (FAST) |

These are the defaults. Configuration supports any jurisdiction's thresholds.

## Setup

### Requirements

- A directional (shotgun/cardioid/super cardioid) USB microphone
- Pi 3 or better device and power supply with a minimum of:
  - 90% availability on two cores at 1GHz+ (process needs to be immediately responsive)
  - 3GB available RAM (only needed when tracking extremely long-running incidents)
  - However much storage space you want to dedicate to evidence recording

  > I highly recommend a Pi 4+, as the increased CPU and RAM speed helps cut down on processing time, which in turn actually keeps the overhead of the system low.

### Hardware BOM

#### Core

- **Raspberry Pi 4B (4 GB)** or **Raspberry Pi 5** — Pi 4 is sufficient; Pi 5 is snappier
- **Official Pi PSU**
- **32–128 GB microSD** (A2-rated) or SSD boot

#### Audio input

- **USB audio interface** — class-compliant, stable Linux support (e.g., UGREEN / Sabrent USB dongle). Avoid the Pi's onboard analog jack.
- **Directional microphone** — Budget: Boya BY-MM1 shotgun. Better: RØDE VideoMic GO II.
- **Windscreen / deadcat** — critical for outdoor or window-adjacent placement
- Optional: secondary mic (for dual-mic differential rejection testing), reference loopback capture path (for adaptive subtraction)

#### Audio output / response (optional)

- **Class D amp board** (e.g., TPA3116/TPA3118)
- **12V power supply** for amp
- **Passive speaker(s)** — outdoor-rated or indoor window-facing
- **5V opto-isolated 1-channel relay module** — controls amp power. Prefer switching amp enable/remote turn-on rather than mains AC.

#### Optional upgrades

- I2S MEMS mic (INMP441) for cleaner digital chain
- Calibrated SPL meter for one-time calibration cross-check
- UPS HAT or USB UPS for power continuity

### Installation

1. Clone this repository on your Pi (or download, transfer, and extract an archive)
2. Run the install script:
    ```bash
    cd noise-warden
    ./scripts/install_pi.sh
    ```
    This will: install system dependencies (`python3-venv`, `portaudio19-dev`, `libsndfile1`, `ffmpeg`), create a Python venv, install pip dependencies, create data directories, and install a systemd service.
3. Edit the configuration:
    ```bash
    nano config/noise_warden.yaml
    ```
4. Enable and start the service:
    ```bash
    sudo systemctl enable --now noise-warden
    ```
5. Open in browser: `http://<pi-ip>:8787/`

## Configuration (IMPORTANT!)

> NOTE: It is extremely important that you take at least a little time to configure, calibrate, and test your setup. Every microphone, environment, and personal irritation threshold is different.

After running the install script and starting the service, it is time to calibrate your system.

Configuration is stored in `config/noise_warden.yaml` and can also be edited via the web UI at `/config`. Key sections:

| Section | Purpose |
|---------|---------|
| `site` | City name and ordinance reference |
| `audio` | Sample rate, block size, device selection, mic calibration offset, secondary/reference mic toggles, snippet pre/post buffer durations |
| `gpio` | Relay pin, active-high/low, enable toggle |
| `response` | Daytime audio response toggle, playlist directory, player command, amp power-on delay |
| `rules` | Day/night schedule, dB thresholds, evaluation interval, release/merge/minimum duration timings, hysteresis |
| `filters` | Per-filter enable toggles and tuning parameters for impulse, drive-by, mower-like, thunder-like, and rain-like exclusions |
| `web` | Host, port, dashboard refresh interval |

## Features

- **Intelligent gating, recording, and logging of incidents** — hysteresis-based state machine prevents chatter at threshold boundaries; song-gap merging (default 10 sec) bundles closely-spaced noise events into single incidents
- **WAV snippet evidence capture** — ring buffer preserves pre-trigger audio (15 sec default) plus post-trigger recording, saved per incident
- **5 false-positive exclusion filters** — impulse, thunder-like, rain-like, mower-like, and drive-by patterns each with configurable tuning parameters
- **Web UI** for calibration, customization, and incident review:
  - Dashboard — live dB readout, arm/disarm controls, incident list with audio playback
  - Timeline — day/week/month incident viewer
  - Thresholds — display current thresholds vs. ordinance limits
  - Config editor — edit YAML configuration without SSH
  - Build documentation — upload photos and notes documenting physical setup
- **Day/night ordinance enforcement** — separate thresholds and behavior; nighttime is record-only
- **GPIO relay + audio playback response** (optional, disabled by default) — powers amplifier and plays audio during daytime threshold violations
- **REST API** — full programmatic access to status, incidents, controls, and configuration
- **CSV export** of incident history for external analysis or evidence submission
- **Home Assistant integration** — REST endpoint examples provided for status polling and control commands
- **Noise exclusion profiles** for common false positives (lawn mower, thunder, rain, drive-bys, etc.)
- **Multi-mic scaffolding** — optional secondary mic for differential rejection and reference input for adaptive subtraction (experimental)


## Notes on legal / practical reality

- My local noise ordinance says measurements should align to ANSI Type 1/2 instruments, but this is **not** one. The code mirrors the *logic* (A-weighted, slow/fast behavior, day/night thresholds) but is _not_ certification-grade.
- My ordinance also states measurements are not strictly required if other evidence/testimony shows a disturbance. This makes my log + timestamps + trend data useful even if my meter is not admissible as a formal calibrated instrument.
- Use the response mode conservatively. The most defensible and neighbor-safe deployment is: **log always, manual review, response disabled by default until calibrated and tested**.

## [CHANGELOG](docs/CHANGELOG.md)

## TODOs & Issues

<details>

- ~~TODO: Invoke `Storage.cleanup_old_snippets()` on a schedule — RESOLVED in v5: startup sweep + daily periodic timer in engine loop.~~
- ~~TODO: Add cookie/session-based auth for browser UI — RESOLVED in v5: adopted LAN-trust model; GET pages are unauthenticated, POST mutation endpoints require bearer token when configured.~~
- ~~TODO: Fix `PlaylistPlayer.start()` — RESOLVED in v5: globs audio files from playlist_dir and passes a random selection to the player command.~~
- ~~TODO: Restore "Clear All Incidents" action — RESOLVED in v5: POST `/incidents/clear` with soft-delete-all and confirmation dialog in template.~~
- ~~TODO: Throttle MQTT `publish_state()` — RESOLVED in v5: fires every ~5 seconds instead of every 0.5s loop (~12 msgs/min vs. ~120).~~
- TODO: Re-integrate GPIO relay control — `RelayController` is currently a boolean flag with no hardware interaction (`gpiozero` dependency removed)
- ~~TODO: Wire `sustain_blocks_required` and `release_blocks_required` config values — RESOLVED in v5: dead config keys removed.~~
- TODO: Drive-by homie detection (even if noise violation is detected, if the sound gets louder, then fades out, or just fades out in less than 30 seconds, just strike it from the record)
- TODO: Recording space quota with warnings (cleanup method exists, but no quota enforcement or alerts)
- TODO: Elegant dashboard auto-refresh solution (currently manual reload only; v3 had JS polling every 5s, but a more sophisticated approach is desired)

### Stability (crash/data-loss risks, courtesy of Claude Opus 4.6)

- ~~**SQLite `check_same_thread=False` without connection locking** — RESOLVED in v3: each operation opens its own connection via `Storage.conn()`.~~
- ~~**`RuntimeState` uses bare class attributes** — RESOLVED in v3: replaced with plain dict `engine.state`.~~
- ~~**Ring buffer `push()` is O(n) per sample** — RESOLVED in v3: replaced with block-level deque in `AudioCapture`.~~
- ~~**No thread safety on shared engine state** — RESOLVED in v4: `StateStore` uses `threading.Lock` on all reads/writes; `snapshot()` returns `copy.deepcopy()`.~~
- ~~**Silent audio capture failure** — RESOLVED in v4: engine loop body wrapped in `try/except`; on error sets `state.mic_ok=False`, `state.last_error`, `state.mode="error"`, sleeps 1s and retries.~~
- ~~**Config save accepts arbitrary text without validation** — RESOLVED in v4: `save_yaml_text_validated()` parses and validates YAML before writing; errors returned via redirect message.~~
- ~~**`engine.stop()` defined but never called** — RESOLVED in v4: FastAPI `lifespan` context manager calls `engine.stop()` on shutdown.~~
- ~~**Event audio frames accumulate unbounded in RAM** — RESOLVED in v4: WAV written to disk in append mode via `soundfile.SoundFile` during capture; only pre-roll blocks briefly held in memory.~~

### Functionality (things that don't work as expected)

- ~~**`snippet_post_seconds` config never used** — RESOLVED in v3: config key removed.~~
- ~~**`requests` in requirements.txt unused** — RESOLVED in v3: removed.~~
- ~~**Build photo `src` path 404** — RESOLVED in v3: copies to `static/build/build_photo.jpg`.~~
- ~~**`night_end` semantic confusion** — RESOLVED in v3: renamed to `night_start_hour` / `night_end_hour` integers.~~
- ~~**Timeline view still has no date filtering** — RESOLVED in v4: `since_for_view()` computes UTC cutoff; `list_incidents(since=...)` filters in SQL.~~
- ~~**`engine.py` directly calls `self.storage.conn()`** — RESOLVED in v4: dedicated `Storage.finalize_incident()` method handles snippet path update.~~
- ~~**`driveby_max_duration_sec` config is still dead code** — RESOLVED in v5: dead config key removed.~~
- ~~**`sustain_blocks_required` and `release_blocks_required` config keys are dead code** — RESOLVED in v5: dead config keys removed.~~
- ~~**`PlaylistPlayer.start()` runs player with no file** — RESOLVED in v5: globs audio files from playlist_dir and passes a random selection to the subprocess command.~~
- **`RelayController` is a boolean flag, not GPIO control** — `on()` and `off()` toggle `self.enabled` but perform no hardware action. `gpiozero` was removed entirely. Response mode will log "respond" but won't actually power anything.
- **`music_like_score` formula still uses undocumented magic numbers** — Same formula as v3 (`0.6 * low + 0.4 * tonal_window`). Now documented inline as "strong low-band energy + not-too-flat spectrum" which is better, but the specific weights (0.6, 0.4, 1.6, 0.35) remain unvalidated.
- ~~**`cleanup_old_snippets()` defined but never invoked** — RESOLVED in v5: called at engine startup and once daily via periodic timer.~~
- ~~**Auth blocks the web UI when enabled** — RESOLVED in v5: LAN-trust model adopted; GET pages are unauthenticated, POST mutation endpoints require bearer token when configured.~~
- **Incident average dB is still a simple running mean** — `np.mean(self.active["dbs"])` over the entire incident. For long incidents, early readings dominate.

### Performance

- ~~**Ring buffer is the primary bottleneck** — RESOLVED in v3: block-level deque.~~
- ~~**`list(self.history_db)` repeated copies** — RESOLVED in v3: `ExclusionEngine` removed.~~
- ~~**CSV export writes temp file** — RESOLVED in v3: uses `StreamingResponse`.~~
- ~~**`list_incidents()` still returns the entire table** — RESOLVED in v4: `list_incidents(limit, offset, since)` with SQL-level filtering and pagination.~~
- ~~**No WAV cleanup or rotation** — RESOLVED in v5: `cleanup_old_snippets()` called at engine startup and daily via periodic timer.~~
- **Blocking `sd.rec()` prevents multi-mic support** — v2 used non-blocking callback streams. v4 still uses blocking `sd.rec()` + `sd.wait()`. Plugin stubs for reference subtraction and dual-mic exist but cannot work with this capture model.
- ~~**MQTT publishes on every engine loop** — RESOLVED in v5: throttled to every ~5 seconds (~12 msgs/min).~~
- **Snippet serving holds file handle for HTTP response duration** — `StreamingResponse(open(path, "rb"))` on large WAV files keeps a file descriptor open. Not critical at home-network scale but not great.
- ~~**`get_snippet()` does a full `list_incidents(limit=100000)`** — RESOLVED in v5: dedicated `Storage.get_incident(id)` method restored.~~

### Usability

- ~~**No confirmation on destructive actions** — RESOLVED in v3 for individual delete. v4 retains confirmation.~~
- ~~**No visible error state on dashboard** — RESOLVED in v4: `state.mic_ok` and `state.mode` ("error") displayed as pill badges; `last_error` available via `/api/health`.~~
- ~~**Config page has no feedback on save** — RESOLVED in v4: `?msg=saved-restart-required` or `?msg=error:...` displayed in template.~~
- ~~**No incident detail/edit view** — RESOLVED in v4: inline notes textarea per incident row.~~
- ~~**No way to pause/resume monitoring** — RESOLVED in v4: Pause/Resume buttons on dashboard toggle `state.armed`.~~
- ~~**No mobile-friendly touch targets** — RESOLVED in v4: button padding increased to `12px 16px`; viewport meta tag added.~~
- **Build page only stores one photo** — Uploading a new photo still overwrites the previous one.
- ~~**Calibration page is now just instructions** — RESOLVED in v5: wizard restored (compute form + saved profiles table) alongside manual calibration instructions.~~
- ~~**No "Clear All Incidents" action** — RESOLVED in v5: POST `/incidents/clear` with soft-delete-all and confirmation dialog.~~
- **Dashboard lacks auto-refresh** — v3 had JS polling every 5 seconds. v4 dashboard is fully server-rendered with no auto-refresh. Users must manually reload to see updated dB readings and incident status.
- **Thresholds page removed** — Ordinance reference is now on the dashboard, but the detailed threshold-vs-config comparison table is gone. Users can't easily see if their configured thresholds differ from the ordinance.

### Security

- ~~**No authentication whatsoever** — RESOLVED in v4: optional bearer token auth via `app.auth_token` config. `must_auth()` enforced on all mutation and page endpoints.~~
- ~~**Photo upload has no file-type validation** — RESOLVED in v4: server-side extension check rejects non-image files.~~
- ~~**Service runs as `User=root`** — RESOLVED in v4: dedicated `noisewarden` system user with `audio` + `gpio` group membership.~~
- **Player command injection** — `PlaylistPlayer.start()` still uses `.split()` on the command string. If `player_command` config contains shell metacharacters or quoted paths with spaces, behavior is undefined. With auth now in place, the attack surface is reduced but not eliminated.
- **Auth token transmitted in cleartext** — Bearer token sent over HTTP (no TLS). On a home LAN this is low-risk, but anyone sniffing the network can capture it. Consider documenting TLS proxy setup for security-conscious deployments.
- ~~**Auth breaks web UI** — RESOLVED in v5: LAN-trust model; GET pages unauthenticated, only POST mutations require bearer token.~~
- **`/api/state` and `/api/health` have no auth** — These endpoints bypass `must_auth()`. Low-risk read-only data on LAN, but noted for awareness.

### Install / deploy

- ~~**Install script `sudo sed` permission failure** — RESOLVED in v3: uses `sudo cp`.~~
- ~~**`static/` and `static/build/` directories not created by install script** — RESOLVED in v4: `web.py` creates them at startup via `os.makedirs(..., exist_ok=True)`.~~
- ~~**No health check endpoint** — RESOLVED in v4: `/api/health` returns engine thread liveness, mic status, and last error.~~
- **Install script enforces `/opt/noise-warden/` but config hardcodes paths** — Both `install_pi.sh` and `noise_warden.yaml` assume `/opt/noise-warden/` paths. Changing one without the other breaks silently. The `NOISE_WARDEN_CONFIG` env var helps for the config file itself but doesn't address the hardcoded `shared_dir`, `base_dir`, etc. inside the YAML.

</details>
