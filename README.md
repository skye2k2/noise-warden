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
├── current -> /opt/noise-warden/noise-warden-v12  # symlink to active version
├── deploy_noise_warden.sh                          # version-swap script (lives outside any version)
├── tls/                                            # self-signed TLS cert (generated on first install)
│   ├── cert.pem
│   └── key.pem
├── venv/                                           # shared Python virtualenv
├── shared/                                         # persistent data — survives upgrades
│   ├── noise_warden.db
│   ├── snippets/
│   ├── playlist/
│   └── build/
│       └── build_photo.jpg
├── noise-warden-v11/                               # previous version (kept for rollback)
└── noise-warden-v12/                               # active version
```

Key invariants:
- `shared/` is **never** inside a version directory — the DB, WAV snippets, playlist, and build assets persist across upgrades
- Config lives in the active version at `current/config/noise_warden.yaml` — copy forward when upgrading if you've made changes
- The `deploy_noise_warden.sh` script is copied to `/opt/noise-warden/` on first install and stays there permanently
- **Project files are always copied into `/opt/noise-warden/`** — the `install_pi.sh` script copies from wherever you extracted the archive. The symlink always points inside `/opt/`, so the `noisewarden` service user can always read the files regardless of where the source directory lives

### First-Time Installation

<details>

1. Transfer the version archive to the Pi:
    ```bash
    scp noise-warden-v12.zip pi@<pi-ip>:~
    ```

2. SSH in and extract anywhere — your Desktop, home directory, wherever:
    ```bash
    ssh pi@<pi-ip>
    unzip ~/noise-warden-v12.zip
    ```

3. Install system-level dependencies (these are not managed by pip):
    ```bash
    sudo apt update && sudo apt install -y python3-venv portaudio19-dev libsndfile1 ffmpeg rsync openssl
    ```

4. Run the install script from inside the extracted directory:
    ```bash
    cd ~/noise-warden-v12
    bash scripts/install_pi.sh
    ```
    This will: copy the project into `/opt/noise-warden/noise-warden-v12/`, create the `shared/` data directories, set up a Python venv at `/opt/noise-warden/venv/`, install pip dependencies, copy `deploy_noise_warden.sh` up to the base directory, set the `current` symlink, create a `noisewarden` system user, generate a self-signed TLS certificate (for Service Worker / offline caching support), install the systemd service unit, and run pre-flight validation to verify the service can start.

    > **You can run this from anywhere** — the script copies files into `/opt/noise-warden/` so the system service can always reach them. You don't need to clone directly into `/opt/`.

5. **Connect your USB microphone** and verify it's detected:
    ```bash
    arecord -l
    ```
    You should see at least one `card` entry (e.g., `card 1: Device [USB Audio Device]`). If nothing appears, check the USB connection or try a different port.

6. Verify the `noisewarden` service user has audio access. The install script attempts this automatically, but confirm it took effect:
    ```bash
    groups noisewarden
    ```
    The output should include `audio`. If it doesn't:
    ```bash
    sudo usermod -a -G audio noisewarden
    ```

7. Edit the configuration:
    ```bash
    sudo nano /opt/noise-warden/current/config/noise_warden.yaml
    ```

8. Enable and start the service:
    ```bash
    sudo systemctl enable --now noise-warden
    ```

9. Verify it's running:
    ```bash
    sudo systemctl status noise-warden
    ```

10. Open in browser: `https://<pi-ip>:8787/`

    > **First visit:** Your browser will show a certificate warning because the TLS certificate is self-signed. This is expected — accept the warning to proceed. You only need to do this once per device. The certificate enables Service Worker registration, which is required by browsers for offline caching (page navigation, snippet pre-loading, etc.).

</details>

### Troubleshooting

<details>

**`status=200/CHDIR`** — the service can't access its working directory. This usually means the `current` symlink points outside `/opt/noise-warden/` (e.g., to `~/Desktop/...`). Fix: re-run `install_pi.sh` from the source directory — it will copy files into `/opt/` and fix the symlink.

**`Module not found`** — the venv is missing dependencies. Fix: `source /opt/noise-warden/venv/bin/activate && pip install -r /opt/noise-warden/current/requirements.txt`

