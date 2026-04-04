# Straightforward build checklist

## Phase 1: Bench setup
- [ ] Assemble Pi + SD + PSU
- [ ] Connect USB mic/interface
- [ ] Confirm input device visible via `python -c "import sounddevice as sd; print(sd.query_devices())"`
- [ ] Connect amp + speakers
- [ ] Connect relay to amp enable/power control (prefer low-voltage side)

## Phase 2: Install software
- [ ] Clone / unzip project
- [ ] Run `bash scripts/install.sh`
- [ ] Copy `config.example.yaml` -> `config.yaml`
- [ ] Set correct `input_device_name`
- [ ] Set `playlist_path`
- [ ] Set `amp_gpio_pin`
- [ ] Keep `playback.enabled: false` initially

## Phase 3: Calibration
- [ ] Place trusted SPL meter next to mic
- [ ] Generate stable reference sound
- [ ] Adjust `calibration_offset_db`
- [ ] Observe Web UI values
- [ ] Confirm daytime/nighttime threshold switching

## Phase 4: False-positive elimination
- [ ] Test lawn mower / weedwhacker conditions
- [ ] Test drive-by vehicles
- [ ] Test actual target nuisance source
- [ ] Increase `trigger_persist_seconds` until false positives stop
- [ ] Increase `clear_below_seconds` to avoid chatter
- [ ] Tune `music_min_spectral_flux`
- [ ] Tune `mower_max_flatness` / `mower_min_tonal_ratio`

## Phase 5: Safe activation
- [ ] Enable `playback.enabled: true`
- [ ] Start with low speaker volume
- [ ] Confirm response only during 7 AM–10 PM
- [ ] Confirm record-only during 10 PM–7 AM
- [ ] Test Emergency Kill from Web UI
- [ ] Test Home Assistant REST commands

## Phase 6: Long-run hardening
- [ ] Install systemd service
- [ ] Verify reboot persistence
- [ ] Set DB retention
- [ ] Export and review incidents weekly
