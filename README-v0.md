# Noise Warden (Ordinance-Aware)

**Purpose:** A Raspberry Pi–class system that monitors a directional microphone, estimates ordinance-relevant sound levels, logs incidents, and (optionally) activates a response playlist **only during daytime**. During **night (10:00 PM–7:00 AM)** it **records only** (no retaliation).

> **Important:** This project is designed for **evidence collection and automation experiments**. It is **not a certified ANSI Type 1/2 sound level meter** and should **not** be relied upon as the sole proof of a legal violation. Your city code explicitly says sound level measurements are *desirable but not required* if other evidence/testimony establishes a disturbance. Use this as a logging/correlation tool, not a courtroom laser cannon.

## Ordinance basis (my city, residential/agricultural)
From your uploaded ordinance:

- **DAY** = **7:00 AM to 10:00 PM** fileciteturn1file0
- **NIGHT** = **10:00 PM to 7:00 AM** fileciteturn1file0
- **Continuous noise** (A2/A3 public disturbance / intentionally caused), residential:
  - **65 dBA day**
  - **55 dBA night** fileciteturn1file4
- **Intermittent noise** (A2/A3), residential:
  - **70 dBA day**
  - **60 dBA night** fileciteturn1file4
- **Impulse noise** (A1–A3), residential:
  - **75 dBA day / 60 dBA night**, **FAST** response fileciteturn1file4
- Ordinance defines:
  - **Impulse noise** = on-cycle ≤10% and max continuous duration ≤2s fileciteturn1file0
  - **Intermittent noise** = on-cycle ≤10% and max continuous duration ≤6 min (e.g., motor vehicle passing) fileciteturn1file0
- Measurement intent:
  - **A-weighting**
  - **SLOW** response unless otherwise specified
  - peak intensity not to exceed listed limits fileciteturn1file4

## Your chosen policy
You requested:

- **Exclude impulse noises**
- **Residential calibration**
- **Night = record only**
- Minimize false positives
- Prefer **simple deterministic heuristics** over ML
- Easy incident log retrieval / clear
- Web UI + Home Assistant integration

This code implements that policy.

## What this system does
1. Continuously captures audio from a USB microphone.
2. Applies:
   - A-weighting approximation
   - SLOW-response envelope smoothing for ordinance alignment
   - simple spectral heuristics to suppress:
     - weedwhackers / lawn mowers
     - drive-by vehicles / short transient motor noise
     - obvious self-playback loopback contamination
3. Classifies likely **continuous music-like** or **repetitive LF impulse-like bass** events.
4. Logs incidents to SQLite.
5. During **DAY**:
   - if threshold & persistence criteria are met:
     - powers amplifier relay
     - starts playlist
     - remains active until hold-down condition clears
6. During **NIGHT**:
   - logs only, never activates response
7. Exposes:
   - REST API
   - simple Web UI
   - Home Assistant friendly endpoints/sensors
8. Supports:
   - arm/disarm
   - emergency kill
   - log export
   - log clear

## Hardware BOM (recommended, low-cost / no subscription)
### Core
- **Raspberry Pi 4B (4 GB)** or **Raspberry Pi 5 (4 GB)**
  Pi 4 is enough; Pi 5 is snappier.
- **32–128 GB microSD card** (A2-rated preferred)
- **Official Pi PSU**

### Audio input
- **USB audio interface** with stable Linux support (avoid bargain-bin gremlins)
  - Recommended class: UGREEN / Sabrent USB audio dongle
- **Directional microphone**
  - Budget: **Boya BY-MM1** shotgun mic (3.5mm TRS, passive)
  - Better: **RØDE VideoMic GO II** (USB capable or analog via interface)
- **Windscreen / deadcat**
- **Outdoor-ish mounting bracket / weather-protected enclosure** (if near window/eave)
- Optional but useful:
  - **ground loop isolator** on analog path if needed

### Audio output / response
- **Class D stereo amp board** (e.g., TPA3116/TPA3118)
- **12V power supply** for amp
- **Passive outdoor-rated or sheltered speaker(s)** (or indoor window-facing speaker)
- **Relay module** (opto-isolated 1-channel 5V, Pi-safe) to switch amp power or enable pin
  - Prefer switching **amp enable / remote turn-on** rather than mains AC if possible

### Optional measurement upgrades
- **I2S MEMS mic** (INMP441) if you want a cleaner digital chain, but USB is easier
- **Calibrated SPL meter** (borrow or buy) for one-time calibration against the Pi system

### Networking / integration
- Wi-Fi or Ethernet
- Optional:
  - small UPS HAT or USB UPS

## Software stack
- Python 3.11+
- FastAPI + Uvicorn
- NumPy / SciPy / sounddevice
- python-vlc (or mpv subprocess alternative)
- SQLite
- gpiozero
- optional MQTT (for Home Assistant)

## Project layout
See `/app` and `/web`.

## Fast start
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev libatlas-base-dev portaudio19-dev vlc
cd noise-warden
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# edit config.yaml for your device names, GPIO pin, thresholds, playlist path

python -m app.main
```

Open:
- `http://<pi-ip>:8787/`

## Notes on legal / practical reality
- The ordinance says measurements should align to ANSI type 1/2 instruments, but this is **not** one. The code mirrors the *logic* (A-weighted, slow/fast behavior, day/night thresholds) but is not certification-grade. fileciteturn1file4
- The ordinance also states measurements are not strictly required if other evidence/testimony shows a disturbance. This makes your log + timestamps + trend data useful even if your meter is not admissible as a formal calibrated instrument. fileciteturn1file0
- Use the response mode conservatively. The most defensible and neighbor-safe deployment is:
  - **log always**
  - **manual review**
  - **response disabled by default until calibrated**