**Permission denied** — the `noisewarden` user can't read project files. Fix: `sudo chown -R noisewarden:noisewarden /opt/noise-warden`

**Dashboard shows `mic ok: false`** — two common causes:
1. **Microphone not plugged in.** Connect your USB mic, then verify with `arecord -l`. Restart the service afterward: `sudo systemctl restart noise-warden`
2. **`noisewarden` user not in the `audio` group.** The service user needs audio group membership to access capture devices. Check: `groups noisewarden`. Fix: `sudo usermod -a -G audio noisewarden`, then restart the service. Group changes don't take effect until the process restarts.

**No audio device / `arecord -l` shows nothing** — the USB microphone isn't connected or isn't recognized. Try a different USB port. If using a USB hub, try connecting directly to the Pi. Some mics need `alsa-utils` installed: `sudo apt install -y alsa-utils`

**`paInvalidSampleRate` ALSA errors in journal** — the microphone doesn't support the configured sample rate (default 22050 Hz). Many cheap USB audio dongles only support 44100 or 48000 Hz. The app will automatically fall back to the device's default rate and log a warning, but to fix it permanently, set `sample_rate: 48000` (or `44100`) in your config. Check what your device supports: `arecord -D hw:1,0 --dump-hw-params /dev/null 2>&1 | grep RATE` (adjust `hw:1,0` to your card number from `arecord -l`)

**`Error querying device -1` looping in journal** — the service's PortAudio device cache went stale. This typically happens when the PulseAudio/PipeWire audio profile for the USB mic is changed while the service is already running (e.g., switching from "Analog Stereo Duplex" to "Analog Stereo Input" in the OS sound settings). The engine now automatically forces a PortAudio device rescan on each reconnection attempt and uses escalating backoff (2s → 30s) to avoid log spam. In most cases, the service will self-recover within a few attempts once the profile settles. If it doesn't recover after ~10 attempts (the journal will say "10 consecutive audio failures"), use the **Restart Service** button on the dashboard, or from SSH: `sudo systemctl restart noise-warden`. You can verify the current profile with `pactl list cards` and confirm the mic is visible to ALSA with `arecord -l`. For a mic-only device (no speaker output), the `input:analog-stereo` profile is correct.

**TLS certificate warning won't go away / pages not caching on phone** — the self-signed TLS certificate must be accepted in the browser for the Service Worker (and thus offline caching) to function. On iOS Safari, you may need to go to Settings → General → About → Certificate Trust Settings and enable the certificate. On Android Chrome, tapping "Advanced" → "Proceed" on the warning page is sufficient. If you regenerate the certificate (delete `/opt/noise-warden/tls/` and re-run `install_pi.sh`), you'll need to accept the new cert on all devices again.

**EACCES permission denied when deleting snippets via SSH** — files under `/opt/noise-warden/shared/` are owned by the `noisewarden` system user. Your SSH user can't modify them by default. Fix: add your SSH user to the `noisewarden` group, then log out and back in for the group change to take effect:

```bash
sudo usermod -aG noisewarden $(whoami)
# Log out and SSH back in, then verify:
groups  # should list "noisewarden"
```

If the directory permissions are still too restrictive (owner-only), update them to allow group write access:

```bash
sudo chmod -R g+w /opt/noise-warden/shared/snippets/
```

Alternatively, use the web UI to delete incidents — it runs as the `noisewarden` user and has full access. To clean up stale database references after manually deleting snippet files, run `python -m noise_warden.reclassify --purge-orphans`.

</details>

### Iterative Upgrade (the actual workflow)

When you have a new version ready:

1. Transfer and extract the new version anywhere on the Pi:
    ```bash
    scp noise-warden-v13.zip pi@<pi-ip>:~
    ssh pi@<pi-ip>
    unzip ~/noise-warden-v13.zip
    ```

2. Run install_pi.sh from the new version (handles the copy into `/opt/`):
    ```bash
    cd ~/noise-warden-v13
    bash scripts/install_pi.sh
    ```

