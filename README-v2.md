# Noise Warden v2.1 — Live Audio + GPIO Integration Edition

Deployable Raspberry Pi package with:
- Live `sounddevice` mic capture
- Optional secondary mic / reference input scaffolds
- GPIO relay control via `gpiozero`
- FastAPI web UI
- Incident logging to SQLite
- WAV snippets
- Daytime response / nighttime record-only
- False-positive suppression (impulse, mower-like, drive-by, thunder-like, rain-like)
- Song-gap merge handling
- Build photo upload + editable notes
