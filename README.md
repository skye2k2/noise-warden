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

- TODO: Recording space quota and rotation, with warnings and automatic archive creation
- TODO: Ability to completely turn off recordings for limited-space scenarios
- TODO: Home Assistant integration beyond REST stubs (MQTT, etc.)
- TODO: Config validation on web UI save (currently accepts invalid YAML)
- TODO: Wire `engine.stop()` to FastAPI shutdown lifecycle (lifespan handler or `on_event("shutdown")`)
- TODO: Add arm/disarm or pause/resume controls back (v3 removed them; engine is always active with no way to temporarily pause monitoring)
- TODO: Expose incident `notes` field in the UI for annotation (column exists in DB, never surfaced)
- TODO: Add `/api/health` endpoint that reports engine thread liveness and mic capture status

### Stability (crash/data-loss risks, courtesy of Claude Opus 4.6)

- ~~**SQLite `check_same_thread=False` without connection locking** — RESOLVED in v3: each operation opens its own connection via `Storage.conn()`.~~
- ~~**`RuntimeState` uses bare class attributes** — RESOLVED in v3: replaced with plain dict `engine.state`.~~
- ~~**Ring buffer `push()` is O(n) per sample** — RESOLVED in v3: replaced with block-level deque in `AudioCapture`.~~
- **No thread safety on shared engine state** — The `engine.state` dict is mutated by the engine thread via `state.update()` while FastAPI handlers read it. `dict.update()` with multiple keys is not atomic — a reader can see a partially-updated snapshot. Use a `threading.Lock` or copy-on-write snapshot.
- **Silent audio capture failure** — `AudioCapture.read_block()` uses `sd.rec()` + `sd.wait()` with no error handling. If the mic disconnects, the exception propagates unhandled in `engine.run()` — the `while self.running` loop has no `try/except`, so the thread dies silently. The dashboard continues showing stale data with no error indication.
- **Config save accepts arbitrary text without validation** — The `/config/save` POST endpoint writes raw form input directly to `noise_warden.yaml`. Invalid YAML will crash the app on next config reload or restart. Validate with `yaml.safe_load()` before writing.
- ~~**Unguarded `int()` / `float()` casts on config values** — Partially mitigated in v3: config access is simpler, but still no validation on load.~~
- **`engine.stop()` defined but never called** — The method exists and correctly joins the thread, stops the player, and turns off the relay. But nothing in the web app lifecycle invokes it. SIGTERM still kills the daemon thread without cleanup, leaving open-ended incidents and potentially a stuck relay.
- **Event audio frames accumulate unbounded in RAM** — `self.active['audio']` appends every block for the entire incident duration. At 16kHz × 0.5s blocks, a 4-hour incident accumulates ~230MB of float32 audio. On a 4GB Pi with other processes, this is a real OOM risk. Should write to disk in chunks or cap the in-memory buffer.

### Functionality (things that don't work as expected)

- ~~**`snippet_post_seconds` config never used** — RESOLVED in v3: config key removed.~~
- ~~**`requests` in requirements.txt unused** — RESOLVED in v3: removed.~~
- ~~**Build photo `src` path 404** — RESOLVED in v3: copies to `static/build/build_photo.jpg`.~~
- ~~**`night_end` semantic confusion** — RESOLVED in v3: renamed to `night_start_hour` / `night_end_hour` integers.~~
- **Timeline view still has no date filtering** — Now server-rendered but still shows all incidents with no day/week/month filtering. The view mode selector was removed, but no filtering was added.
- **`driveby_max_duration_sec` config is dead code** — The drive-by exclusion filter was removed from `classify_noise()` but the config key persists, doing nothing.
- **`music_like_score` formula uses undocumented magic numbers** — `0.6 * clamp(lowband_ratio * 1.6) + 0.4 * clamp(1.0 - abs(flatness - 0.35))` — the weights and constants appear hand-tuned with no documentation of what audio characteristics they target or how they were validated. This is the core detection mechanism.
- **`beat_confidence` measures volatility, not rhythm** — `np.mean(np.abs(np.diff(arr))) / 8.0` on recent dB readings measures general level fluctuation, not rhythmic periodicity. A steady HVAC hum with fluctuating amplitude would score similarly to bass-heavy music. True beat detection needs autocorrelation or onset detection.
- **Mower filter is broader than v2** — v3 uses `flatness >= 0.55 AND centroid > 500` which will match many broadband noise sources. v2's filter was more constrained (300–3000 Hz centroid range + multi-frame envelope stability). Expect more false rejections of legitimate nuisance noise.
- **`engine.py` directly calls `self.storage.conn()`** — Line 56 reaches through the `Storage` abstraction to run raw SQL for updating `snippet_path`. Should be a `Storage` method.
- **Incident average dB is still a simple running mean** — `sum(dbs)/len(dbs)` over the entire incident. For long incidents, early readings dominate. A sliding window or EMA would be more representative of recent conditions.

