# Architecture

```text
Directional Mic
   |
   v
USB Audio Interface / Pi Audio Input
   |
   v
[ Audio Capture ]
   |
   v
[ A-Weighting Approximation ]
   |
   v
[ Feature Extraction ]
   |- RMS dB
   |- Slow dB
   |- Fast dB
   |- Spectral Flux
   |- Bandwidth
   |- Bass Ratio
   |- Tonal Ratio
   |
   v
[ Deterministic Classifier ]
   |- Ignore impulse (your policy)
   |- Suppress mower/weedwhacker
   |- Suppress likely drive-bys / intermittent passers
   |- Prefer sustained continuous / music-like / bass-pulse-like
   |
   v
[ Incident State Machine ]
   |- Start incident log on threshold crossing
   |- Update peak dB
   |- Daytime: may trigger playback after persistence
   |- Night: record only
   |- Clear after sustained below-threshold
   |
   +--> [ SQLite Incident Log ]
   |
   +--> [ Playback Controller ]
           |- GPIO relay -> amp power/enable
           |- VLC playlist playback
           |- self-playback suppression
   |
   +--> [ FastAPI + Web UI ]
           |- Status
           |- Arm / Disarm
           |- Emergency Kill
           |- Export CSV
           |- Clear Log
           |- HA integration
```
