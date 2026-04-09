# noise-warden

Pi-deployable nuisance noise incident logging, management, and reporting tool (web interface for both access and configuration)

<details>

<summary>Why?</summary>

Well, to make a very long story short, sometimes people choose to be loud and obnoxious. And everyone else just gets to suffer, despite there regularly being city noise ordinances, HOA codes of conduct, and apartment tenant agreements that are supposed to protect us all.

Now, let's be clear, I don't mean the occasional Super Bowl party. I mean people who care so little about how they impact others' lives that they will run air compressors and play rap music at 11pm or 6am or whatever suits them without a thought for the newborn their neighbor just got to sleep, the neighborhood children who have school the next day, or the graveyard shift you just finished.

Yes. All of these are examples from my life, each of them in different cities with different neighbors. Our current neighbor takes the cake, though. He stuck an amplified music system in his garage, and started playing frankly terrible music for _hours_ at a time, sending the bass vibrating through my house, night and day. We had begged each jerk in the past to be reasonable humans, but each refused, and this piece was no exception. Recently, after finally giving in and calling the city hotline to report a noise disturbance, only to have my neighbor turn his music back down to still-technically-violating-the-noise-ordinance-but-quiet-enough-no-one-can-reasonably-complain levels before the policeman stopped by, I decided to get quantifiable irrefutable proof on my side. Something that I could show on my phone or tablet to a patrolman or take into city council and have them instantly see and understand the recent violations and impact. Especially because for me, I was able to program in the exact city noise ordinance as the trigger threshold.

Since I had previously gotten out of bed, located my camcorder, opened up a sound meter on my phone, gotten shoes on, gone outside, and stood at my fence line, holding my phone up to the video camera for minutes while I actively fumed, triggering fight-or-flight adrenaline dumps into my system that would take half an hour to wear off enough to fall asleep, I decided to see if we would take me out of the equation. And with recent improvements to Raspberry Pi, ChatGPT and Claude, I could iterate quickly and have something deployable within days, not the months or years it would have taken for me to become proficient enough to architect each piece of my solution one at a time while learning a new programming language and hardware environment. And it finally got the wife to let me buy a Pi. Definite win-win, there.

</details>

> **Important:** This project is designed for **evidence collection and automation experiments**. It is **not a certified ANSI Type 1/2 sound level meter** and should **not** be relied upon as the sole proof of a legal violation. Your city code, like mine, may explicitly say sound level measurements are *desirable but not required* if other evidence/testimony establishes a disturbance. Use this as a logging/correlation tool, not a courtroom laser cannon.

## How it works

```
Microphone → USB Audio → Metric Computation → Noise Floor Gate (skip white noise)
  → Exclusion Filters → Threshold Evaluation (day/night ordinance-aware)
  → Self-Noise Suppression (skip detection during own playback + cooldown)
  → Incident State Machine (hysteresis + song-gap merging)
  → SQLite Incident Log + WAV Snippet Capture
  → Web Dashboard ↔ REST API
  → (Optional) GPIO Relay + Audio Playback Retaliation
```

The system runs a continuous background thread that monitors audio in 1-second blocks against configurable ordinance-aligned thresholds. It distinguishes between genuine nuisance noise and common false positives (impulses, thunder, rain, lawn equipment, drive-bys), merges closely-spaced events into single incidents, and captures WAV snippet evidence with pre-trigger audio buffering.

During **daytime** (7 AM–10 PM), the system can optionally activate a GPIO relay to power an amplifier and play audio in response. During **nighttime** (10 PM–7 AM), it records incidents only — no response activation. Self-noise suppression via a secondary microphone prevents the system from registering its own playback as a noise incident, with a configurable cooldown window after response stops.

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
    - At the default 22,050 Hz sample rate: ~2.6 MB/min (~3.7 GB/day of continuous recording)
    - At 44,100 Hz (CD quality): ~5.3 MB/min (~7.6 GB/day)
    - At 48,000 Hz (studio quality): ~5.8 MB/min (~8.3 GB/day)
    - For storage-limited deployments (≤16 GB SD), stick with 22,050 Hz and consider a shorter `retention_days`

  > I highly recommend a Pi 4+, as the increased CPU and RAM speed helps cut down on processing time, which in turn actually keeps the overhead of the system low.