### Performance

- ~~**Ring buffer is the primary bottleneck** — RESOLVED in v3: block-level deque.~~
- ~~**`list(self.history_db)` repeated copies** — RESOLVED in v3: `ExclusionEngine` removed.~~
- ~~**CSV export writes temp file** — RESOLVED in v3: uses `StreamingResponse`.~~
- **`list_incidents()` still returns the entire table** — No `LIMIT`, no pagination, no date filter. Dashboard slices to 20 server-side (improvement), but the incidents page and timeline load everything. For a home system running 24/7 over weeks, the incident count will grow and page load times will degrade.
- **No WAV cleanup or rotation** — Snippets accumulate in `/opt/noise-warden/shared/snippets/` forever. On a Pi with limited storage, this will eventually fill the disk.
- **Blocking `sd.rec()` prevents multi-mic support** — v2 used non-blocking callback streams that could handle multiple inputs concurrently. v3's blocking `sd.rec()` + `sd.wait()` serializes the engine thread — re-adding secondary/reference mic support would require the architecture to change again.

### Usability

- ~~**No confirmation on destructive actions** — RESOLVED in v3: `onsubmit="return confirm(...)"` added to delete forms.~~
- ~~**Dashboard "Disarm" doubles as "Emergency Kill"** — RESOLVED (by removal) in v3: arm/disarm controls removed entirely. Engine is always active.~~
- **No visible error state on dashboard** — If the mic disconnects, the dashboard continues showing stale state JSON. There's no visual indicator that the system is deaf. The engine thread dies silently.
- **Config page has no feedback on save** — After saving, the user is redirected back to the config page with no success/failure message.
- **Build page only stores one photo** — Uploading a new photo overwrites the previous one. For documenting a build over time (or different angles/placements), multiple photos would be valuable.
- **No mobile-friendly touch targets** — Buttons use default `8px` padding. On a phone screen (the most likely way to check status from inside the house), these are uncomfortably small.
- **No incident detail/edit view** — The `notes` column exists in the DB but is never exposed in the UI. You can't annotate incidents with context like "this was the Wednesday party" or "false positive — garbage truck."
- **No way to pause/resume monitoring** — v3 removed arm/disarm. The engine runs unconditionally. If you need to temporarily stop monitoring (e.g., you're the one making noise), the only option is stopping the systemd service via SSH.
- **Calibration profiles are computed but not auto-applied** — The calibration wizard computes and saves an offset, but the user must manually edit `config/noise_warden.yaml` to apply it. A "use this profile" button would close the loop.

### Security

- **No authentication whatsoever** — Any device on the network can delete all incidents, overwrite the config, or upload files. The v0 bearer token auth was removed and never replaced.
- **Player command injection** — `PlaylistPlayer.start()` uses `.split()` on the command template. While the filename is now passed as a separate arg (improvement over v2), `self.player_cmd.split()` still breaks on quoted args or paths with spaces. The config endpoint has no auth, so an attacker could inject arbitrary commands via the player_cmd field.
- **Photo upload has no file-type validation** — No server-side MIME type or extension check. A malicious upload could place executable content in the static directory.
- **Service runs as `User=root`** — v3 changed from `User=pi` to `User=root`. The entire web server, file writes, GPIO access, and subprocess execution now run with full root privileges. Should use a dedicated unprivileged user with GPIO group membership.
- **Static file mounts expose data directories** — `/static/build/` serves uploaded photos to anyone on the network without authentication.

### Install / deploy

- ~~**Install script `sudo sed` permission failure** — RESOLVED in v3: uses `sudo cp`.~~
- ~~**Service file hardcodes `User=pi`** — Changed in v3, but to `User=root` (see Security).~~
- **`static/` and `static/build/` directories not created by install script** — The web app mounts `/static` and the photo upload copies to `static/build/`. Neither directory is created during install. First photo upload will fail with a FileNotFoundError.
- **No health check endpoint** — No `/api/health` for monitoring tools to verify the service and audio pipeline are healthy. Systemd restarts on crash but cannot detect a hung engine thread.
- **Install script enforces `/opt/noise-warden/` but config hardcodes paths** — Both `install_pi.sh` and `noise_warden.yaml` assume `/opt/noise-warden/shared/` paths. If someone changes the base path in one place, the other breaks silently.

</details>