3. If you've customized `config/noise_warden.yaml`, copy it forward:
    ```bash
    sudo cp /opt/noise-warden/noise-warden-v12/config/noise_warden.yaml \
            /opt/noise-warden/noise-warden-v13/config/noise_warden.yaml
    ```

4. Start/restart the service:
    ```bash
    sudo systemctl restart noise-warden
    sudo systemctl status noise-warden
    ```

    Or use the deploy script (which lives at the base level, outside any version):
    ```bash
    cd /opt/noise-warden
    sudo ./deploy_noise_warden.sh noise-warden-v13
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
| `audio` | Sample rate, block size, device selection, mic calibration offset, secondary/reference mic toggles, snippet pre/post buffer durations, spectral denoising (enable + percentile/alpha/beta tuning), snippet normalization (enable + target peak dBFS) |
| `detection` | Zone, mode, armed state, calibration offset, noise floor gate, borderline margin + `record_borderline_events` toggle, day/night hours, music-focus thresholds, impulse/filter detection parameters, holdover tuning |

**Recording quality**: The `audio.sample_rate` setting controls recording fidelity. Three options are available via the Calibration page or config YAML:

| Rate | Label | Frequency ceiling | Disk usage | Notes |
|------|-------|-------------------|------------|-------|
| 22,050 Hz | Wideband | 11 kHz | ~2.6 MB/min | Default. Captures all A-weighted energy for dB calculations. Good balance of fidelity and size |
| 44,100 Hz | CD quality | 22 kHz | ~5.3 MB/min | Full audible spectrum. Recordings sound natural to any listener |
| 48,000 Hz | Studio quality | 24 kHz | ~5.8 MB/min | Professional standard. Marginal benefit over 44,100 for evidence purposes |
| `gpio` | Relay pin, active-high/low, enable toggle |
| `response` | Daytime audio response toggle, playlist directory, player command, amp power-on delay, response cooldown |
| `rules` | Day/night schedule, dB thresholds, evaluation interval, release/merge/minimum duration timings, hysteresis |
| `filters` | Per-filter enable toggles and tuning parameters for all 11 exclusion filters (thunder, impulse, birdsong, amplified bass, wind, rain, weedwhacker, mower, diesel, flyover, conversation) |
| `web` | Host, port, dashboard refresh interval |

### Troubleshooting configuration tweaking and adding your own calibration test sounds

> [!TIP]
> If you, like me, do not have access to high-end recording equipment, you might try calibrating and testing your noise-warden configuration using a handy YouTube or sound site recording. _Extensive_ analysis of exactly this kind of source data has revealed that YouTube submissions and subsequent compression completely washes out the volume of diesel engines, to the point of not being able to ever trigger the detection criteria. You can try if you want, but be warned. However, high-fidelity FLAC recordings by design contain the data we need as part of the digital signal processing engine, provided that the microphone used was not absolute garbage. Many times you can find helpful audiophiles on [freesound.org](freesound.org) whose audio files (pun intended) meet our criteria. You can then download, downsample, decouple, and export to an uncompressed mono WAV at a sample rate of 44.1khz and 16-bit depth using your sound editor of choice.

## Features

- **Intelligent gating, recording, and logging of incidents** — hysteresis-based state machine prevents chatter at threshold boundaries; song-gap merging (default 10 sec) bundles closely-spaced noise events into single incidents. Optional `record_borderline_events: false` auto-dismisses incidents whose peak is within `borderline_margin_db` of the threshold (classified as 'borderline', snippet quarantined), keeping the log focused on clear violations
- **WAV snippet evidence capture** — ring buffer preserves pre-trigger audio (15 sec default) plus post-trigger recording, saved per incident
- **11 noise exclusion filters** — thunder, impulse, birdsong, amplified bass, wind, rain, weedwhacker, mower, diesel, flyover, and conversation — each with individually configurable tuning parameters and holdover persistence through brief gaps
- **Reclassify tool** — replay the DSP pipeline against saved snippets to test config changes before deploying them. Available as both a CLI module (see [Reclassify CLI](#reclassify-cli) below) and an in-popup UI button on every incident with a stored snippet. Batch mode (`--all`) compares both classification and journal timeline, reporting change types. Also supports `--denoise` to batch-denoise ambient hiss and `--normalize` to batch-normalize snippet volumes after analysis
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
- **Ambient noise floor gate** — configurable minimum dBA threshold (default 50 dB) below which the DSP pipeline is skipped entirely. Reduces CPU load on the Pi and prevents constant analysis of background white noise. Adjustable via calibration page UI or YAML config
- **GPIO relay + audio playback response** (optional, disabled by default) — powers amplifier and plays audio during daytime threshold violations. Real GPIO control via `gpiozero` when `_RELAY_HW_ENABLED = True` in `response.py`; boolean-only fallback on non-Pi systems
- **Self-noise suppression** — automatically skips incident detection while the system is playing a response (and for a configurable cooldown window afterward), preventing the system from logging its own retaliation as a noise violation
- **Continuous callback audio streaming** — `sd.InputStream` with a callback pushes audio blocks into a thread-safe queue, capturing continuously with zero sample loss between blocks. An earlier blocking mode (`sd.rec()` + `sd.wait()`) was removed after sine wave analysis proved it caused audible clicks at every block boundary
- **Dual-mic reference subtraction plugins** — `ReferenceSubtractor` (NLMS adaptive filter) and `DualMicDifferential` (spectral subtraction) in `plugins.py` with documented algorithms. Not yet wired into the engine loop — requires callback streams and a second `AudioCapture` instance
- **Intensity waveform visualization** — Canvas-based RMS envelope displayed above the audio player in incident popups. Color-coded (teal → lime → yellow → orange → red) by amplitude so loud sections are immediately visible. Click anywhere on the waveform to seek; playback cursor tracks position in real-time
- **Armed state persistence** — Pausing detection writes to YAML config so pauses survive server restarts and watch-mode reloads
- **Offline-capable UI** — Service worker eagerly pre-caches all navigable pages on first visit and caches audio snippets for offline review, with proper HTTP Range request handling for cached audio scrubbing. Server-reachability detection (not `navigator.onLine`) persists across page navigations via `sessionStorage`, graying out dashboard pills and disabling mutation controls when the server is unreachable. Requires HTTPS — the install script generates a self-signed TLS certificate for Pi deployment; `localhost` is exempt for local development
- **Snippet spectral denoising** — self-adaptive spectral subtraction removes the omnipresent ambient hiss ("seashore whoosh") from USB microphone recordings. Uses minimum-statistics noise estimation per snippet (Martin 1994 simplified) — no manual noise profile capture required. Per-frequency-bin, the Nth percentile of magnitudes across all STFT frames is treated as the stationary noise floor, then subtracted with an oversubtraction factor and spectral floor to prevent musical-noise artifacts. Runs AFTER DSP analysis (classification uses the raw signal) and BEFORE normalization (so gain boost amplifies clean audio, not hiss). Enable with `snippet_denoise: true`; tunable via `denoise_percentile`, `denoise_alpha`, and `denoise_beta`. Existing snippets can be batch-denoised via `python -m noise_warden.reclassify --all --denoise`
- **Snippet volume normalization** — USB microphones produce very quiet WAV files (-30 to -50 dBFS) despite measuring loud real-world sounds, because the calibration offset maps raw digital levels to dBA. Two complementary solutions: (1) **write-time normalization** (`snippet_normalize: true`) boosts each snippet's peak to −6 dBFS on save — only amplifies, never attenuates — so evidence recordings are immediately audible on any device; and (2) **playback volume boost** (1×–5× gain selector in the incident popup via WebAudio GainNode) for reviewing quiet legacy recordings without re-processing them. Existing snippets can be batch-normalized via `python -m noise_warden.reclassify --all --normalize`

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

# Batch-denoise all snippet WAVs (remove ambient hiss via spectral subtraction)
# Denoising runs AFTER analysis, BEFORE normalization
python -m noise_warden.reclassify --all --denoise

# Batch-normalize all snippet WAVs (boost quiet recordings to -6 dBFS)
# Normalization runs AFTER analysis and denoising
python -m noise_warden.reclassify --all --normalize

# Combine: reclassify + denoise + normalize + write changes back
python -m noise_warden.reclassify --all --update --denoise --normalize

# Clean up stale DB references after manually deleting snippet files
python -m noise_warden.reclassify --purge-orphans

# Purge orphans then reclassify all remaining snippets
python -m noise_warden.reclassify --purge-orphans --all

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

## Troubleshooting Triage

<details>

When the web interface becomes unreachable, SSH into the Pi and run these commands in order. Each section builds on the previous — start at the top and stop when you find the problem.

### T.1 — Is the Pi alive and reachable?

```bash
# From another machine on the network:
ping 192.168.13.5                    # Replace with your Pi's IP
ssh username@192.168.13.5           # If ping fails, power-cycle the Pi