### Hardware BOM

<details>

#### Core

- **Raspberry Pi 4B (4 GB)** or **Raspberry Pi 5** — Pi 4 is sufficient; Pi 5 is snappier
- **Official Pi PSU**
- **32–128 GB microSD** (A2-rated) or SSD boot

#### Audio input

- **USB audio interface** — class-compliant, stable Linux support (e.g., UGREEN / Sabrent USB dongle). Avoid the Pi's onboard analog jack.
- **Directional microphone** — Budget: Boya BY-MM1 shotgun. Better: RØDE VideoMic GO II.
- **Windscreen / deadcat** — critical for outdoor or window-adjacent placement

> **⚠️ STRONGLY RECOMMENDED: Dual microphone setup if using response mode.**
>
> If you enable the GPIO relay + audio playback retaliation feature, you **really should** use two separate USB audio interfaces with two microphones:
>
> 1. **Primary mic** — pointed at the noise source (neighbor's property). This is the mic used for threshold detection and incident recording.
> 2. **Reference mic** — pointed at your own speaker/amp. This mic captures your playback signal for adaptive noise cancellation (NLMS) and spectral subtraction.
>
> Without a reference mic, self-noise suppression relies solely on a time-based cooldown window (the system stops detecting while responding, then waits `response_cooldown_sec` after playback stops). This works for the common case but cannot distinguish between lingering echo/reverb from your speakers and genuine ongoing neighbor noise that started during your response.
>
> With a reference mic, the `ReferenceSubtractor` (NLMS adaptive filter) and `DualMicDifferential` (spectral subtraction) plugins can mathematically remove your own playback from the primary mic's signal, giving you clean neighbor-noise-only measurements even during active response. See `noise_warden/plugins.py` for algorithm documentation.
>
> **Hardware tip:** Use two separate USB dongles (not a single stereo device) to avoid clock synchronization issues and channel crosstalk. Label them clearly — Pi USB port assignment can change across reboots.

#### Audio output / response (optional)

- **Class D amp board** (e.g., TPA3116/TPA3118)
- **12V power supply** for amp
- **Passive speaker(s)** — outdoor-rated or indoor window-facing
- **5V opto-isolated 1-channel relay module** — controls amp power. Prefer switching amp enable/remote turn-on rather than mains AC.

#### Optional upgrades

- I2S MEMS mic (INMP441) for cleaner digital chain
- Calibrated SPL meter for one-time calibration cross-check
- UPS HAT or USB UPS for power continuity

</details>

### Deployment Architecture

The project uses a symlinked versioning layout that keeps **code disposable** and **data persistent**. Multiple versions can coexist on disk, and rollback is a simple symlink flip + service restart.

```
/opt/noise-warden/
├── current -> /opt/noise-warden/noise-warden-v8   # symlink to active version
├── deploy_noise_warden.sh                          # version-swap script (lives outside any version)
├── venv/                                           # shared Python virtualenv
├── shared/                                         # persistent data — survives upgrades
│   ├── noise_warden.db
│   ├── snippets/
│   ├── playlist/
│   └── build/
│       └── build_photo.jpg
├── noise-warden-v8/                                # previous version (kept for rollback)
└── noise-warden-v9/                                # active version
```

Key invariants:
- `shared/` is **never** inside a version directory — the DB, WAV snippets, playlist, and build assets persist across upgrades
- Config lives in the active version at `current/config/noise_warden.yaml` — copy forward when upgrading if you've made changes
- The `deploy_noise_warden.sh` script is copied to `/opt/noise-warden/` on first install and stays there permanently

### First-Time Installation

<details>

1. Transfer the version archive to the Pi:
    ```bash
    scp noise-warden-v9.zip pi@<pi-ip>:~
    ```

2. SSH in and set up the base directory structure:
    ```bash
    ssh pi@<pi-ip>
    sudo mkdir -p /opt/noise-warden
    sudo chown -R $USER:$USER /opt/noise-warden
    cd /opt/noise-warden
    mv ~/noise-warden-v9.zip .
    unzip noise-warden-v9.zip
    ```

3. Install system-level dependencies (these are not managed by pip):
    ```bash
    sudo apt update && sudo apt install -y python3-venv portaudio19-dev libsndfile1 ffmpeg
    ```

4. Run the install script from inside the version directory:
    ```bash
    cd noise-warden-v9
    bash scripts/install_pi.sh
    ```
    This will: create the `shared/` data directories, create a Python venv at `/opt/noise-warden/venv/`, install pip dependencies, copy `deploy_noise_warden.sh` up to the base directory, set the `current` symlink, create a `noisewarden` system user, and install the systemd service unit.

5. Edit the configuration:
    ```bash
    nano config/noise_warden.yaml
    ```

6. Enable and start the service:
    ```bash
    sudo systemctl enable --now noise-warden
    ```

7. Open in browser: `http://<pi-ip>:8787/`

</details>

### Iterative Upgrade (the actual workflow)

When you have a new version ready:

1. Transfer and extract the new version alongside the old one:
    ```bash
    scp noise-warden-v9.zip pi@<pi-ip>:~
    ssh pi@<pi-ip>
    cd /opt/noise-warden
    mv ~/noise-warden-v9.zip .
    unzip noise-warden-v9.zip
    ```

2. If you've customized `config/noise_warden.yaml`, copy it forward:
    ```bash
    cp current/config/noise_warden.yaml noise-warden-v9/config/noise_warden.yaml
    ```

3. Run the deploy script (which lives at the base level, outside any version):
    ```bash
    ./deploy_noise_warden.sh noise-warden-v9
    ```

    The deploy script will: stop the service, swing the `current` symlink, rebuild/update the venv, install the new systemd unit, and restart the service. `shared/` is untouched.

4. Verify:
    ```bash
    sudo systemctl status noise-warden
    ```

### Rollback

If something goes sideways:
```bash
cd /opt/noise-warden
./deploy_noise_warden.sh noise-warden-v8
```

That's it. Previous version is still on disk, data is still in `shared/`.

### Running Locally (Mac / laptop dev machine)

You don't need a Pi to test the code — most laptops have a built-in microphone, and macOS has straightforward Python support. GPIO relay and response features gracefully degrade to no-ops on non-Pi hardware.

<details>

#### Prerequisites

- **Python 3.11+** (`python3 --version`)
- **Homebrew** (macOS) — [brew.sh](https://brew.sh/)
- System-level audio libraries:
    ```bash
    brew install portaudio libsndfile ffmpeg
    ```

#### Setup

1. Clone the repo and enter it:
    ```bash
    git clone <your-repo-url> noise-warden
    cd noise-warden
    ```

2. Create a virtual environment and install dependencies (including test extras):
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[test]"
    ```

3. Create local data directories (these substitute for `/opt/noise-warden/shared/` on the Pi):
    ```bash
    mkdir -p local_data/snippets local_data/playlist local_data/build
    ```

4. Create a local config override so you don't modify the committed config:
    ```bash
    cp config/noise_warden.yaml config/noise_warden_local.yaml
    ```

    Edit `config/noise_warden_local.yaml` and update the paths:
    ```yaml
    app:
      base_dir: .
      shared_dir: ./local_data
      static_dir: ./static
    ```
    And under `response:`:
    ```yaml
    response:
      playlist_dir: ./local_data/playlist
    ```

5. Point the app at your local config:
    ```bash
    export NOISE_WARDEN_CONFIG=config/noise_warden_local.yaml
    ```

#### Running the app

```bash
# From the repo root, with the venv active:
uvicorn noise_warden.main:app --host 127.0.0.1 --port 8787 --reload
```

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser. The `--reload` flag auto-restarts on code changes.

Your laptop's built-in microphone will be used by default (the `input_device: null` config setting picks the system default). You can verify it's capturing audio on the dashboard's live dB readout — clap or talk near the mic to watch it spike.

#### Running the test suite

Tests run entirely without audio hardware (all capture is mocked):

```bash
pytest tests/ -v
```

#### Cleanup

When you're done testing:

```bash
# Deactivate the virtual environment
deactivate

# Remove the local data directory (DB, snippets, build assets)
rm -rf local_data/

# Remove the local config override
rm -f config/noise_warden_local.yaml

# (Optional) Remove the virtual environment entirely
rm -rf .venv/
```

</details>

## Configuration (IMPORTANT!)

> NOTE: It is extremely important that you take at least a little time to configure, calibrate, and test your setup. Every microphone, environment, and personal irritation threshold is different.

After running the install script and starting the service, it is time to calibrate your system.

### Calibration portability (local → Pi)

> **Can I calibrate on my laptop and transfer the offset to the Pi?**
>
> **Yes, if** you use the same USB audio interface + microphone combo on both machines. The calibration offset compensates for the mic capsule's sensitivity and the USB ADC's gain — those are properties of the hardware, not the host computer. A MacBook and a Pi reading from the same USB dongle + mic will produce the same raw dBFS values (within ~1 dB of noise floor variation).
>
> **HOWEVER, comma,** the built-in microphones on a laptop and a Pi are completely different hardware. A calibration profile computed with your MacBook's internal mic is meaningless when you switch to an external USB mic on the Pi. Always calibrate with the exact mic + interface + cable you intend to deploy.
>
> **Recommended workflow:**
> 1. Connect your USB mic + interface to your laptop
> 2. Run Noise Warden locally, compute a calibration profile via the wizard
> 3. Transfer the `calibration_offset_db` value to your Pi's `noise_warden.yaml`
> 4. On the Pi, do a quick sanity check with a phone SPL meter to confirm the dashboard reading is in the right ballpark (±2 dB)

Configuration is stored in `config/noise_warden.yaml` and can also be edited via the web UI at `/config`. Key sections:

| Section | Purpose |
|---------|---------|
| `site` | City name and ordinance reference |
| `audio` | Sample rate, block size, device selection, mic calibration offset, secondary/reference mic toggles, snippet pre/post buffer durations |

**Recording quality**: The `audio.sample_rate` setting controls recording fidelity. Three options are available via the Calibration page or config YAML:

| Rate | Label | Frequency ceiling | Disk usage | Notes |
|------|-------|-------------------|------------|-------|
| 22,050 Hz | Wideband | 11 kHz | ~2.6 MB/min | Default. Captures all A-weighted energy for dB calculations. Good balance of fidelity and size |
| 44,100 Hz | CD quality | 22 kHz | ~5.3 MB/min | Full audible spectrum. Recordings sound natural to any listener |
| 48,000 Hz | Studio quality | 24 kHz | ~5.8 MB/min | Professional standard. Marginal benefit over 44,100 for evidence purposes |
| `gpio` | Relay pin, active-high/low, enable toggle |
| `response` | Daytime audio response toggle, playlist directory, player command, amp power-on delay, response cooldown |
| `rules` | Day/night schedule, dB thresholds, evaluation interval, release/merge/minimum duration timings, hysteresis |
| `filters` | Per-filter enable toggles and tuning parameters for impulse, drive-by, mower-like, thunder-like, and rain-like exclusions |
| `web` | Host, port, dashboard refresh interval |

### TROUBLESHOOTING configuration tweaking

> [!TIP]
> If you, like me, do not have access to high-end recording equipment, you might try calibrating and testing your noise-warden configuration using a handy YouTube or sound site recording. _Extensive_ analysis of exactly this kind of source data has revealed that YouTube submissions and subsequent compression completely washes out the volume of diesel engines, to the point of not being able to ever trigger the detection criteria. You can try if you want, but be warned.

## Features

- **Intelligent gating, recording, and logging of incidents** — hysteresis-based state machine prevents chatter at threshold boundaries; song-gap merging (default 10 sec) bundles closely-spaced noise events into single incidents
- **WAV snippet evidence capture** — ring buffer preserves pre-trigger audio (15 sec default) plus post-trigger recording, saved per incident
- **5 false-positive exclusion filters** — impulse, thunder-like, rain-like, mower-like, and drive-by patterns each with configurable tuning parameters
- **Reclassify tool** — replay the DSP pipeline against saved snippets to test config changes before deploying them. Available as both a CLI module (see [Reclassify CLI](#reclassify-cli) below) and an in-popup UI button on every incident with a stored snippet
- **Web UI** for calibration, customization, and incident review:
  - Dashboard — live dB readout, arm/disarm/pause controls, detection mode switcher, force test incident, recording toggle, incident list with ▶ play buttons
  - Timeline — day/week/month incident viewer with severity-colored blocks, borderline filtering, and click-to-inspect detail popups
  - Incident detail popup — shared across all pages; shows ordinance violation badge, classification, timestamps, notes, audio player with intensity waveform and click-to-seek, and a "Re-analyze with current config" button that replays the DSP pipeline against the stored snippet and shows an inline comparison (old → new classification, journal timeline, filter distribution). When the classification or timeline has changed, an Apply button commits the update without leaving the popup
  - Calibration — 3-step wizard with live dB/raw dBFS readouts, click-to-fill, offset slider, profile management, sample rate and detection mode controls
  - Thresholds — display current thresholds vs. ordinance limits
  - Config editor — edit YAML configuration without SSH
  - Build documentation — upload photos and notes documenting physical setup
  - Light/dark mode — toggle in nav bar, preference persisted in localStorage. Dark theme uses a Monokai-inspired charcoal palette
- **Day/night ordinance enforcement** — separate thresholds and behavior; nighttime is record-only
- **REST API** — full programmatic access to status, incidents, controls, and configuration
- **CSV export** of incident history for external analysis or evidence submission
- **Home Assistant integration** — REST endpoint examples provided for status polling and control commands
- **Noise exclusion profiles** for common false positives (lawn mower, thunder, rain, drive-bys, etc.)
- **Ambient noise floor gate** — configurable minimum dBA threshold (default 50 dB) below which the DSP pipeline is skipped entirely. Reduces CPU load on the Pi and prevents constant analysis of background white noise. Adjustable via calibration page UI or YAML config
- **GPIO relay + audio playback response** (optional, disabled by default) — powers amplifier and plays audio during daytime threshold violations. Real GPIO control via `gpiozero` when `_RELAY_HW_ENABLED = True` in `response.py`; boolean-only fallback on non-Pi systems
- **Self-noise suppression** — automatically skips incident detection while the system is playing a response (and for a configurable cooldown window afterward), preventing the system from logging its own retaliation as a noise violation
- **Continuous callback audio streaming** — `sd.InputStream` with a callback pushes audio blocks into a thread-safe queue, capturing continuously with zero sample loss between blocks. An earlier blocking mode (`sd.rec()` + `sd.wait()`) was removed after sine wave analysis proved it caused audible clicks at every block boundary
- **Dual-mic reference subtraction plugins** — `ReferenceSubtractor` (NLMS adaptive filter) and `DualMicDifferential` (spectral subtraction) in `plugins.py` with documented algorithms. Not yet wired into the engine loop — requires callback streams and a second `AudioCapture` instance
- **Intensity waveform visualization** — Canvas-based RMS envelope displayed above the audio player in incident popups. Color-coded (teal → lime → yellow → orange → red) by amplitude so loud sections are immediately visible. Click anywhere on the waveform to seek; playback cursor tracks position in real-time
- **Armed state persistence** — Pausing detection writes to YAML config so pauses survive server restarts and watch-mode reloads
- **Offline-capable timeline** — Service worker caches the timeline page and audio snippets for offline review, with proper HTTP Range request handling for cached audio scrubbing

### Reclassify CLI

After tuning filter thresholds in your config YAML, you can re-run the DSP pipeline against any previously-captured snippet to see if the new settings would produce a different classification — without waiting for the next real incident.

```bash
# Analyze a single incident by ID (reads snippet path from DB)
python -m noise_warden.reclassify 63

# Full block-by-block feature table (like the old .TMP_analyze_clip.py)
python -m noise_warden.reclassify 63 --verbose

# Analyze a standalone WAV file (no database needed)
python -m noise_warden.reclassify path/to/clip.wav

# Dry-run all incidents with snippets — shows what WOULD change
python -m noise_warden.reclassify --all

# Batch reclassify and write new classifications back to the database
python -m noise_warden.reclassify --all --update

# Use a specific config file (defaults to local config if present)
python -m noise_warden.reclassify 63 -c config/noise_warden.yaml
```

The tool compares the newly-computed dominant classification against the stored value and reports changes. Use `--update` to commit the new classification and journal to the database. Without it, the tool is read-only (dry run).

## Testing

<details>

Tests use [pytest](https://docs.pytest.org/) and run entirely without audio hardware — all capture and MQTT interactions are mocked.

### Setup (one time, from the repo root)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

### Running the suite

```bash
# All tests, verbose output
pytest tests/ -v

# A single test file
pytest tests/test_engine.py -v

# A single test class or method
pytest tests/test_engine.py::TestDriveByDetection -v
pytest tests/test_storage.py::TestStaleIncidentRepair::test_repairs_incident_without_end_ts -v

# Stop on first failure (useful when debugging)
pytest tests/ -x -v

# Show print() output from the code under test
pytest tests/ -v -s
```

### What's covered

| File | Covers |
|------|--------|
| `test_audio.py` | AudioCapture blocking/callback modes, pre-roll buffer, queue drain, stream lifecycle |
| `test_config.py` | YAML loading, validation, required sections, error paths |
| `test_dsp.py` | RMS/dBFS math, A-weighting, spectrum features, beat confidence, music score, exclusion filters |
| `test_engine.py` | Lifecycle, incident creation, disarmed skip, error recovery, drive-by detection, drive-by quarantine, disk quota, disk-full recording stop, weighted avg dB, device validation, day/night boundary split, max duration split, self-noise suppression (response start/stop/cooldown) |
| `test_ordinance.py` | Threshold lookups, day/night boundary edge cases, ordinance data integrity |
| `test_plugins.py` | ReferenceSubtractor NLMS adaptation/convergence, DualMicDifferential spectral subtraction, single-mic pass-through |
| `test_response.py` | PlaylistPlayer file selection, shlex command parsing, RelayController (boolean fallback + mocked gpiozero GPIO path) |
| `test_state.py` | StateStore get/set, snapshot isolation, thread safety |
| `test_storage.py` | Incident CRUD, soft delete, pagination, CSV export, snippet cleanup, autodismissed cleanup, stale repair, vacuum, WAL mode, schema versioning, index existence |
| `test_reclassify.py` | analyze_clip pipeline, _compute_dominant journal logic, DB-backed reclassify with update, missing snippet/incident handling |
| `test_web.py` | All GET pages, API endpoints, POST mutations, auth enforcement, recording toggle, calibration apply, timeline JSON embedding, SW route, path leak prevention |

</details>

## Notes on legal / practical reality

- My local noise ordinance says measurements should align to ANSI Type 1/2 instruments, but this is **not** one. The code mirrors the *logic* (A-weighted, slow/fast behavior, day/night thresholds) but is _not_ certification-grade.
- My ordinance also states measurements are not strictly required if other evidence/testimony shows a disturbance. This makes my log + timestamps + trend data useful even if my meter is not admissible as a formal calibrated instrument.
- Use the response mode conservatively. The most defensible and neighbor-safe deployment is: **log always, manual review, response disabled by default until calibrated and tested**.

### Database backup

All incident history lives in a single SQLite file (`shared/noise_warden.db`). SD card failure = total loss. Set up periodic backups:

```bash
# /opt/noise-warden/backup_db.sh
#!/bin/bash
BACKUP_DIR=/opt/noise-warden/backups
mkdir -p "$BACKUP_DIR"
sqlite3 /opt/noise-warden/shared/noise_warden.db ".backup $BACKUP_DIR/noise_warden_$(date +%Y-%m-%d).db"
# Keep 30 days of backups
find "$BACKUP_DIR" -name '*.db' -mtime +30 -delete
```

Add to crontab (`crontab -e`):
```
0 */6 * * * /opt/noise-warden/backup_db.sh
```

## [CHANGELOG](docs/CHANGELOG.md)

## TODOs & Issues

<details>

### Speculative / future use cases

- TODO: What if someone wants to use this for identifying overall dog nuisance? Dog barks have a particular noise pattern, and could even be categorized with some spectrographic analysis into "dog1", "dog2", for individual incidents and later tagged with names/locations, etc. Would this just completely break _my_ use case?
- TODO: We could also potentially provide clean recordings of birds (robin, seagull, dove, quail, crow, sparrow-finch-things, roosters) to either identify and record or definitively _exclude_, based on spectral matching. Then potentially other things, like car alarms, lawn mowers, and weedwhackers.
- TODO: Consider alternate usage scenario as a cheap sleep snoring monitor--enabled for _nighttime_ only, coupled with a deep sleep monitor, some correlations could be determined. And easier than having a recorder run _all_ night, and then needing to scrub eight hours of recording for potential data.
- TODO: Consider minority impulse and unknown entries to not be technically part of the (multiple) stipulation.
- TODO: The yaml configuration has some odd sorting, like which things are put under `audio` versus `detection`. I might change audio to recording, and move things like noise_floor_db and calibration_offset_db into it.
- TODO: How did a _lawn mower_ get a music-like score of .55 and actual birdsong only get a score of .19? TODOTODO: After re-recording, see what our new values are

### Architecture

- TODO: **Externalize ordinance thresholds from `ordinance.py` into `noise_warden.yaml`** — Currently, the Pleasant Grove UT thresholds are hardcoded in `ordinance.py` as a Python dict. The thresholds page, config page, and engine all reference this hardcoded data. A proper `ordinance:` section in the YAML would: (a) make thresholds user-editable without touching source code, (b) eliminate magic numbers specific to one city from the codebase, (c) allow the thresholds page to stitch together config + ordinance data for a complete picture, and (d) make evidence logs more credible by having explicit, traceable ordinance references. The YAML section should include: city name, ordinance section reference, day/night hour boundaries, zone-specific dB thresholds per category (continuous, intermittent, impulse), measurement guidance (weighting, mic placement), and legal notes. `ordinance.py` becomes a loader that reads from config instead of a hardcoded dict. All existing test assertions against `ORDINANCE` values need to be updated to use fixture-injected config.

### Functionality

- **`music_like_score` formula still uses undocumented magic numbers** — Same formula as v3 (`0.6 * low + 0.4 * tonal_window`). Now documented inline as "strong low-band energy + not-too-flat spectrum" which is better, but the specific weights (0.6, 0.4, 1.6, 0.35) remain unvalidated.

### Performance

- **Dual-mic plugins not wired into engine** — `ReferenceSubtractor` (NLMS) and `DualMicDifferential` (spectral subtraction) are implemented in `plugins.py` with documented algorithms but not yet called from the engine loop. Requires creating a second `AudioCapture` instance for the reference device.

### Usability

- **Build page only stores one photo** — Uploading a new photo will overwrite the previous one.

### Security

- **Auth token transmitted in cleartext** — Bearer token sent over HTTP (no TLS). On a home LAN this is low-risk, but anyone sniffing the network can capture it. Consider documenting TLS proxy setup for security-conscious deployments.
- **`/api/state` and `/api/health` have no auth** — These endpoints bypass `must_auth()`. Low-risk read-only data on LAN, but noted for awareness.

### Install / deploy

- **Install script enforces `/opt/noise-warden/` but config hardcodes paths** — Both `install_pi.sh` and `noise_warden.yaml` assume `/opt/noise-warden/` paths. Changing one without the other breaks silently. The `NOISE_WARDEN_CONFIG` env var helps for the config file itself but doesn't address the hardcoded `shared_dir`, `base_dir`, etc. inside the YAML.

</details>
