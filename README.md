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

### Stability (crash/data-loss risks, courtesy of Claude Opus 4.6)

- **No thread safety on shared `RuntimeState`** — The engine thread writes fields (`active`, `last_db`, `peak_db`, etc.) while FastAPI handler threads read them simultaneously. Python's GIL provides some protection for simple attribute reads, but compound operations (like the running average calculation `sum_db / count_db`) are not atomic. Use a `threading.Lock` or at minimum a thread-safe snapshot method.
- **SQLite `check_same_thread=False` without connection locking** — The engine thread and the web server thread both call into `IncidentStore` concurrently. SQLite in multi-threaded mode requires serialized access. A single accidental concurrent write will raise `sqlite3.OperationalError: database is locked`. Wrap all DB operations with a `threading.Lock`.
- **Silent audio capture failure loop** — In `controller._run()`, if `primary.read()` throws, the code does `time.sleep(0.1); continue` with no logging, no retry counter, and no state indication. A disconnected USB mic will spin silently forever, looking armed and healthy on the dashboard. At minimum, log the error and set a visible `state.last_classification` like `"mic_error"`.
- **Config save accepts arbitrary text without validation** — The `/config` POST endpoint writes raw form input directly to `noise_warden.yaml`. Invalid YAML (or even non-YAML content) will crash the app on next config reload or restart. Validate with `yaml.safe_load()` before writing.
- **Unguarded `int()` / `float()` casts on config values** — `controller.__init__` does things like `int(self.cfg.get("audio","sample_rate"))` with no `try/except`. A typo in the YAML (`sample_rate: "fast"`) will crash the entire app at startup with an unhelpful ValueError.
- **`RuntimeState` uses bare class attributes for `current_incident_start` and `pending_gap_since`** — These are declared outside the `dataclass` field syntax (no type annotation or `field()` call), meaning they're class-level attributes shared across instances. This works by accident with a single instance but is a latent bug if the class is ever instantiated more than once.
- **No graceful shutdown** — `controller.stop()` is never called. The engine thread is daemonic so it dies on process exit, but active incidents are never closed, relay is never explicitly turned off, and the SQLite connection is never closed. A mid-incident kill leaves an open-ended incident in the DB and potentially a relay stuck on.
- **Ring buffer `push()` is O(n) per sample** — `AudioRingBuffer.push()` iterates each sample individually via `.tolist()` and appends one at a time to a `deque`. At 48kHz with 1-second blocks, that's 48,000 `deque.append()` calls per block. This should use `deque.extend()` or better yet, a pre-allocated NumPy circular buffer.

### Functionality (things that don't work as expected)

- **Timeline view is cosmetic only** — The day/week/month selector in `loadTimeline()` changes a label string but performs no actual date filtering. All incidents are always shown regardless of the selected view mode.
- **`snippet_post_seconds` config is never used** — The config declares it but the controller never reads it. Post-trigger frames are accumulated into `event_frames` indefinitely until the incident ends, meaning snippet length is determined entirely by incident duration, not the configured post-buffer.
- **`driveby_max_duration_sec` and `thunder_taper_window_sec` config values are dead** — Declared in config but never referenced in `ExclusionEngine`. The drive-by filter uses a simple 3-sample peak-then-decay heuristic with no duration check.
- **Incident average dB is a running mean, not a windowed mean** — `sum_db / count_db` accumulates over the entire incident. For a 4-hour incident, the average will be heavily weighted toward the early readings. A sliding window or exponential moving average would be more representative.
- **`requests` in requirements.txt is unused** — Imported nowhere in the codebase. Likely a leftover from planned HA integration.
- **Build photo `src` path has no leading separator normalization** — `build.html` renders `src="/{{ build.photo_path }}"` but `photo_path` is stored as `"data/uploads/build_photo.jpg"`, producing `src="/data/uploads/..."` which relies on the `/uploads` static mount being at a different path. The photo will 404. The `src` should reference `/uploads/build_photo{ext}` instead.
- **Exclusion engine history is only 8 frames deep** — With 1-second blocks, the engine can only look back 8 seconds. The mower filter checks 4 frames and the thunder filter checks 4 frames, but a real mower or thunder event may need 15–30 seconds of context to confidently classify.
- **`night_end` is used as `night_start` semantically** — `is_night_mode` uses `rules.night_end` as the "end" parameter which is `07:00`, but the actual config field name `night_end` is confusing because 07:00 is when night _ends_ and day _starts_. Consider renaming to `day_start` / `day_end` or `night_start` / `night_end` with consistent semantics, and add a comment explaining the boundary behavior.