# If SSH works but web doesn't, the service is likely down — continue below
```

### T.2 — Is the noise-warden service running?

```bash
sudo systemctl status noise-warden
# Look for: Active: active (running) vs. failed/inactive/dead

# If "failed" — check why it failed:
sudo systemctl show noise-warden -p ActiveState,SubState,Result,ExecMainStatus,NRestarts
# Result=signal + ExecMainStatus=9 → OOM killed
# Result=exit-code → application crash (check logs)
# NRestarts shows how many times systemd has restarted it this boot

# If the service hit its restart limit ("start request repeated too quickly"):
sudo systemctl reset-failed noise-warden
sudo systemctl start noise-warden
```

### T.3 — Check the service logs

```bash
# Recent logs (last 100 lines):
sudo journalctl -u noise-warden --no-pager -n 100

# Logs since last boot:
sudo journalctl -u noise-warden -b 0 --no-pager | tail -60

# Logs from a specific time window:
sudo journalctl -u noise-warden --since "2026-04-25 20:00" --until "2026-04-30" --no-pager

# Follow live logs:
sudo journalctl -u noise-warden -f

# All error-level messages from the current boot:
sudo journalctl -b 0 -p err --no-pager | tail -40
```

### T.4 — Check for OOM kills

```bash
# Kernel OOM events (current boot):
sudo dmesg | grep -i "oom\|killed process\|out of memory"

# Historical OOM events across boots:
sudo journalctl -k --no-pager --grep "oom_kill\|Out of memory\|Killed process" | tail -20

# Check current memory pressure:
free -h
cat /proc/meminfo | head -10

# Check the noise-warden process specifically:
ps aux | grep uvicorn
# Look at RSS column — normal is ~90-120 MB. If it's 500+ MB, there's a leak.

# Detailed process memory breakdown:
ps -p $(pgrep -f "uvicorn.*noise_warden") -o pid,rss,vsz,etime,pcpu,pmem
```

### T.5 — Check boot history (was there a crash/reboot?)

```bash
# List all boots — gaps in timestamps reveal unplanned shutdowns:
sudo journalctl --list-boots --no-pager

# Check the previous boot's final messages:
sudo journalctl -b -1 --no-pager | tail -30

# System uptime:
uptime

# Last shutdown/reboot events:
last reboot | head -5
last shutdown | head -5
```

### T.6 — Check thermal status

```bash
# Current CPU temperature:
vcgencmd measure_temp
# Normal: 50-70°C. Throttling starts at 75°C.

# Has the Pi throttled (current or historical)?
vcgencmd get_throttled
# 0x0  = no throttling ever
# 0x1  = currently under-voltage
# 0x2  = currently ARM frequency capped
# 0x4  = currently throttled
# 0x8  = soft temperature limit active
# Bits 16-19 are the same flags but indicate "has occurred since boot"
# e.g. 0x50000 = throttling has occurred since boot but not currently active

# Kernel thermal trip points:
for t in /sys/class/thermal/thermal_zone0/trip_point_*_temp; do
  idx=$(echo $t | grep -oP 'trip_point_\K\d+')
  echo "Trip $idx: $(cat $t)m°C ($(cat /sys/class/thermal/thermal_zone0/trip_point_${idx}_type))"
done

# Current CPU frequency (should be 1500000 or 2400000 on Pi 5):
cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq
```

### T.7 — Check network connectivity

```bash
# Interface status:
ip addr show | grep -E "inet |state"
# wlan0 should show state UP with an IP address
# eth0 state DOWN/NO-CARRIER means no cable