### Performance

- **Ring buffer is the primary bottleneck** — As noted above, sample-by-sample Python-level iteration for 48,000 samples per second is orders of magnitude slower than it needs to be. A pre-allocated numpy array with a write pointer would eliminate this.
- **`list(self.history_db)` called up to 4 times per frame in exclusion engine** — Each `list()` call copies the entire deque. Cache it once at the top of `decide()`.
- **`spectral_features()` is called in `ExclusionEngine.decide()` unconditionally** — Even when all exclusion filters are disabled, the FFT is still computed. Short-circuit if no filters are enabled.
- **`list_incidents()` returns the entire table every time** — No `LIMIT`, no pagination, no date filter. The dashboard slices to 25 client-side, but the full table is serialized and transferred on every 5-second poll. Add server-side `LIMIT` and offset params.
- **CSV export writes all incidents to a file then serves it** — This creates a temp file on every export request. Use `StreamingResponse` with a generator instead, or at least clean up the file afterward. The file also accumulates if exported repeatedly under different names (though currently it's always the same filename, so it overwrites).
- **No WAV cleanup or rotation** — Snippets accumulate in `data/snippets/` forever. On a Pi with a 32GB SD card recording multi-hour incidents at 48kHz mono, storage will fill up. This is already listed as a TODO but bears repeating: without rotation, this will eventually crash the system.

### Usability

- **No confirmation on destructive actions** — "Clear All Incidents" and individual "Delete" buttons fire immediately with no confirmation dialog. One accidental tap deletes your evidence.
- **No visible error state on dashboard** — If the mic disconnects, the dashboard continues showing the last known dB reading and "armed" status. There's no visual indicator that the system is deaf.
- **Config page has no feedback on save** — After saving, the user is redirected back to the config page with no success/failure message. If the save failed (disk full, permissions), they'd never know.
- **Dashboard "Disarm" button doubles as "Emergency Kill"** — The label says "Disarm / Emergency Kill" but functionally it just sets `armed = False`. There's no separate kill state, no confirmation for the emergency action, and no distinction between "pause monitoring" and "stop everything NOW."
- **Build page only stores one photo** — The singleton `build_info` table (enforced by `CHECK (id = 1)`) means uploading a new photo silently overwrites the old one. For documenting a build over time, multiple photos would be valuable.
- **No mobile-friendly touch targets** — Buttons use default `8px 12px` padding. On a phone screen (the most likely access method for checking status from inside), these are uncomfortably small.
- **No incident detail/edit view** — The `notes` column exists in the DB but is never exposed in the UI. You can't annotate incidents with context like "this was the Wednesday party" or "false positive — garbage truck."

### Security

- **No authentication whatsoever** — Any device on the network can arm/disarm the system, delete all incidents, or overwrite the config. The v0 bearer token auth was removed and not replaced. At minimum, add a simple shared secret or basic auth for mutation endpoints.
- **Player command template is an injection vector** — `PlaylistPlayer.start()` uses `command_template.format(file=str(target)).split()` to build the subprocess args. If a filename contains spaces, the split will break the command. If the `player_command` config is modified by an attacker (since config has no auth), they can execute arbitrary commands via template injection.
- **Photo upload has no file-type validation** — The build page accepts any file the browser sends, regardless of the `accept="image/*"` hint (which is client-side only). There's no server-side MIME type or extension check. A malicious upload could place executable content in the uploads directory.
- **Static file mounts expose data directories** — `/snippets` and `/uploads` are mounted as static file directories, making all uploaded and recorded files browsable by anyone on the network.

### Install / deploy

- **Install script uses `sudo sed` write to `/etc/systemd/system/` with no permission check** — The intermediate step writes to `/tmp/noise-warden.service` then uses `sudo sed` to write to the system directory, but the `sed` redirect (`>`) runs in the calling shell's context, not under `sudo`. This will fail with "Permission denied." Should be `sudo cp` or `sudo tee`.
- **Service file hardcodes `User=pi`** — Not all Pi setups use the `pi` user. The install script should either detect the current user or make it configurable.
- **No health check endpoint** — There's no `/api/health` or similar for monitoring tools to check if the service is running and the audio pipeline is healthy. The systemd service will restart on crash but won't detect a hung audio thread.

</details>