# WiFi signal strength:
iw wlan0 link
# Look for "signal:" — below -70 dBm is weak, below -80 dBm is unreliable

# WiFi power saving (should be off for reliability):
iw wlan0 get power_save

# NetworkManager status and recent events:
nmcli general status
nmcli connection show --active

# Recent WiFi disconnection events:
sudo journalctl -b 0 --no-pager --grep "wlan0.*link timed out\|association\|disconnect" | tail -10
```

### T.8 — Check disk space and database health

```bash
# Filesystem usage:
df -h /

# Swap status:
swapon --show

# Snippet directory size:
du -sh /opt/noise-warden/shared/snippets/

# Database size and integrity:
ls -lh /opt/noise-warden/shared/noise_warden.db
sqlite3 /opt/noise-warden/shared/noise_warden.db "PRAGMA integrity_check;"
sqlite3 /opt/noise-warden/shared/noise_warden.db "SELECT COUNT(*) FROM incidents;"
sqlite3 /opt/noise-warden/shared/noise_warden.db "SELECT COUNT(*) FROM incidents WHERE end_ts IS NULL;"
# ^ Non-zero means stale incidents from a crash (engine repairs these on startup)
```

### T.9 — Check systemd resource limits and service configuration

```bash
# Memory and restart limits applied to the service:
systemctl show noise-warden -p MemoryMax,MemoryHigh,MemoryCurrent,RestartUSec,NRestarts,StartLimitIntervalUSec,StartLimitBurst,OOMPolicy

# Full service status with recent log excerpt:
systemctl status noise-warden -l --no-pager

# Is the service enabled to start on boot?
systemctl is-enabled noise-warden
```

### T.10 — Emergency recovery commands

```bash
# If the service is in "failed" state and won't start:
sudo systemctl reset-failed noise-warden
sudo systemctl start noise-warden

# If the service starts but immediately crashes (check logs first!):
# Validate the config file:
python3 -c "import yaml; yaml.safe_load(open('/opt/noise-warden/current/config/noise_warden.yaml'))"

# If the symlink is broken:
ls -la /opt/noise-warden/current
# Fix with: sudo ln -sfn /opt/noise-warden/noise-warden-v14 /opt/noise-warden/current

# If audio device is missing/changed:
python3 -c "import sounddevice; print(sounddevice.query_devices())"

# Manual start (bypasses systemd, useful for debugging):
cd /opt/noise-warden/current
sudo -u noisewarden /opt/noise-warden/venv/bin/uvicorn noise_warden.main:app \
  --host 0.0.0.0 --port 8787 \
  --ssl-certfile /opt/noise-warden/tls/cert.pem \
  --ssl-keyfile /opt/noise-warden/tls/key.pem

# If nothing works — check the TLS certificate:
openssl x509 -in /opt/noise-warden/tls/cert.pem -noout -dates
# Self-signed certs can expire; browsers reject expired certs silently
```

### T.11 — Quick health check one-liner

```bash
# Paste this for a full snapshot of system and service health:
echo "=== SERVICE ===" && sudo systemctl status noise-warden --no-pager -l | head -15 && echo "=== MEMORY ===" && free -h && echo "=== PROCESS ===" && ps -p $(pgrep -f "uvicorn.*noise_warden" || echo 1) -o pid,rss,vsz,etime,pcpu,pmem 2>/dev/null || echo "NOT RUNNING" && echo "=== TEMP ===" && vcgencmd measure_temp && echo "=== THROTTLE ===" && vcgencmd get_throttled && echo "=== DISK ===" && df -h / | tail -1 && echo "=== NETWORK ===" && iw wlan0 link 2>/dev/null | grep -E "signal|SSID" && echo "=== UPTIME ===" && uptime
```

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
- TODO: Now that we have a number of different classifications, i see them being applied a little too strictly, like in thunder-and-light-rain and thunder-cracks--if we are only going to classify something for a block or three, differently than the sweeping majority, it should not be bolded. Or maybe more, if we implement weighting and backtracking. Because, for example, we _know_ that thunder rumble trail off looks like a diesel, but the odds that someone started their diesel up _right_ as a thunderclap hits is extraordinarily low, so we should clamp.
- TODO: The yaml configuration has some odd sorting and grouping, like which things are put under `audio` versus `detection`. I might change `audio` to `recording`, and move things like `noise_floor_db` and `calibration_offset_db` into it.
- TODO: Make sure that the system will function the same with 48kHz sampling and recording.
- TODO: Under windy conditions, that portion of the attic has a few locations that creak/rattle. Shim/reattach/glue/foam insulate as best as possible.
- TODO: It also feels that the reclassifying did not _actually_ reclassify the data...denoise, normalize, re-journal. Because manually clicking re-analyze still had changes it would apply, despite the batch job having run. Pick one from the second page that I did not already click through to manually reclassify on, download it, and compare to the original download from 2026-04-19.
- TODO: Add instruction to (right-click on the player and select "Save Audio as..." to export audio)
- TODO: The backup job that is recommneded is just the database--if you want the _data_, you also need to grab the snippets directory. And if you are just connected to the webapp, you may just want to "download all", instead of clicking into each to right-click-and-save-as individually.
- TODO: In music-detection mode, have the ability to drop everything _except_ unknown and music
- TODO: If it helps classification, we can apply some mutual exclusion filters. For example, if thunder has been detected, it should stick very heavily, even if the system thinks it heard a plane flyover (they so have a lot of similarity), because the two events are pretty much mutually exclusive — **PARTIALLY ADDRESSED in v14**: Thunder Path B threshold relaxation (rumble_min_db 95→40, rumble_centroid_max 1300→1500) now catches enough consecutive rumble blocks to activate holdover, which naturally suppresses flyover during active thunder. True explicit mutual exclusion (post-processing reclassification of flyover→thunder when both appear) remains a potential future enhancement for journal cleanup.

### Architecture

- TODO: **Externalize ordinance thresholds from `ordinance.py` into `noise_warden.yaml`** — Currently, the Pleasant Grove UT thresholds are hardcoded in `ordinance.py` as a Python dict. The thresholds page, config page, and engine all reference this hardcoded data. A proper `ordinance:` section in the YAML would: (a) make thresholds user-editable without touching source code, (b) eliminate magic numbers specific to one city from the codebase, (c) allow the thresholds page to stitch together config + ordinance data for a complete picture, and (d) make evidence logs more credible by having explicit, traceable ordinance references. The YAML section should include: city name, ordinance section reference, day/night hour boundaries, zone-specific dB thresholds per category (continuous, intermittent, impulse), measurement guidance (weighting, mic placement), and legal notes. `ordinance.py` becomes a loader that reads from config instead of a hardcoded dict. All existing test assertions against `ORDINANCE` values need to be updated to use fixture-injected config.

### Functionality

- **`music_like_score` formula still uses undocumented magic numbers** — Same formula as v3 (`0.6 * low + 0.4 * tonal_window`). Now documented inline as "strong low-band energy + not-too-flat spectrum" which is better, but the specific weights (0.6, 0.4, 1.6, 0.35) remain unvalidated.

### Performance

- **Dual-mic plugins not wired into engine** — `ReferenceSubtractor` (NLMS) and `DualMicDifferential` (spectral subtraction) are implemented in `plugins.py` with documented algorithms but not yet called from the engine loop. Requires creating a second `AudioCapture` instance for the reference device.

### Usability

- **Build page only stores one photo** — Uploading a new photo will overwrite the previous one.

### Security

- **Auth token now encrypted in transit** — Bearer token transmitted over TLS (self-signed certificate generated during install). While a self-signed cert doesn't verify identity, it does encrypt traffic — sniffing the LAN no longer exposes the token in cleartext.
- **`/api/state` and `/api/health` have no auth** — These endpoints bypass `must_auth()`. Low-risk read-only data on LAN, but noted for awareness.

### Install / deploy

- **Install script enforces `/opt/noise-warden/` but config hardcodes paths** — Both `install_pi.sh` and `noise_warden.yaml` assume `/opt/noise-warden/` paths. Changing one without the other breaks silently. The `NOISE_WARDEN_CONFIG` env var helps for the config file itself but doesn't address the hardcoded `shared_dir`, `base_dir`, etc. inside the YAML.

</details>
