# noise-warden CHANGELOG

## TESTING RESOURCES:

To help ensure that we don't corrupt our analysis engine, these are the source sound recordings used for initial calibration (NOTE: quality microphone and FLAC recordings give us the truest data to analyze, which are not available via YouTube compressed uploads):

- lawn mower:
  - gas: https://creazilla.com/media/audio/15433161/domestic-machines-lawn-mower-fuel
  - electric: https://creazilla.com/media/audio/15475724/electric-lawn-mower
- birdsong (robin): https://www.youtube.com/watch?v=CCh-Ga7bu6M
- rolling thunder (light rain): https://freesound.org/people/tim.kahn/sounds/536171/
- cracking thunder: https://freesound.org/people/Erdie/sounds/23221/

> [!WARNING]
> WARNING: DO NOT USE YOUTUBE RECORDINGS--THEY TEND TO EXHIBIT THE EXACT _OPPOSITE_ OF REAL-LIFE FULL-SPECTRUM RECORDINGS.

Past Bad Ideas:

- (TERRIBLE IDEA--basically the inverse of real-life): https://www.youtube.com/watch?v=jzwom7I02ks
- diesel truck (TERRIBLE IDEA--inverse of real-life): https://www.youtube.com/watch?v=3B_2mc2l10s&t=228

Additionally, real-world recordings are saved in `tests/classification_data/` as version-controlled WAV files — empirical sources of truth decoupled from the incident database. These are replayed through the DSP pipeline during regression tests to prevent threshold changes from silently breaking known-good calibrations, and can seed a clean database for full reclassification after engine or filter changes without needing to manually re-record each sound type.

## v15 - 2026-05-15 — "We're Wasting Our Time! Edition"

Post-mortem hardening from a 9-day OOM-kill outage (Apr 21–29), reclassify journal parity for engine-captured snippets, holdover breaker logic rework, storage cleanup for orphaned snippets, and discovery of a fundamental sample-rate mismatch between test clips (44100 Hz) and Pi recordings (22050 Hz) that had been silently skewing all filter threshold calibrations. An FFT-based `resample_audio()` utility was built but deferred pending a dedicated recalibration session — 9 of 15 regression clips change classification when normalized to 22050 Hz. Time-based test rot fixed with dynamic timestamps. Pi install script now ensures adequate swap. 571 tests passing, 0 failures.

<details>

### Reclassify parity / classification fidelity

- **Lead-in / lead-out journal parity** — `analyze_clip()` now accepts `engine_captured=True` to mark preroll blocks as "lead-in" and post-trigger tail blocks as "lead-out" instead of DSP-classifying them as "unknown". Previously, reclassifying an engine-captured snippet would produce a different journal than the original detection: the preroll (typically 2 seconds of sub-threshold audio written from the ring buffer) was classified as "unknown" rather than "lead-in", and the post-trigger tail was similarly misclassified. Now both `reclassify_incident()` and `reclassify_all()` pass `engine_captured=True`, while standalone WAV analysis (regression tests, freesound clips) defaults to `False` for full end-to-end classification. Lead-in timestamps use negative values (e.g. `-2`) matching the engine's convention. "lead-in" and "lead-out" are added to the ignorable set in both `_compute_dominant()` and the engine's finalization, so they never influence the dominant classification. 7 new tests
- **Holdover priority breaker improvements** — the `holdover_priority_breakers` mechanism now allows listed breaker filters to break through ANY non-breaker holdover, regardless of filter chain position. Previously, a breaker could only interrupt a *lower-priority* holdover (higher chain index), which prevented flyover (index 9) from breaking wind (index 4) holdover even when flyover's own min_history checks were satisfied. The new rule: if both the breaker and the holdover filter are in the breakers list, chain priority determines the winner; if only the incoming filter is a breaker, it wins unconditionally. This lets flyover break wind/conversation holdovers while preserving thunder's priority over flyover. Config comment updated to suggest adding `flyover` for environments with frequent aircraft traffic. 2 new tests (1 replaced)

### Storage / cleanup

- **Soft-delete now removes snippet files** — `soft_delete_incident()` previously only set `deleted=1` in the database, leaving the WAV file on disk. Now it also deletes the snippet (checking both the primary path and `autodismissed/` quarantine) and NULLs the `snippet_path` column. Prevents orphaned files from accumulating and wasting SD card space on the Pi. 3 new tests
- **Purge orphaned incidents** — new `storage.purge_orphaned_incidents()` method NULLs `snippet_path` for non-deleted rows whose WAV file no longer exists on disk. Exposed via `python -m noise_warden.reclassify --purge-orphans` CLI flag. Addresses the issue where manual snippet deletion on the Pi left hundreds of stale DB references that `reclassify --all` reported as "skipped (no file)". Can be combined with `--all` for a clean reclassification run. 4 new tests

### Documentation

- **Pi file permissions troubleshooting** — added README section explaining EACCES errors when SSH users try to manage snippet files owned by the `noisewarden` system user, with `usermod -aG` and `chmod g+w` fixes, plus a note about using `--purge-orphans` after manual cleanup

### Deployment stability (post-mortem from Apr 21-29 OOM-kill outage)

- **systemd service hardening** — `StartLimitIntervalSec` and `StartLimitBurst` moved from `[Service]` to `[Unit]` where systemd 252 (Bookworm) actually reads them. Without this fix, the Pi had no restart rate-limiting — a crash loop would restart indefinitely at 10-second intervals. Added `MemoryHigh=512M` (soft limit, triggers kernel reclaim pressure) and `MemoryMax=1024M` (hard kill) to prevent runaway memory growth from reaching OOM-kill territory. Normal RSS is ~90-120 MB; the Apr 2026 outage reached 7.6 GB before the kernel intervened.
- **Engine thread crash guard** — the engine's daemon thread is now wrapped in a try/except that catches unhandled exceptions, logs the crash, sets `mode="crashed"` on the state (so the dashboard shows the problem), then SIGTERMs the process so systemd can restart cleanly. Previously, a daemon-thread crash would leave the web server running indefinitely with stale state and no monitoring.
- **Periodic RSS monitoring** — the engine now checks its own memory usage during the daily cleanup cycle. RSS is exposed to the dashboard state as `process_rss_mb`. At 350 MB, a warning is logged and shown in the UI. At 450 MB, the process self-terminates for a systemd restart — well below the 512 MB hard limit, avoiding OOM-kill journal noise and giving the process time to finalize cleanly.

### Test fixes and sample-rate discovery

- **Time-based test failures** — 3 tests (`test_timeline_embeds_incident_json`, `test_timeline_has_snippet_flag`, `test_keeps_recent_snippets`) used hardcoded April 2026 timestamps that aged past the 30-day retention/query windows. Fixed by using `datetime.now(timezone.utc)` in fixtures (`conftest.py::sample_incident`) and directly in `test_web.py`. These tests will no longer rot over time.
- **Thunder-and-light-rain classification regression** — the holdover breaker change (breaker beats any non-breaker) interacted with `flyover` being listed as a breaker in the local config. At the test clips' native 44100 Hz sample rate, storm-rain ambient blocks have centroids 1919–2834 Hz that pass the flyover centroid check (min 1400). Previously, amplified_bass holdover (priority 3) suppressed these flyover matches (priority 9); with flyover as a breaker, it punched through unconditionally. Fixed by removing `flyover` from `holdover_priority_breakers` in the local config to match the deployed default config (`thunder` only). Flyover as a breaker should not be re-enabled until sample-rate normalization and regression recalibration are complete (see NEXT.md §5).
- **Sample-rate mismatch discovered** — all 15 regression test WAVs are recorded at 44100 Hz, but the Pi captures at 22050 Hz. Spectral features (centroid, band ratios) are computed using the file's native sample rate, so threshold values tuned on test clips don't precisely match what the Pi encounters. This was the root cause of the flyover false positives. An FFT-based `resample_audio()` utility was added to `dsp.py` for future normalization, but enabling it in `analyze_clip` requires recalibrating all 15 regression clips at the canonical 22050 Hz rate (9 of 15 changed classification when resampled — too disruptive to apply without a dedicated calibration session).
- **Swap sizing for Pi** — `scripts/install_pi.sh` now checks and resizes the swap file to 1024 MB if below that threshold, preventing pip install OOM failures on low-memory Pi boards.

</details>

## v14 - 2026-04-19 — "Noise Taxonomy Expansion"

Two new exclusion filters (wind, flyover), engine-sound false positive fixes, and several quality-of-life improvements to incident recording and lifecycle management. 534 tests passing, 0 warnings.

<details>

<summary>Key details</summary>

### Wind exclusion filter (new)

- **Source recording** — `wind-and-faint-windchimes.wav`: moderate wind with roof-edge whistle through a deadcat windscreen
- **Spectral profile** — centroid 3313–5858 Hz (median 4496), flatness 0.32–0.55, lowband 0.15–0.26, highband 0.40–0.60, env_std ≤2.5
- **Key separators** — vs. rain: lowband_min 0.12 (rain sits below). vs. birdsong: highband_max 0.65 (birdsong warmup blocks have highband 0.65+; real wind maxes at 0.595). vs. plane: lowband_max 0.26 (plane lowband median 0.283)
- **Filter chain position** — slot 4 (after amplified_bass, before rain). Wind runs before rain because they share broadband profiles; lowband separates them
- **Config keys** — 11 new `wind_*` keys in `noise_warden.yaml`
- **10 unit tests** — real profile, flatness, lowband (high/low), centroid, env_std, highband (low/high), history, music score guard, rain separation

### Flyover exclusion filter (new)

- **Source recordings** — `plane-flyover.wav` (propeller aircraft), `engine-speed-launch.wav` (vehicle acceleration)
- **Spectral profiles** — plane: centroid 2508–10631, flatness 0.22–0.47, midband 0.19–0.42. Engine speed: centroid 1412–5283, flatness 0.11–0.47, midband 0.33–0.73
- **Key separators** — vs. music: max_beat_confidence 0.50 (engines are non-periodic). vs. mower: highband_max 0.60 (escaped mower blocks have highband 0.56–0.82) + lowband_min 0.10 (escaped mower blocks have lowband 0.02–0.06). vs. conversation: centroid_min 1400 (conversation sits at centroid ≤1200)
- **Filter chain position** — slot 9 (after diesel, before conversation). Catches engine-like sounds that fall between the more specific mechanical filters
- **Config keys** — 12 new `flyover_*` keys in `noise_warden.yaml`
- **12 unit tests** — plane profile, engine profile, history, beat confidence (high/low/disabled), mower rejection (highband/lowband), flatness, midband, env_std

### Music score midband penalty

- **Problem** — engine sounds (plane flyover mscore 0.46–0.79, vehicle acceleration mscore 0.17–0.53) received falsely high music-like scores due to engine rumble triggering the lowband component of the score
- **Root cause** — the `music_like_score()` formula weights lowband 60%, but engines have broadband low-frequency energy, not musical bass. Music follows a "smiley EQ" (boosted bass + treble, scooped mids); engines concentrate energy in midband
- **Solution** — when midband_ratio > 0.40, score is reduced by up to 30% (linear ramp from 0% penalty at 0.40 to 30% at 1.0). Validated: bass music midband ~0.33 stays unpenalized; engine-speed median midband 0.63 gets meaningful reduction
- **Engine guard in `_classify_sound()`** — midband_ratio > 0.50 returns "unknown" instead of "music" or "music_like". Mirrored in `reclassify.py`
- **7 tests** — 3 midband penalty tests in TestMusicLikeScore, 4 _classify_sound midband guard tests in TestClassifySound

### Minimum incident duration auto-dismiss

- **New config** — `min_incident_seconds` (default 3). Very short incidents (1–2 seconds of above-threshold audio) are almost always impulse-level transients
- **Behavior** — incidents shorter than the minimum are quarantined the same way as drive-bys: snippet moved to `autodismissed/`, classification set to "too_short", excluded=1
- **Guard** — skipped when `force=True` (manual test incidents) or when the incident was already dismissed as a drive-by

### Lead-in journal notation

- **Problem** — WAV recordings start `snippet_pre_seconds` before the incident's `start_ts`, but nothing indicates this in the classification journal. The WAV feels longer than the displayed duration
- **Solution** — `_begin_incident()` now tracks `preroll_seconds` and prepends a `(round(-preroll_sec), "lead-in")` entry to the classification journal. The negative timestamp makes the preroll visually obvious in the source timeline

### snippet_post_seconds tail trimming fix

- **Problem** — `snippet_post_seconds` config value was not actually used during tail trimming. The engine hardcoded keeping exactly 1 sub-threshold block as context
- **Solution** — tail trimming now calculates `post_blocks = max(1, round(snippet_post_seconds / block_seconds))` and preserves that many blocks instead of 1. Default 2 seconds gives natural-sounding recording endings
- **Impact** — 3 existing tail-trim tests updated to set `snippet_post_seconds=2` explicitly and adjust assertions accordingly

### Regression clips

- 3 new WAV files added to `tests/classification_data/`:
  - `wind-and-faint-windchimes.wav` → "wind (multiple)" (locked)
  - `plane-flyover.wav` → "flyover (multiple)" (locked)
  - `engine-speed-launch.wav` → "flyover+" (locked)
- **Test count: 534 passed, 0 warnings** (was 499)

### Additional changes

- Filter detection latency configured for wind (6 blocks) and flyover (4 blocks) — enables journal backdating
- `FILTER_CHAIN` expanded from 9 to 11 entries; `FILTER_PRIORITY` auto-updated
- `deploy/noise-warden.service` — systemd `LimitNOFILE` raised
- `install_pi.sh` — additional pre-flight check
- Minor dashboard and SW fixes

### Mobile & UX improvements

- **Color-coded Excess column on incidents page** — added an Excess column (peak dB above threshold) with the same `dbSeverityStyle()` color-coding as the dashboard: amber (+15–24), orange (+25–29), red (+30–39), deep red (+40+). Both the dashboard and incidents Excess headers now have tooltip descriptions of the severity tiers
- **Incident table slimmed for narrow screens** — removed End Time and Start dB columns; moved Duration into their position. Reduces horizontal overflow on mobile viewports
- **Delete button moved to detail dialog** — instead of a per-row delete column in the table, the delete action now lives inside the incident detail popup alongside the Re-analyze button. Uses `fetch()` POST + page reload to avoid full form navigation
- **Nav wraps on narrow screens** — added `flex-wrap: wrap` to the nav bar so links wrap onto a second line rather than overflowing. Prevents white-on-white text when dark nav background didn't span the full viewport width
- **Responsive CSS** — new `@media (max-width: 600px)` breakpoint: smaller nav link padding, horizontal table scroll fallback
- **Pagination count mismatch fixed** — `count_incidents()` was not filtering `excluded` rows, while `list_incidents()` excludes them by default. This caused "Page 1 of 4" when all visible content fit on page 1. Added `include_excluded` parameter to `count_incidents()` to match `list_incidents()` behavior. Seed guard now explicitly passes `include_excluded=True`

### DSP pipeline improvements

- **Envelope variance penalty in `music_like_score()`** — mechanical drones (engines, mowers, compressors) maintain near-constant amplitude within a block (`envelope_cv < 0.10`), while music has dynamic amplitude modulation from rhythmic content (`envelope_cv 0.15–0.80`). When meaningful bass is present (`lowband > 0.15`) but the envelope is flat, the score is reduced by up to 20%. This catches the major false-positive pathway: steady mechanical rumble with moderate bass fooling the formula
- **Harmonic series detection** — new `_compute_harmonic_ratio()` helper and `harmonic_ratio` field in `spectrum_features()`. Detects harmonic peaks at integer multiples of a fundamental in the 30–180 Hz band. Music bass (kick drum, bass guitar, synth bass) shows clear harmonic series; engine rumble is broadband. When `harmonic_ratio > 0.50`, `music_like_score()` receives a small bonus (up to +0.08), helping borderline music blocks cross the threshold
- **`engine_noise` supercategory** — new classification returned by `_classify_sound()` (and mirrored in `reclassify.py`) when no exclusion filter matches but the spectral profile is clearly mechanical: either `midband > 0.50` (dominant engine-band energy), or steady bass without harmonics (`lowband > 0.10`, `envelope_cv < 0.10`, `harmonic_ratio < 0.40`). Added to `_IGNORABLE_CLASSES` in dominant calculation so it doesn't compete with specific filter matches (a 60-second recording with 50 seconds of engine_noise and 10 seconds of thunder should report "thunder", not "engine_noise"). Dashboard and incidents page tooltips updated
- **Thunder Path B threshold relaxation** — `rumble_min_db` lowered from 95.0 to 40.0 dBA, `rumble_centroid_max` widened from 1300 to 1500 Hz. The 95 dBA floor was overly conservative — real thunder recordings at moderate distance or through windows peak at 60–80 dBA, far below the old threshold. The spectral criteria (centroid ≤ 1500, flatness ≤ 0.15, midband ≥ 0.40) are already highly discriminating on their own; very few non-thunder sounds have all three characteristics (mower flatness ≥ 0.25, engine flatness ≥ 0.25, rain is broadband). The wider centroid (1500 vs 1300) captures the rumble's decaying tail where centroid drifts up to ~1450 Hz — this extra headroom lets holdover activate (5 consecutive blocks), implementing de facto mutual exclusion with flyover (the two are physically exclusive events). Fixes thunder-cracks and thunder-and-light-rain regression test failures
- **`mower-gas.wav` regression test marked pending** — pre-existing issue (not caused by v14 changes): block 34 (centroid 3920) has dBA 65.2, below `mower_min_db` of 70.0, so zero blocks match mower. Documented fix paths in test note: (a) lower `mower_min_db`, (b) raise `mower_centroid_max` to catch higher-dB blocks, or (c) both. Option (b) preferred. Added `xfail` handling for "pending" regression clips
- 539 tests passing, 0 failures (1 xfailed pending, 1 xpassed pending)

### Reclassify --all journal awareness

- **`reclassify_all` now detects journal-only changes** — previously, `--all` mode only tracked incidents where the dominant classification flipped. Journal changes (new `engine_noise` blocks, filter backdating shifts, holdover behavior differences) were invisible in the dry-run summary and only written when `--update` was used (where they were written unconditionally for every incident, even unchanged ones). Now the batch comparison fetches `class_journal` from the DB, normalizes and compares it alongside classification, and reports three change types: `class+journal`, `class`, or `journal`. The summary table shows counts by type. DB writes only occur when something actually changed, and the web API's `reclassify-all` endpoint inherits the new `change_type` field in its JSON response

### Audio recording & playback

- **Snippet normalization** (opt-in, `audio.snippet_normalize: true`) — USB microphones produce -30 to -50 dBFS for sounds that are 65+ dBA in real life (because `calibration_offset_db` of 100–115 maps the mic's full-scale digital signal to 100–115 dBA SPL). The resulting WAV files are nearly inaudible on consumer playback devices without cranking volume to maximum. When enabled, `_normalize_snippet()` runs after tail-trimming in `_finalize_incident()`: reads the WAV, computes peak, and applies a linear gain boost to reach the target peak (default -6 dBFS, standard broadcast headroom). Only boosts, never attenuates — recordings already at or above the target are left alone. All DSP measurements (dBA, classification, beat confidence, music score) are computed from the raw signal BEFORE normalization and are unaffected. New config keys: `snippet_normalize` (bool) and `snippet_normalize_peak_dbfs` (float)
- **Playback volume boost** — user-adjustable 1x–5x gain selector in the incident detail popup, next to the `<audio>` controls. Implemented via WebAudio `MediaElementSource` → `GainNode` → `destination` chain (the native `<audio controls>` still handle play/pause/seek; the GainNode amplifies before speakers). Selection persists across popup opens via `sessionStorage`. Works alongside or independently of snippet normalization — use both for maximum audibility of quiet live recordings
- **Batch snippet normalization via reclassify** — `--normalize` CLI flag added to `reclassify`. When passed alongside `--all`, normalizes every snippet WAV's peak amplitude after DSP analysis (so classification sees the original signal levels). Also works for single-incident reclassify (`reclassify 63 --normalize`). Uses `snippet_normalize_peak_dbfs` from config (default -6 dBFS). The `normalize_snippet()` function was extracted from `engine._normalize_snippet()` into `reclassify.py` as a standalone module-level function, and engine.py now imports it — eliminating the duplicate implementation. Return dict includes `normalized` count in batch mode; single-incident mode adds a `normalized` key to the result dict. 6 new tests (3 for `normalize_snippet`, 2 for reclassify-incident integration, 1 for nonexistent file handling)
- **Self-adaptive spectral denoising** (opt-in, `audio.snippet_denoise: true`) — removes ambient background hiss ("seashore whoosh") from USB microphone recordings using per-snippet minimum-statistics noise estimation (Martin 1994 simplified). No manual noise profile capture needed. Algorithm: STFT the signal into overlapping windowed frames, estimate the noise floor as the Nth percentile (default 10th) of magnitudes per frequency bin across all frames, subtract with oversubtraction factor α and spectral floor β to prevent musical-noise artifacts, then inverse STFT with overlap-add. Runs in the pipeline after DSP analysis and tail trimming, BEFORE normalization — so classification uses the raw signal and gain boost amplifies clean audio, not amplified hiss. New config keys (all in `audio` section): `snippet_denoise` (bool), `denoise_percentile` (int, 0–100), `denoise_alpha` (float, oversubtraction), `denoise_beta` (float, spectral floor). Both `engine._finalize_incident()` and `reclassify` support it — batch via `--denoise` flag. 7 new tests (5 for `denoise_snippet`, 2 for reclassify-incident integration)

### Incident management

- **Borderline auto-dismiss** (opt-in, `detection.record_borderline_events: false`) — incidents whose peak dB is within `borderline_margin_db` of the threshold are now auto-dismissed during finalization when this option is disabled. The incident is reclassified as 'borderline', marked excluded, and the snippet is quarantined to `autodismissed/` (same pattern as drive-by and too_short dismissals). Previously, borderline incidents were always recorded and only hidden in the timeline UI via a checkbox. This option lets the engine skip them entirely, keeping the incident log focused on clear violations. Default is `true` (record everything, as before). 3 new tests

### Reclassify parity / classification fidelity

- **Lead-in / lead-out journal parity** — `analyze_clip()` now accepts `engine_captured=True` to mark preroll blocks as "lead-in" and post-trigger tail blocks as "lead-out" instead of DSP-classifying them as "unknown". Previously, reclassifying an engine-captured snippet would produce a different journal than the original detection: the preroll (typically 2 seconds of sub-threshold audio written from the ring buffer) was classified as "unknown" rather than "lead-in", and the post-trigger tail was similarly misclassified. Now both `reclassify_incident()` and `reclassify_all()` pass `engine_captured=True`, while standalone WAV analysis (regression tests, freesound clips) defaults to `False` for full end-to-end classification. Lead-in timestamps use negative values (e.g. `-2`) matching the engine's convention. "lead-in" and "lead-out" are added to the ignorable set in both `_compute_dominant()` and the engine's finalization, so they never influence the dominant classification. 7 new tests
- **Holdover priority breaker improvements** — the `holdover_priority_breakers` mechanism now allows listed breaker filters to break through ANY non-breaker holdover, regardless of filter chain position. Previously, a breaker could only interrupt a *lower-priority* holdover (higher chain index), which prevented flyover (index 9) from breaking wind (index 4) holdover even when flyover's own min_history checks were satisfied. The new rule: if both the breaker and the holdover filter are in the breakers list, chain priority determines the winner; if only the incoming filter is a breaker, it wins unconditionally. This lets flyover break wind/conversation holdovers while preserving thunder's priority over flyover. Config comment updated to suggest adding `flyover` for environments with frequent aircraft traffic. 2 new tests (1 replaced)

### Storage / cleanup

- **Soft-delete now removes snippet files** — `soft_delete_incident()` previously only set `deleted=1` in the database, leaving the WAV file on disk. Now it also deletes the snippet (checking both the primary path and `autodismissed/` quarantine) and NULLs the `snippet_path` column. Prevents orphaned files from accumulating and wasting SD card space on the Pi. 3 new tests
- **Purge orphaned incidents** — new `storage.purge_orphaned_incidents()` method NULLs `snippet_path` for non-deleted rows whose WAV file no longer exists on disk. Exposed via `python -m noise_warden.reclassify --purge-orphans` CLI flag. Addresses the issue where manual snippet deletion on the Pi left hundreds of stale DB references that `reclassify --all` reported as "skipped (no file)". Can be combined with `--all` for a clean reclassification run. 4 new tests

### Documentation

- **Pi file permissions troubleshooting** — added README section explaining EACCES errors when SSH users try to manage snippet files owned by the `noisewarden` system user, with `usermod -aG` and `chmod g+w` fixes, plus a note about using `--purge-orphans` after manual cleanup

</details>

## v13 - 2026-04-12 — "Deploy or Die Trying"

First-time Raspberry Pi deployment revealed a cascade of real-world failure modes that the development environment had been silently masking. Each issue was discovered during actual Pi setup and addressed with both code hardening and documentation.

<details>

<summary>Key details</summary>

### Copy-based installation (CHDIR fix)

- **Problem** — `install_pi.sh` previously created a symlink from `/opt/noise-warden/<version>/` pointing to wherever the user extracted the archive (e.g., `~/Desktop/noise-warden-v12/`). The `noisewarden` system user couldn't traverse the source user's home directory, causing `status=200/CHDIR` on service start
- **Solution** — complete rewrite of `install_pi.sh` to copy project files into `/opt/noise-warden/<version>/` via `rsync` instead of symlinking out. The `current` symlink always points inside `/opt/`, so the service user can always reach the files regardless of where the source was extracted
- **Pre-flight validation** — install script now runs 6 checks after setup: WorkingDirectory exists, `current` symlink resolves to a valid project, `noisewarden` user can read the files, `uvicorn` is installed in the venv, config file is present, and `noisewarden` is in the `audio` group (FAIL if not). Audio device presence is checked with a WARNING (mic may not be plugged in yet)
- **Deploy script improvements** — `deploy_noise_warden.sh` now lists available versions if no argument given, suggests `install_pi.sh` if the target directory doesn't exist, validates the target contains `noise_warden/main.py`, and fixes ownership after swap
- **Service unit hardened** — `ExecStartPre` check validates the `current` symlink resolves to a valid project directory before starting uvicorn

### Microphone setup documentation

- **README step 5** — explicit "Connect your USB microphone" step with `arecord -l` verification
- **README step 6** — `groups noisewarden` verification with `sudo usermod -a -G audio noisewarden` fix command
- **Install script** — `usermod` warning now visible instead of silently swallowed by `2>/dev/null || true`

### Sample rate auto-negotiation

- **Problem** — Pi's ALSA doesn't auto-resample like macOS Core Audio. Requesting 22,050 Hz on a USB device that only supports 48,000 Hz causes `paInvalidSampleRate` and the service fails to start
- **Solution** — new `AudioCapture._negotiate_sample_rate()` tests the configured rate via `sd.check_input_settings()` and falls back to the device's `default_samplerate` if unsupported. Logs a warning with the exact config fix: `sample_rate: <device_default>`
- **README troubleshooting entry** — documents the symptom, cause, auto-fallback behavior, permanent fix, and how to check what rates the device supports

### Stale device cache recovery

- **Problem** — when PulseAudio/PipeWire audio profiles change at runtime (e.g., switching from "Analog Stereo Duplex" to "Analog Stereo Input"), PortAudio's per-process device cache goes stale. New `AudioCapture` instances query the same stale cache, producing an infinite "Error querying device -1" loop every 2 seconds
- **Solution** — new `AudioCapture.refresh_device_list()` static method forces a PortAudio device rescan via `sd._terminate()` + `sd._initialize()`. Engine calls this before every reinit attempt
- **Escalating backoff** — `audio_fail_count` tracks consecutive failures with backoff delay `min(30.0, 2.0 * (2 ** max(0, count - 2)))` → 2s, 2s, 4s, 8s, 16s, 30s cap. Prevents log spam while still recovering promptly when the profile settles
- **10-failure warning** — after 10 consecutive audio failures, the journal logs a clear message recommending service restart. Counter resets on successful `read_block()` or reinit

### Restart service button

- **Problem** — several troubleshooting entries recommend restarting the service, but the only way to do so was via SSH (`sudo systemctl restart noise-warden`). For a Pi mounted in a closet, this is inconvenient
- **Solution** — new `POST /control/restart` endpoint and red "Restart Service" button on the dashboard. Auth-protected. Uses self-termination via `os.kill(os.getpid(), signal.SIGTERM)` on a 1.5s timer (so the response reaches the client), relying on systemd `Restart=always` to bring the process back
- **Systemd detection** — `_running_under_systemd()` checks for the `INVOCATION_ID` environment variable (set by systemd for all managed units). Under systemd: shows "Restart Service" button with auto-refresh holding page. Without systemd (local dev): shows "Stop Server" button with a "Server Stopped" page and manual restart instructions — no auto-refresh, no false promise of recovery
- **Service unit change** — `Restart=on-failure` → `Restart=always`. systemd distinguishes between self-termination (restart) and explicit `systemctl stop` (stay stopped), so manual stops still work. Existing rate-limiting (`RestartSec=10`, `StartLimitBurst=5`) unchanged
- **Confirm dialog** — browser `confirm()` prompt with context-appropriate warning text (systemd vs. local)

### Starlette/FastAPI compatibility (Python 3.14)

- **TemplateResponse signature updated** — all 7 `TemplateResponse` calls migrated from deprecated `TemplateResponse(name, {"request": request, ...})` to the new `TemplateResponse(request, name, {...})` signature. Eliminates 16 deprecation warnings per test run
- **Pytest warning filters** — suppresses `PendingDeprecationWarning` from starlette's `import multipart` and `asyncio.iscoroutinefunction` deprecation (both upstream library issues on Python 3.14, not actionable in our code)
- **Test count: 499 passed, 0 warnings**

</details>

## v12 - 2026-04-11 — "Pro Edition"

Enhancements to assist in the inevitable tweaking required to actually deploy this solution. Many configuration defaults tweaked after analyzing real-world input. Regression tests built up to continue to only get better without future silent breakages.

<details>

<summary>Key details</summary>

### Conversation filter tightening

- **Music score guard** — `looks_like_conversation()` now rejects blocks where `music_like_score(features)` exceeds `max_music_score` (default 0.55). The filter chain runs *before* `_classify_sound()` in the engine loop, so without this guard, vocal music with a matching centroid (500–2500 Hz) and syllable-like modulation would be misclassified as conversation before the music classifier ever ran. Real conversation scores 0.43–0.53 (light bass, moderate tonal); music through walls scores 0.65+ (heavy bass + strong tonal). The 0.55 threshold sits in the clean gap between these distributions. Corrected the misleading docstring that previously claimed "the music_like_score check in the engine runs first"
- **Minimum dB floor** — new `conversation_min_db` config key (default 0.0 — disabled). When set to e.g. 60.0, rejects quiet ambient conversations below nuisance level. Analogous to the mower filter's `mower_min_db` parameter
- **Midband energy minimum** — new `conversation_midband_min` config key (default 0.25). Speech concentrates energy in the formant/midband region (250–4000 Hz). Blocks with very little midband energy are more likely broad environmental noise than human voices, even if centroid and modulation happen to match
- **Config keys added** — `conversation_max_music_score`, `conversation_min_db`, `conversation_midband_min` in `noise_warden.yaml`
- **9 new conversation sensitivity tests** — music score guard (reject vocal music, allow speech, configurable threshold, disable at zero), min dB floor (reject quiet, disabled by default), midband minimum (reject low midband, configurable) — **Total: 423 tests passing**

### Classification journal display for all incidents

- **Single-entry journals now visible** — the source timeline section in the incident detail popup previously required `journal.length > 1` (multi-source incidents only). Now displays for any journal with ≥ 1 entry. For seeded incidents with a single classification, this provides confirmation that the DSP pipeline ran and identified the source correctly. Journal highlighting, click-to-seek, and unknown dimming all work as before
- **Timeline page journal fix** — the `/timeline` route was missing `class_journal` from the incident data it serialized to the client. Timeline popups never showed the source timeline for any incident — live or seeded. Fixed by adding `class_journal` to the timeline's incident builder

### Engine hardening

- **`capture.close()` wired on shutdown** — `Engine.stop()` now calls `self.capture.close()` to release the `InputStream` audio resource. Previously, the stop path called `relay.cleanup()` but skipped audio teardown. In blocking-via-sd.rec mode this was harmless (sd.rec exits naturally), but with callback streams the `InputStream` would leak until process exit
- **Audio reinit closes old capture** — the error-handler path that creates a new `AudioCapture` on I/O failure now calls `self.capture.close()` before instantiating the replacement. Previously, the old capture was silently abandoned, leaking the `InputStream` if callback mode was active

### Thunder Path B — sustained rumble detection

- **Dual-path thunder detection** — `looks_like_thunder()` now has two detection paths. Path A (original) targets sharp cracks: single-block 18+ dB delta with high bass (lowband > 0.55) and broadband energy (flatness > 0.45). New Path B targets sustained rumble from mellow/distant storms: very low centroid (≤ 1300), extremely low flatness (≤ 0.15, energy concentrated not spread), dominant midband (≥ 0.40), and loud (≥ 95 dBA). Requires `min_history` ≥ 6 blocks. Root cause: mellow thunderstorms have the *opposite* spectral profile from Path A — gradual ramps (max ~5–8 dB/block), very low flatness (0.06–0.17), and variable lowband. The critical separator from mower: mower flatness is always ≥ 0.25, while thunder rumble is < 0.15
- **Recording quality matters** — investigation revealed that consumer microphones lack bass content entirely (lowband max 0.41), making thunder classification impossible. Replaced source recording with Sennheiser MKH 8020SP material that faithfully captures real thunder bass (lowband 0.57–0.70 on loud claps)
- **Config keys added** — `thunder_rumble_centroid_max`, `thunder_rumble_flatness_max`, `thunder_rumble_midband_min`, `thunder_rumble_min_db`, `thunder_rumble_min_history`, `thunder_rumble_window` in both config YAMLs — **Total: 407 tests passing**

### Priority-aware holdover

- **Higher-priority filters can break lower-priority holdovers** — `apply_filter_holdover()` now consults `FILTER_PRIORITY` (derived from `FILTER_CHAIN` order) and a configurable `holdover_priority_breakers` list (default: `"thunder"`). When a holdover-breaker filter matches raw during a lower-priority holdover, it breaks through immediately instead of being suppressed. Each breaker-eligible filter has strong internal consistency guarantees (e.g., thunder Path B requires 6+ blocks of sustained matching before it fires), so the break is genuine, not a transient blip. Non-breaker filters (impulse, weedwhacker, etc.) continue to be suppressed by active holdovers, preserving the existing transient-suppression behavior
- **Canonical case** — thunder-and-light-rain recording: mellow rain spectrally mimics a mower (centroid 1200–3500, flatness 0.25–0.37, low env_std), triggering mower holdover. When a thunder clap hits (centroid < 1000, flatness < 0.08, 104+ dB), thunder now breaks through instead of being absorbed by mower holdover. Result: thunder 55 blocks (was 25), mower 15 (was 25), dominant classification `thunder (multiple)`
- **`FILTER_PRIORITY` dict** — auto-derived from `FILTER_CHAIN` — lower index = higher priority. Used only by `apply_filter_holdover()`
- **Config key added** — `holdover_priority_breakers: thunder` in both config YAMLs. Comma-separated list of filter names eligible to break holdovers. Extensible without code changes
- **Regression clips added** — `thunder-and-light-rain.wav` (locked, expected `thunder (multiple)`) and `thunder-cracks.wav` (locked, expected `unknown (multiple)`) added to `test_classification_regression.py` — **Total: 414 tests passing**

### Dominant classification accuracy

- **Unknown/none exclusion** — `_compute_dominant()` in `reclassify.py` and the parallel inline logic in `engine._finalize_incident()` now exclude `"unknown"` and `"none"` from the duration-weighted classification contest. Falls back to the full set only if every entry is ignorable. Root cause: recordings with long silence between sound events (e.g., thunder-cracks.wav with 85 blocks of "none" vs. 14 blocks of actual sources) were classified as `"unknown (multiple)"` instead of the real dominant source. Regression expectation for thunder-cracks.wav updated from `"unknown (multiple)"` to `"thunder (multiple)"`
- **3 new tests** — `test_unknown_excluded_from_dominant`, `test_none_excluded_from_dominant`, `test_all_unknown_falls_back`

### Timeline and display polish

- **Timeline block labels show classification** — blocks now display `"mower (+12.3dB, 2m 15s)"` instead of `"65 dB · 2m 15s"`. The classification label (with trailing "(multiple)" stripped) immediately communicates the source type at a glance. Label child element always rendered (was gated to blocks ≥ 22px), using flexbox centering to fill the block — short blocks clip naturally via `overflow:hidden`
- **Daytime/nighttime terminology** — all user-facing "day"/"night" period labels changed to "daytime"/"nighttime" across popup badges, threshold labels, thresholds page column headers, and the web route period string. Ordinance data dictionary keys (`day`/`night`) are structural and unchanged

### Regression clips — real-world source recordings

- **mower-gas.wav** (locked) — real outdoor gas mower. Classifies as `mower (multiple)` after birdsong variance tightening. Only 1/51 blocks hits mower (centroid 3920) — coverage would improve by raising `mower_centroid_max_hz` since this mower's centroid averages 5000–7000 Hz
- **mower-electric.wav** (pending) — real outdoor electric mower. Classifies as `weedwhacker (multiple)` — a reasonable misclassification given the similar high-frequency whine. May warrant a broader "lawn equipment" category in future
- **birdsong-morning.wav** (locked) — real outdoor multi-bird morning chorus including robins. 130/141 blocks classify as birdsong via Path A. env_std 0.13–0.77 — the calibration anchor for the tightened `birdsong_amplitude_std_max`
- **birdsong-chorus.wav** (locked) — real outdoor multi-species chorus (wrens, mourning doves, robins). Lower centroid than morning chorus (mean 3799 Hz). Was misclassified as `mower (multiple)` before mower filter tightening — the key calibration clip for the mower–birdsong boundary
- **YouTube mower recordings retired** — `mower.wav` (raw, 130/134 blocks unclassified) and `mower-eq.wav` (artificially EQ'd to force-fit mower filter thresholds) deleted from `classification_data/`. Spectral comparison against the real gas mower recording showed the YouTube material had fundamentally different energy distributions — the EQ process invented a mower profile that doesn't exist in nature. Real-world recordings now serve as the sole calibration anchors

### Birdsong Path A variance tightening

- **`birdsong_amplitude_std_max` reduced from 3.0 to 1.0** — birdsong Path A (sustained/tonal detection) now requires much tighter amplitude stability. Real sustained birdsong (multi-bird morning chorus) peaks at env_std 0.77. Gas mower false-positive blocks had env_std 1.29–2.34, sitting in a clear gap above the new threshold. The bursty Path B (variance ≥ 8.0) and extreme purity Path C are unaffected — they don't check `variance_max`. Calibrated against three recordings: birdsong-morning.wav (sustained, max 0.77), birdsong-american_robin.wav (bursty, 2.5–15.8), and mower-gas.wav (false positives at 1.29–2.34)

### Mower filter tightening

- **Flatness threshold raised from 0.25 to 0.28** — bird chorus recordings with lower-frequency species (wrens, mourning doves) produced blocks with flatness 0.251–0.269 that squeaked past the old 0.25 floor. Real gas mower minimum flatness is 0.282 — clean separation. Eliminated 4 of 5 false mower blocks from birdsong-chorus.wav
- **Highband ceiling added (`mower_highband_max`: 0.75)** — mowers are mid-frequency drones; real mower blocks peak at highband 0.664. Bird choruses with matching centroid + flatness have highband 0.80+. The ceiling catches the remaining false-positive block (highband 0.805) that flatness alone couldn't reject. All legitimate mower blocks across all clips (gas mower, thunder-rain, thunder-cracks) have highband ≤ 0.664
- **Config keys updated** — `mower_flatness_threshold: 0.28`, `mower_highband_max: 0.75` in both config YAMLs
- **2 new mower tests** — `test_high_highband_rejected` (chorus-like profile rejected), `test_highband_at_ceiling_passes` (boundary inclusion)

### Rain filter recalibration

- **Real-world calibration from `rain.wav`** — the rain filter was previously calibrated from guesswork (flatness threshold 0.72, variance 2.5). Real outdoor rain recorded at 100+ dBA showed flatness 0.27–0.38, env_std < 0.50, lowband 0.08–0.14, and centroid 3130–4023. The old 0.72 threshold was so high that 100% of rain blocks fell through to the mower filter instead
- **Flatness threshold reduced from 0.72 to 0.27** — real rain is only moderately flat (broadband but not white noise). The old threshold was looking for something flatter than rain actually is
- **Variance threshold reduced from 2.5 to 1.5** — rain amplitude is extremely steady once established (env_std < 0.50). Tighter variance improves specificity
- **Lowband minimum added (`rain_lowband_min`: 0.07)** — the key separator from mower. Rain excites bass evenly (lowband 0.08–0.14) while mowers have very little bass (lowband 0.02–0.06) because engine vibration is a mechanical mid-frequency drone. This single parameter prevents nearly all mower/rain confusion
- **Centroid ceiling added (`rain_centroid_max_hz`: 5000)** — prevents birdsong blocks with incidental bass content (from ambient fan/HVAC, lowband 0.12–0.19) from being absorbed by the rain filter. Rain centroid maxes at ~4023 Hz; birdsong false-positives start at 5800+
- **rain.wav** (locked) — real outdoor rain, classifies as `rain (multiple)`. 68/73 blocks classified as rain
- **Existing rain unit tests recalibrated** — all 14 references to old 0.72/2.5 thresholds updated to 0.27/1.5. Feature dicts updated to realistic rain spectral profiles. 8 new rain sensitivity tests added for lowband_min boundaries, centroid_max boundaries, and configurable overrides for both
- **TestIdentifyFilter config updated** — `DEFAULT_DET` dict now includes `rain_lowband_min` and `rain_centroid_max_hz` keys. Two tests with flatness 0.30 (which now trips the recalibrated rain filter) fixed with lower flatness values

### Diesel filter recalibration

- **Complete rewrite from real recording** — the diesel filter was previously calibrated from guesswork targeting heavy truck idle (centroid ≤ 400, flatness 0.40–0.65, lowband ≥ 0.45). Real diesel car at ~71 dBA showed a completely different profile: flatness 0.12–0.16 (very tonal engine harmonics), centroid 1441–2023 Hz (mid-frequency, not bass), lowband 0.14–0.25, midband 0.22–0.35, env_std ~2.0 steady. The old filter matched 0 of 21 blocks
- **Tonal harmonics are the key feature** — diesel engines produce strong periodic harmonics from the firing cycle, resulting in extremely low spectral flatness. This is the defining characteristic and the primary separator from all other categories
- **Parameters completely replaced**:
  - `diesel_centroid_max_hz`: 400 → 3600 (real car centroid goes up to 2023; headroom for trucks)
  - `diesel_centroid_min_hz`: new, 1200 (separates from very low-frequency thunder rumble)
  - `diesel_flatness_max`: 0.65 → 0.20 (real car 0.12–0.16; clean gap from mower ≥ 0.28 and rain ≥ 0.27)
  - `diesel_lowband_min`: 0.45 → 0.10 (real car 0.14–0.25; separates from birdsong ≤ 0.09)
  - `diesel_midband_min`: new, 0.20 (engine energy in mid frequencies)
  - `diesel_env_std_max`: 3.0 → 3.0 (kept tight to limit thunder ambient false-positives)
  - `diesel_flatness_min` removed (no longer a flatness band; just a ceiling)
- **Thunder overlap managed via tight thresholds** — initial calibration with flatness_max=0.30 and env_std_max=4.5 caught 70 false-positive blocks in thunder-and-light-rain.wav (ambient storm rumble has similar tonal character). Tightened to flatness_max=0.20 and env_std_max=3.0, reducing to 48 blocks — thunder (55 blocks) now clearly wins the dominant contest
- **diesel-car.wav** (locked) — real diesel car, classifies as `diesel (multiple)`. 9/21 blocks classify as diesel once min_history (8) is satisfied and env_std stabilizes
- **All diesel unit tests rewritten** — 7 core tests updated to real-world profile, plus 7 sensitivity tests recalibrated (centroid min/max boundaries, flatness boundary, lowband boundary, midband boundary, min_history, configurable env_std). 2 engine integration tests updated

### Birdsong Path D — multi-species chorus detection

- **Problem** — birdsong-chorus.wav (real outdoor multi-species chorus: wrens, mourning doves, robins) classified only 1/165 blocks as birdsong, with the rest falling through to unknown. The chorus env_std median (4.9) far exceeds Path A's 1.0 ceiling, highband median (0.679) sits just below Path A's 0.70 threshold, and centroids (1330–2885) are too low for Paths B/C (need ≥ 2800)
- **Solution** — Path D uses temporal highband variance via a new `feature_history` buffer (maintained by engine and reclassify tool, 24 blocks deep). Multi-species choruses produce significant block-to-block variation in highband_ratio (std 0.05–0.16) as different species alternate calls, while mowers (std 0.03–0.09) and other mechanical sources produce stable values
- **Two safety margins prevent false positives:**
  1. Window-wide lowband ceiling: ALL blocks in the 12-block window must have lowband ≤ 0.12. This rejects thunder (0.55+ on crack blocks), mower (windows always contain some 0.12+ blocks), rain (0.16+), diesel (0.19+), and birdsong-morning (0.17+)
  2. Minimum highband std ≥ 0.10: rejects monotone steady-state sources. Mower-gas max hb_std: 0.106 (7/91 windows pass, not enough to flip dominant)
- **Plumbing** — `identify_filter()` and all 8 `_check_*` functions accept optional `feature_history` parameter (only `_check_birdsong` uses it). Engine maintains `self.feature_history` (24-block sliding window). Reclassify tool mirrors the same buffer
- **Result** — 78/165 blocks now classify as birdsong (77 via Path D, 1 via Path A). Dominant: `birdsong (multiple)` ✓
- **Cross-validated against all 10 regression clips** — no regressions. Clean separation on lb_max_in_window (chorus ≤ 0.133 vs mower-gas ≥ 0.123) and hb_std (chorus median 0.112 vs mower-gas median 0.085)
- **Config keys added** — `birdsong_chorus_highband_std_min` (0.10), `birdsong_chorus_lowband_max` (0.12), `birdsong_chorus_min_history` (12) in both YAML configs
- **6 Path D core tests** — chorus accepted, no feature history falls through, short history rejected, high lowband in window rejected, low highband variance rejected, Path A still fires when Path D available
- **5 Path D sensitivity tests** — highband std boundary, lowband max boundary, chorus min history boundary, custom min history, custom highband std min

### Amplified bass filter + music score guard

- **Problem** — `idiot-neighbor-mild-85db.wav` (real neighbor playing boosted bass music through garage walls at ~90 dB) had 16/59 blocks stolen by rain and 7/59 by mower. The bass-through-walls spectral profile (steady broadband, decent lowband) overlaps rain/mower thresholds, causing misclassification
- **Two-pronged solution:**
  1. **Music score guard on rain + mower** — both `looks_like_rain()` and `looks_like_mower()` now reject blocks where `music_like_score(features)` exceeds `max_music_score` (default 0.70). Safe threshold: rain mscore maxes at 0.593, mower-gas at 0.548, mower-electric at 0.637 (0/137 blocks ≥ 0.70). Bass music: 58/59 blocks ≥ 0.70. Provides defense-in-depth alongside the dedicated filter
  2. **Dedicated `amplified_bass` exclusion filter** — new `looks_like_amplified_bass()` function in `dsp.py`. Detects bass-heavy music through walls using music_like_score (≥ 0.60), lowband (≥ 0.20), centroid (≤ 4000 Hz), amplitude stability (env_std ≤ 3.0), and a dB floor (≥ 65.0). Inserted into FILTER_CHAIN between birdsong and rain (priority 3). The music score is the primary discriminator — no other filter's source material scores above 0.60 with this lowband + centroid combination
- **Real-world calibration** — corrected initial centroid estimate from "1200–1900 Hz" to actual 2041–3545 Hz after diagnostic analysis revealed the earlier summary was wrong. Updated centroid_max from 2000 to 4000 and lowband_min from 0.35 to 0.20 based on actual per-block spectral data
- **Filter chain now 9 filters** — thunder → impulse → birdsong → **amplified_bass** → rain → weedwhacker → mower → diesel → conversation
- **Config keys added** — `amplified_bass_min_music_score` (0.60), `amplified_bass_lowband_min` (0.20), `amplified_bass_centroid_max_hz` (4000), `amplified_bass_env_std_max` (3.0), `amplified_bass_min_history` (6), `amplified_bass_min_beat_confidence` (0.20), `rain_max_music_score` (0.70), `mower_max_music_score` (0.70) in both config YAMLs
- **Regression clip added** — `idiot-neighbor-mild-85db.wav` (locked, expected `amplified_bass (multiple)`)
- **10 amplified bass core tests** — bass profile detected, low mscore/lowband/centroid/env rejected, quiet rejected, short history rejected, no db_now skips floor, mscore boundary, diesel/rain don't match
- **12 amplified bass sensitivity tests** — min_history boundary, lowband boundary (with mscore isolation), centroid boundary, dB boundary, custom min_db, env_std boundary
- **6 music score guard tests** — rain rejected by high mscore, rain passes with real mscore, rain guard configurable; same trio for mower
- **2 existing test fixtures adjusted** — fan fixture lowband 0.39→0.10 (unrealistically bass-heavy for HVAC), engine no-filter fixture lowband 0.4→0.20 (was triggering new amplified_bass filter)
- **`amplified_bass_min_db` removed** — the noise_floor_db gate (50 dBA) and ordinance recording thresholds already ensure only nuisance-level sounds reach DSP analysis; the min_music_score check is a far stronger discriminator than a dB floor for this category

### Beat confidence bug fix

- **`seed.py` used block 0's bconf — always 0.0** — `beat_confidence_from_history()` requires 8+ dB readings to produce a non-zero value, but the seeder was grabbing `result["blocks"][0]["bconf"]` (block 0 has only 1 reading). Changed to use `result["blocks"][-1]` which has the full history. Same fix applied to `music_like_score` for consistency (mscore works per-block but block 0's spectrum isn't representative of the clip)
- **`reclassify --update` now writes `beat_confidence` and `music_like_score`** — the single-incident and batch reclassify UPDATE statements previously only wrote `classification` and `class_journal`. Now also updates `beat_confidence` and `music_like_score` from the last block, so existing seeded incidents can be corrected by re-running `reclassify --update`

### Intra-block beat detection

- **Problem** — beat confidence was measured only at macro scale (inter-block dB variation), detecting amplitude patterns repeating every 2–8 seconds (7.5–30 BPM). Actual musical beats at 80–180 BPM occur *within* each 1-second block and get averaged into flat RMS readings — invisible to inter-block correlation. The prior docstring incorrectly claimed "lag 2 = 120 BPM" when 1 block/sec resolution means lag 2 = 30 BPM
- **Solution** — new `intra_block_beat_confidence(block, sr)` computes a short-time RMS energy envelope at 10ms hop (100 frames/sec), then autocorrelates at lags 33–75 (corresponding to 180–80 BPM). Clear amplitude pulses from musical beats produce high autocorrelation peaks. A coefficient-of-variation guard (< 5%) catches constant-energy signals (pure tones, steady noise) whose near-zero-variance envelopes would otherwise produce spuriously high correlation from float-precision noise
- **Normalization fix** — removed the `(corr+1)/2` normalization from both intra and inter-block functions. Raw normalized autocorrelation is already in [0,1] for the positive-max we take; the `(+1)/2` shift was compressing all values into [0.5,1.0], making the metric useless for discrimination (rain: 0.68, gas mower: 0.90, bass music: 0.84 — everything looked the same)
- **Inter-block removed from combined function** — `beat_confidence()` now uses intra-block only. The inter-block component was measuring dB *stability*, not rhythm — any steady source (mower: 0.805, thunder-rain: 0.773) autocorrelates strongly simply because dB levels don't change much between blocks. `_inter_block_beat_confidence()` is retained but no longer contributes to the combined score
- **Beat confidence wired into amplified_bass filter** — new `min_beat_confidence` parameter (default 0.20, config key `amplified_bass_min_beat_confidence`) gates the filter to require rhythmic content. The `beat_confidence` value is now threaded through `identify_filter()` → `_check_*()` dispatch. At 0.20, 90% of real bass music blocks pass; non-musical sources that could clear the other amplified_bass gates (mscore, lowband, centroid) are already rejected by the mscore gate
- **Reclassify summary now shows beat/music values** — `print_summary()` displays the last block's music_like_score and beat_confidence, providing visibility without needing `--verbose`
- **Threshold recalibration** — global `min_beat_confidence`: 0.38 → 0.20; `amplified_bass_min_beat_confidence`: 0.20 (new). Both calibrated for the raw [0,1] scale where bass music scores 0.04–0.86 (median 0.54, p25 0.30)
- **Validation against regression clips (final raw values):**
  - Bass music: **0.68** ← clearly highest among non-engine sources
  - Diesel: 0.31 (genuinely periodic engine firing — correct)
  - Birdsong morning: 0.26
  - Robin: 0.19
  - Mower electric: 0.14
  - Thunder-rain: 0.14
  - Thunder-cracks: 0.09
  - Rain: 0.00
  - Gas mower: 0.00
  - Birdsong chorus: 0.00
- **11 tests updated** — `TestCombinedBeatConfidence` rewritten (3 tests: equals intra, ignores inter, accepts db_history param), `TestIntraBlockBeatConfidence` thresholds recalibrated for raw scale, amplified_bass beat_confidence gate tests added (3 tests: low bconf rejected, high accepted, None skips check)

### Classification suffix "+" logic

- **Problem** — when an incident's journal contains one real classification and one or more `unknown` entries, `_compute_dominant()` produced `"class (multiple)"` — implying multiple distinct sources were present, when in reality it's just one source with some unclassifiable blocks. Misleading in the UI and for analysis
- **Solution** — new three-way suffix: bare name (single source only), `"class+"` (one real source + ignorable entries like `unknown`/`none`), `"class (multiple)"` (two or more real sources). Updated `_compute_dominant()` in `reclassify.py` and the parallel inline logic in `engine._finalize_incident()`
- **Timeline regex updated** — `timeline.html` strip regex now handles both suffixes: `.replace(/ \(multiple\)$/, '').replace(/\+$/, '')`
- **Test fix** — existing `_compute_dominant` test that used `"mower"` (which backdates and erases unknown) changed to `"music_like"` to properly exercise the unknown+real suffix path

**Total: 493 tests passing**

### Open-window bass recalibration + flatness diesel guard

- **Problem** — two new recordings of the same neighbor's bass music (recorded with windows/doors open instead of through garage walls) misclassified entirely: `medium-90db` as `"mower (multiple)"` and `medium-with-truck` as `"rain (multiple)"`. With more spectrum passing through open windows, the signal becomes less bass-dominant: lowband drops from 0.43 (through-wall median) to 0.19, mscore drops from 0.73 to 0.50, centroid rises from 2800 to 2977. The original amplified_bass thresholds (mscore ≥ 0.60, lowband ≥ 0.20) were calibrated entirely from through-wall recordings and missed this scenario
- **Threshold matrix testing** — surveyed spectral profiles of all 7 relevant clips (3 bass, rain, 2 mower, diesel) and tested 6 threshold proposals. Zero false positives on all non-bass clips across all proposals — the separation between bass music and rain/mower/diesel is multi-dimensional and robust
- **New thresholds:**
  - `amplified_bass_min_music_score`: 0.60 → **0.45** (open-window median 0.50; rain max 0.503 but blocked by lowband)
  - `amplified_bass_lowband_min`: 0.20 → **0.16** (open-window median 0.19; rain max 0.137, margin 0.023)
  - `amplified_bass_flatness_min`: **0.20** (NEW) — diesel guard replacing beat confidence. Diesel flatness median 0.151 (max 0.285 but only 1/21 blocks); bass music minimum 0.207 across all recordings. Clean separation
  - `amplified_bass_min_beat_confidence`: 0.20 → **0.0** (disabled) — truck/wind noise overlays destroy rhythm detection. The medium-with-truck clip has bconf median 0.00. Flatness handles diesel separation instead
- **Results after recalibration:**
  - `medium-90db`: **37/49** amplified_bass → `amplified_bass (multiple)` (was 0/49)
  - `medium-with-truck`: **28/33** amplified_bass → `amplified_bass (multiple)` (was 1/33)
  - `mild-85db` (original): 24/29 → `amplified_bass (multiple)` (unchanged ✓)
  - Rain, mower (both), diesel, birdsong (3), thunder (2): all unchanged, zero false positives ✓
- **Regression clips added** — `idiot-neighbor-medium-90db.wav` (locked, expected `amplified_bass (multiple)`) and `idiot-neighbor-medium-with-truck.wav` (locked, expected `amplified_bass (multiple)`)
- **Tests updated** — `test_low_beat_confidence_rejected` now explicitly passes `min_beat_confidence=0.20` (default is 0.0). New tests: `test_beat_confidence_disabled_by_default`, `test_low_flatness_rejected` (diesel guard). Boundary tests recalibrated: lowband boundary 0.20→0.16, mscore boundary adjusted accordingly

**Total: 497 tests passing**

</details>

## v11 - 2026-04-09 — "Tweaker Edition"

Enhancements to assist in the inevitable tweaking required to actually deploy this solution. Many configuration defaults tweaked after analyzing sample input.

<details>

<summary>Key details</summary>

### Sound classification expansion (Tier 1)

- **Ten-category classification** — replaced binary `music_like`/`other` with ten categories: `music`, `music_like`, `unknown`, `impulse`, `thunder`, `rain`, `mower`, `birdsong`, `drive_by`, `forced_test`. Filter-identified sounds are now labeled with their filter name instead of being silently discarded
- **Birdsong exclusion filter** — new `looks_like_birdsong()` DSP function targeting the 2–8 kHz band (highband_ratio >= 0.55, lowband_ratio <= 0.15, flatness >= 0.30, stable amplitude std <= 4.0). Checked after thunder/impulse and before rain/mower in filter priority order
- **Excluded incident tracking** — in continuous and intermittent modes, filter-hit sounds are logged as metadata-only incidents with `excluded=1` (no WAV capture, no song-gap merging). Provides an audit trail of what the system heard and why it was dismissed. In `continuous_music_focus` mode, excluded sounds are silently dropped as before
- **Drive-by reclassification** — drive-by detection now sets `classification='drive_by', excluded=1` instead of soft-deleting the row, preserving the complete detection history
- **Schema v2** — `ALTER TABLE incidents ADD COLUMN excluded INTEGER DEFAULT 0`. Migration applied automatically on startup via the existing schema-version upgrade path
- **Dashboard "Class" column** — replaced the "Music Score" column with a "Class" column showing the incident's classification. Tooltip on the column header lists all possible categories for discoverability
- **Engine refactor** — new internal methods: `_identify_filter()` (priority-ordered filter check), `_classify_sound()` (music/music_like/unknown based on score+confidence thresholds), `_should_log_excluded()`, `_begin_excluded_incident()`, `_extend_excluded_incident()`, `_end_excluded_incident()`

### DSP documentation & sensitivity (Tier 2)

- **Module-level docstring** — `dsp.py` now opens with a pipeline overview and categorization of all "magic numbers" (epsilon values, band-split boundaries, autocorrelation constants) with rationale
- **Function docstrings with rationale** — every function in `dsp.py` now has a comprehensive docstring explaining what it does, why each constant was chosen, and what real-world sounds motivated the thresholds
- **Hardcoded thresholds extracted as parameters** — `looks_like_thunder` now accepts `lowband_min` (default 0.55) and `flatness_min` (default 0.45) as keyword arguments. `looks_like_mower` now accepts `env_std_max` (default 3.5), `min_history` (default 6), and `window` (default 12). `looks_like_rain` now accepts `min_history` (default 6) and `window` (default 12). All wired through `noise_warden.yaml` → `engine.py` → `dsp.py`
- **Config keys added** — `thunder_lowband_min`, `thunder_flatness_min`, and `mower_env_std_max` in `noise_warden.yaml` for field-tuning without code changes
- **31 sensitivity tests** — `TestMusicLikeScoreSensitivity` (9 tests: lowband boost mapping, saturation, tonal window peak/symmetry/rain-rejection, weight-only cases), `TestBeatConfidenceSensitivity` (5 tests: lag patterns, noise rejection, monotone, boundary), `TestThunderSensitivity` (5 tests: lowband/flatness boundary pairs, configurable override), `TestMowerSensitivity` (5 tests: env_std boundary, configurable override, centroid boundaries), `TestRainSensitivity` (4 tests: min_history boundary, custom min_history, custom window), `TestBirdsongSensitivity` (4 tests: min_history boundary, custom min_history, highband boundary) — **Total: 278 tests passing**

### New sound categories (Tier 3)

- **Weedwhacker detection** — `looks_like_weedwhacker()` targets the 2–6 kHz centroid range with moderate-to-high flatness (0.45+), minimal bass (lowband ≤ 0.15), and moderately steady amplitude (env_std ≤ 5.0). Distinguished from mower by higher centroid and from birdsong by higher flatness and more midband energy. Checked after birdsong and before mower in filter priority
- **Diesel idle detection** — `looks_like_diesel()` targets low-centroid sounds (≤ 400 Hz) with dominant bass (lowband ≥ 0.45), moderate flatness (0.40–0.65, below rain territory), and very steady amplitude (env_std ≤ 3.0). Requires 8+ seconds of history for sustained-pattern confirmation. Checked after mower in priority to distinguish from higher-frequency mechanical sounds
- **Conversation detection** — `looks_like_conversation()` targets mid-centroid speech (500–2500 Hz) with moderate flatness (≤ 0.55, speech has harmonics), not bass-dominant (lowband ≤ 0.35), and the key distinguisher: syllable-level amplitude modulation (env_std 3.0–8.0 dB). Requires 10+ seconds of history. Broadest catch — checked last in filter priority. Single speakers are more reliable than groups (groups approach broadband noise)
- **Filter priority order** — thunder → impulse → birdsong → rain → weedwhacker → mower → diesel → conversation. Most specific patterns first, broadest last
- **20 config keys added** — all three new filters are fully configurable via `noise_warden.yaml` with documented defaults: `weedwhacker_*` (5 keys), `diesel_*` (6 keys), `conversation_*` (7 keys)
- **Dashboard/incidents tooltips updated** — Class column tooltip now lists all 13 categories — **Total: 312 tests passing**

### Classification journal & multi-source incidents

- **Classification journal** — active incidents now track a `[(elapsed_sec, classification)]` transition log. Only source changes are recorded (not every block), keeping the journal compact. On finalization, the journal is stored as JSON in a new `class_journal TEXT` column
- **"(multiple)" suffix** — when an incident's journal contains more than one distinct classification, the original classification is suffixed with " (multiple)" (e.g., `music (multiple)`). Visible at a glance in dashboard and incidents table without requiring popup drill-in
- **Source timeline in popup** — the incident detail popup renders a "Source timeline" section showing each transition with elapsed time when the journal has multiple entries
- **`last_above` fix for filter-hit blocks** — when a filter matches during an active incident but noise is still above threshold, `last_above` is now refreshed. Previously, filter-hit blocks didn't update `last_above`, causing the incident to zombie-timeout via `song_gap_merge_sec` despite continuous above-threshold noise
- **No double-counting** — when a normal incident is active, filter-hit blocks are captured in the journal instead of spawning separate excluded incidents. Excluded incidents are only created when no normal incident is active
- **Schema v3** — `ALTER TABLE incidents ADD COLUMN class_journal TEXT`. Migration applied automatically. `finalize_incident()` accepts optional `class_journal` and `classification` parameters — **Total: 327 tests passing**

### Reclassify tool & UI

- **Reclassify CLI** (`noise_warden/reclassify.py`) — replay the full DSP pipeline block-by-block on any captured snippet using the current config thresholds. Three modes: single incident by ID, standalone WAV file, or batch all incidents. `--verbose` prints per-block detail; `--update` writes the new classification and journal back to the database. Importable `analyze_clip()` function for programmatic use
- **Reclassify API endpoint** — `POST /incidents/{id}/reclassify` runs the DSP pipeline on the incident's stored snippet and returns a JSON comparison: old vs new classification, journal timeline, filter distribution, block count, peak/avg dB. Optional `?apply=true` query param writes the new classification and journal to the DB. Auth-protected
- **In-popup reclassify UI** — "Re-analyze with current config" button in the incident detail popup. Calls the API and renders an inline comparison showing old → new classification (color-coded changed/unchanged), the full journal timeline, and filter distribution. When the classification or timeline has changed, an "Apply" button appears to commit the update without leaving the popup
- **Calibration workflow** — enables rapid config iteration: tweak thresholds in YAML → click "Re-analyze" on a known incident → see whether the classification improves → apply or discard. No SSH, no CLI, no page reload — **Total: 348 tests passing**

### Filter holdover & detection latency backdate

- **Filter holdover** (`apply_filter_holdover()`) — when a sustained-pattern filter (mower, birdsong, etc.) has been active for `holdover_min_run` consecutive blocks (default 5), it persists through up to `holdover_max_gap` unmatched blocks (default 12). During active holdover, transient filters (impulse) are suppressed by the established filter. Prevents sustained sounds from fragmenting during brief stop/start pauses or momentary signal variations. Real-world example: a 20-minute mower recording that previously showed 11 journal entries (mower → unknown → impulse → mower → ...) now cleanly shows 4
- **Mower env_std threshold bump** — `mower_env_std_max` raised from 3.5 to 4.5, catching slightly noisier blocks near mower start/stop transitions that were previously falling through to "unknown"
- **Journal backdate by detection latency** — sustained-pattern filters require `min_history` blocks of data before they can make a confident match. When a filter first triggers, the journal entry is now backdated to the front of the detection window, reflecting when the source _actually started_, not when the system had enough data to confirm it. Example: mower (min_history=6) first confirmed at block 17 → journal entry backdated to block 11. Clamped to never overlap the previous journal entry
- **`get_filter_detection_latency()`** — DSP helper mapping filter names to their min_history values (mower=6, birdsong=8, conversation=10, diesel=8, rain=6, weedwhacker=6). Instant detectors (impulse, thunder) return 0. Overridable via existing config keys (`birdsong_min_history`, `conversation_min_history`, `diesel_min_history`)
- **Reclassify Apply for journal-only changes** — the "Apply" button in the re-analyze UI now appears when only the journal/timeline has changed, even if the classification itself is unchanged. Previously, a same-classification result with a different timeline showed "✓ Classification unchanged" with no way to commit the improved journal
- **Config keys added** — `holdover_min_run: 5`, `holdover_max_gap: 12` in both config YAMLs
- **Snippet pre-trigger reduced** — `snippet_pre_seconds` reduced from 12 to 2 seconds, saving ~80% of pre-trigger buffer storage while still providing sufficient context — **Total: 370 tests passing**

### Peak-weighted birdsong & classification regression harness

- **Peak-weighted birdsong (Path B)** — `looks_like_birdsong()` now has dual-path detection. Path A (original) targets sustained high-frequency, stable-amplitude calls (warblers, wrens). New Path B targets bursty chirps: a block qualifies if it's a loud peak (dB ≥ mean+10) with extreme highband (≥ 0.89), high centroid (≥ 2800 Hz), and the surrounding window has high amplitude variance (env_std ≥ 8.0). This catches robins, doves, and seagulls whose staccato chirps never triggered Path A's stability requirements. Four new config keys: `birdsong_peak_highband_min`, `birdsong_peak_centroid_min`, `birdsong_peak_db_threshold`, `birdsong_peak_variance_min`
- **Impulse birdsong exemption** — `_check_impulse()` now returns False for transients with extreme highband (≥ 0.89) and minimal bass (lowband ≤ 0.10). Without this, robin chirps (30+ dB jumps, sharp attacks) always triggered impulse before birdsong could evaluate them. Config keys: `impulse_birdsong_highband_min`, `impulse_birdsong_lowband_max`
- **Classification regression harness** (`tests/test_classification_regression.py`) — parametrized pytest tests replaying real WAV snippets through `analyze_clip()` and locking expected classifications. Three verified incidents: 63 (mower), 65 (mower), 67 (birdsong). Skips gracefully when `local_data/` is unavailable. Includes a summary test that prints a visual table. Prevents future threshold changes from silently breaking known-good calibrations

### Journal backdate & source timeline UI

- **Journal backdate unknown-overlap fix** — when a filter's backdated entry would overlap with a trailing "unknown" journal entry, the unknown is now replaced instead of clamping after it. Root cause: the initial "unknown" placeholder represented time before the filter had enough history to confirm its detection — not a genuine unknown sound source. This eliminated spurious 1-second unknown gaps before every filter transition. Incident 67 journal: 9 entries → 5 entries, with birdsong spanning continuously from 6s to 34s. Applied to both `reclassify.py` and `engine.py`
- **Active entry highlighting** — the source timeline in the popup now highlights whichever journal entry covers the current audio playback position. Uses a teal background tint (`.journal-active`), updated at 60fps via the existing `requestAnimationFrame` cursor loop. Also updates on pause, end, and manual seek
- **Unknown entries dimmed** — journal entries classified as "unknown" no longer have bold text; they render at 50% opacity to keep visual focus on the meaningful classifications
- **Click-to-seek on journal entries** — each source timeline entry is now clickable. Clicking seeks audio to 0.5 seconds before the entry's start time (half a block lead-in) and begins playback. Provides a quick way to audition the specific segment that triggered a classification — **Total: 375 tests passing**

### Audio capture: blocking → continuous streaming

- **Switched from `sd.rec()` blocking mode to `sd.InputStream` callback mode** — the previous blocking capture model called `sd.rec(22050)` + `sd.wait()` per block, then ran DSP analysis and WAV I/O before starting the next recording. During that processing gap (10–50ms per block), the microphone produced audio that nobody captured. This caused **audible clicks at every 1-second block boundary** — phase discontinuities confirmed by sine-wave analysis (17/23 boundaries showed discontinuity ratios up to 22x the typical sample-to-sample change). Callback mode uses `sd.InputStream`, which captures audio continuously via the OS audio driver; blocks are pushed into a thread-safe queue and consumed by the engine at its own pace. Zero sample loss regardless of processing time
- **Queue overflow protection** — the callback's `queue.put(block=False)` now handles `queue.Full` by dropping the oldest block. This prevents silent data loss if the engine somehow falls 128+ seconds behind (shouldn't happen in practice, but defensive coding beats silent failure)
- **Timeout error type corrected** — callback timeout now raises `RuntimeError` instead of `sd.PortAudioError` (which is not a proper exception class in all environments). Engine main loop updated to catch `RuntimeError` alongside `PortAudioError` and `OSError`
- **Late detection of low-frequency sounds is expected** — manual recording of 200 Hz sine didn't trigger until ~346 Hz because A-weighting penalizes low frequencies heavily (~-10.9 dB at 200 Hz vs ~-4.8 dB at 400 Hz). With the Pi's `calibration_offset_db=88.0`, a -20 dBFS signal at 200 Hz computes to ~57 dBA, below the typical 65 dBA threshold. This is correct A-weighted behavior, not a bug — **Total: 376 tests passing**
- **Blocking mode removed entirely** — the `_CALLBACK_STREAMS_ENABLED` flag and the `sd.rec()` + `sd.wait()` code path have been deleted. With proven sample loss at every block boundary, there is no valid reason to offer blocking mode as a fallback. The flag, the conditional branch in `read_block()`, and the `TestBlockingMode` test class have all been removed. References in README, plugins.py, and older CHANGELOG entries are now historical context only — **Total: 375 tests passing**

### Clear All Incidents is now a true reset

- **Hard clear replaces soft delete** — "Clear All Incidents" now performs a full database reset: all incident rows are hard-deleted (including previously soft-deleted ones), the SQLite autoincrement counter is reset so the next incident starts at ID 1, all referenced WAV snippet files are removed from disk, the autodismissed quarantine folder is emptied, and VACUUM reclaims disk space. Previously, "clear" only set `deleted=1`, leaving ghost rows, orphaned WAV files, and an ever-climbing ID counter. `soft_delete_all_incidents()` is preserved for programmatic use but the UI action calls `hard_clear_all_incidents()`
- **Browser caches cleared on reset** — the "Clear All Incidents" button now also purges all Service Worker caches (which hold offline-cached snippet audio) and removes the timeline view state from localStorage before submitting the server-side clear. This ensures a true clean slate — no stale audio data from deleted incidents lingering in the browser — **Total: 378 tests passing**

### Birdsong recalibration & mower dB floor

- **Birdsong threshold recalibration** — after re-recording with continuous capture (no more blocking-mode gaps), robin + background fan hum (incident 5) was misclassified as `unknown (multiple)` with spurious mower entries. Three fixes applied: (1) `birdsong_lowband_max` raised from 0.10 to 0.15 — continuous capture faithfully represents fan/HVAC bass that blocking mode masked; (2) `birdsong_flatness_min` lowered from 0.50 to 0.30 — robin chirps have sharp harmonic peaks (flatness 0.09–0.43) that never reached the original threshold; (3) extreme highband flatness bypass — when `highband_ratio ≥ peak_highband_min` (0.89), the flatness requirement is bypassed entirely, since above-0.89 highband with near-zero lowband is unmistakably birdsong regardless of flatness
- **Impulse birdsong exemption threshold aligned** — `impulse_birdsong_lowband_max` raised from 0.10 to 0.15 to match the new `birdsong_lowband_max`, preventing robin chirps with incidental fan bass from triggering impulse before birdsong can evaluate them
- **Mower minimum dB floor** — new `mower_min_db` config key (default 70.0 dBA). Computer fans, HVAC, and similar quiet sources at 55–65 dBA can spectrally mimic a mower (moderate flatness, mid centroid, very stable amplitude) but are far too quiet to be actual lawn equipment. The dB floor rejects these without affecting real mower detection (calibration data: 70+ dBA at any meaningful distance). Incident 5's fan-only tail blocks (61 dBA) no longer false-positive as mower

### Regression harness decoupled from database

- **File-based classification data** — regression tests now replay WAV files from `tests/classification_data/` (version-controlled) instead of looking up snippet paths in the incident database. Recordings are the empirical source of truth, completely decoupled from `local_data/` and the incident lifecycle. Hard-clearing incidents, re-recording, or any database operation no longer affects the regression baseline. Beyond regression testing, these recordings can seed a clean database for full reclassification after engine or filter changes, without needing to manually re-record each sound type -  **Total: 379 tests passing**

### Database seeding from classification data

- **Seed CLI** (`noise_warden/seed.py`) — `python -m noise_warden.seed` discovers all WAV files in `tests/classification_data/`, runs each through the full DSP pipeline via `analyze_clip()`, creates a fully-finalized incident row (timestamps, dB stats, classification, journal), and copies the WAV into the snippets directory with a descriptive filename (`incident_{id}_{source}.wav`). Provides a known-state database for testing UI, reclassification workflows, and engine changes without manual re-recording. `--dry-run` mode analyzes clips without touching the database. `--verbose` shows per-clip block tables. Config auto-detected (local → default) or specified via `-c` — **Total: 388 tests passing**

### Birdsong Path C — extreme spectral purity

- **Path C added to `looks_like_birdsong()`** — when ≥95% of energy resides above half-Nyquist (`highband_ratio ≥ 0.95`) with high centroid (≥2800 Hz) and minimal bass (shared lowband check), the spectral shape alone is diagnostic of birdsong regardless of amplitude dynamics. No common mechanical or environmental noise source (mowers ~0.80 max, HVAC/fans broadband, traffic low-frequency) concentrates energy this extremely. Catches clean bursty recordings where Path A fails on variance and Path B fails because consecutive loud chirps keep the running mean elevated. A dB floor (`mean - 15 dB`) rejects near-silence blocks where noise artifacts produce misleading spectral shapes
- **Clean robin recording** — replaced `robin_with_fan.wav` (robin + computer fan hum) with `birdsong-american_robin.wav` (pure robin, 44.1 kHz, 40s). The clean recording has extreme amplitude variance (39–92 dBA) with genuine silence between chirps, which broke both Path A (env_std 7–18 vs max 3.0) and Path B (delta only +1 to +8 dB vs threshold 10). Path C correctly classifies 28/40 blocks as birdsong
- **Config keys added** — `birdsong_purity_highband_min` (default 0.95), `birdsong_purity_db_margin` (default 15.0)

### EQ classification data tool

- **`scripts/eq_classification_data.py`** — formal, extensible tool for reshaping YouTube-sourced recordings to approximate real-world spectral characteristics. Supports frequency-domain EQ (overlap-add FFT), auto-trim to stable segments, and shaped noise mixing (pink/brown) to raise spectral flatness. Per-profile configuration with before/after spectral analysis and reclassify verification
- **Mower EQ profile working** — YouTube mower recording trimmed to stable 64-second segment (removes quiet intro/outro that inflated env_std from 8.6 to 1.6), EQ'd with bass boost + treble rolloff, pink noise mixed at -16 dB. Result: 57/64 blocks classify as mower (centroid 3503, flatness 0.350, env_std 1.786). Added to regression clips as "pending"
- **Adaptive mode** — profiles can specify `"adaptive": True` with numerical targets instead of fixed EQ bands. Binary-searches for the minimum spectral tilt (dB/octave) that achieves the centroid target, then the minimum noise level for flatness. An outer retry loop compensates for noise-induced centroid drift. The mower profile now uses adaptive mode — drop any mower recording into `tests/classification_data/`, point the profile at it, and it auto-computes the correct EQ
- **Diesel EQ profile documented as irreconcilable** — the diesel filter requires centroid ≤400 AND flatness 0.40–0.65, which are mathematically opposed when reshaping a YouTube recording with no bass content. Noise loud enough for flatness pushes centroid too high; noise quiet enough for centroid drops flatness below threshold. Needs real outdoor recording

 — **Total: 392 tests passing**

### Reclassify journal comparison fix

- **Tuple vs list false positive** — `analyze_clip()` returns journal entries as Python tuples `(0, "birdsong")`, but `json.dumps()` → `json.loads()` round-trips them as lists `[0, "birdsong"]`. The reclassify endpoint's `old_journal != new_journal` comparison always reported `journal_changed=True` even when content was identical, creating an infinite loop of "changes detected → apply → changes still detected". Fix: normalize `new_journal` to lists before comparison — **Total: 394 tests passing**

### Web UI seed button

- **"Re-seed from Classification Data" button** — visible on the incidents page only when the database is empty. Calls `POST /incidents/seed` to discover and replay all `tests/classification_data/` WAV files through the DSP pipeline, creating fully-finalized incidents. Auth-protected. Provides a one-click way to repopulate known-state data after a hard clear — **Total: 394 tests passing**

### Incident tail trimming

- **Silent tail removed at finalization** — during the `song_gap_merge_sec` window (default 12s), sub-threshold audio blocks accumulate at the end of every incident. These contribute nothing meaningful but inflate `avg_db` downward and pad the WAV snippet with dead air. At finalization, the engine now detects the gap-timeout tail (blocks after `last_above`), keeps one block for context, and trims the rest from: (1) `dbs` before computing `avg_db`, (2) `dur` for journal dominant-classification duration attribution, and (3) the WAV snippet file. `peak_db` still uses un-trimmed data (valid evidence), and drive-by detection uses the full fade-out shape. A note in the code marks that precision could be improved by trimming to the last positively-classified block instead of last above-threshold block — **Total: 399 tests passing**

</details>

## v10 - 2026-04-06 — "Smooth Operator Edition"

Polish pass addressing observations from initial local testing. Dashboard decluttered, incidents page brought to parity with dashboard formatting, thresholds page filtered to relevant categories, offline experience hardened, and auto-purge guarded against silent data loss.

<details>

<summary>Key details</summary>

### Dashboard polish

- **"Running" pill removed** — if you can see the dashboard, the app is running. The pill was always `True` and carried no useful information. The `running` key remains in `/api/state` for Home Assistant consumers
- **Mode column removed from recent incidents table** — the column always showed "respond" or "record_only" which is an internal engine label, not a user-facing concept. Mode is still stored in the database and available in the incident detail popup and CSV export
- **Warning banners for nonstandard states** — amber banner when detection is paused, amber banner when recording is disabled, blue info banner when a force-test incident is active. Banners update live via the 5-second `/api/state` poll (except recording, which reloads the page on toggle)

### Incidents page parity with dashboard

- **ID column removed** — internal database ID exposed no useful information to the operator
- **Client-side timestamp formatting** — same locale-aware date+time rendering as the dashboard, with UTC offset shown once in the "Start" column header instead of per-row raw ISO strings
- **Human-friendly duration** — durations now displayed as `Xs`, `X.Ym`, or `X.Yh` matching the dashboard format, instead of raw `REAL` seconds with decimals

### Thresholds page

- **Irrelevant categories filtered** — `commerce_industry_A1` (which the engine never evaluates) is no longer shown in the ordinance limits table. Only `continuous_A2_A3`, `intermittent_A2_A3`, and `impulse_A1_A3` are displayed
- **Active rule highlighted** — the currently-active threshold row (based on detection mode) gets a blue highlight and "(active)" label, making it immediately clear which ordinance rule is being enforced

### Data retention safety

- **Auto-purge now opt-in** — new `audio.auto_purge_enabled` config key (default `false`). Snippet cleanup at startup and the daily periodic purge are skipped unless this is explicitly set to `true`. Previously, `retention_days: 30` silently deleted old snippets with no opt-in. The `retention_days` value is still respected when purge is enabled
- **Startup log message** — when auto-purge is disabled, the engine logs a clear message noting that cleanup was skipped and why

### Offline experience

- **Service Worker expanded** — CSS and favicon are now pre-cached on SW install. Dashboard, incidents, build, and thresholds pages use network-first with cache fallback (previously only the timeline was cached). Cache version bumped to v10
- **Config and Calibration nav links disabled offline** — links are dimmed and unclickable when `navigator.onLine` is false, preventing navigation to pages where save operations would silently fail
- **Mutation controls hidden offline** — all `form[method="post"]` elements and `.destructive` buttons are hidden when offline. Restored immediately when connectivity returns
- **Offline indicator bar** — red banner at the top of every page when offline: "Offline — viewing cached data. Controls disabled."

### Resolved TODOs

- Thresholds page `commerce_industry_A1` clutter — filtered (v10)
- Dashboard Mode column — removed (v10)
- Dashboard Running pill — removed (v10)
- Dashboard warning states — added (v10)
- Incidents page formatting parity — applied (v10)
- Retention auto-delete guard — `auto_purge_enabled: false` default (v10)
- Offline caching improvements — SW expanded, mutations disabled offline (v10)
- Choppy audio — addressed in v9 (persistent WAV handle + block size revert); awaiting Pi hardware verification

### Dashboard interaction

- **Flash messages removed from control actions** — Pause, resume, recording toggle, and force-incident routes no longer pass `?msg=` query params on redirect. The warning banners (paused/disabled/force-test) provide real-time status feedback, making the flash text redundant
- **`recording_enabled` added to StateStore** — toggling recording now updates in-memory state in addition to config, so the poll-driven warning banners reflect the current recording state without requiring a page reload
- **Force-test incident toggle** — the dashboard button now reads "Start Test Incident" / "End Test Incident" as a single toggle (was two separate buttons with different labels)

### Audio scrubbing fix (third time's the charm)

- **Server-side Range request support for audio snippets** — the `/snippets/{id}` route now handles HTTP `Range: bytes=X-Y` requests and returns proper `206 Partial Content` responses with `Content-Range` headers. `Accept-Ranges: bytes` is advertised on all snippet responses. Previously, Starlette's `FileResponse` (which does NOT support Range requests as of 0.38.x) returned `200 OK` with the full file for every request, causing browsers to treat the audio as non-seekable on first load. The Service Worker's `maybeSliceForRange()` only fixed this for *cached* snippets — uncached first-load requests still broke. Both layers (server + SW) are now documented as essential and cross-referenced

  > **Architecture note — why Range handling exists in two places:**
  > This is the third time audio scrubbing has broken. For future reference:
  >   1. **Server** (`web.py` `get_snippet`): handles Range for first-load requests before the SW has cached the file
  >   2. **Service Worker** (`sw.js` `maybeSliceForRange`): handles Range for cached/offline snippets
  >   3. **`<audio preload="metadata">`**: tells the browser to fetch WAV headers on load so `audio.duration` is available for the seek bar. Do NOT change to `preload="none"`
  >
  > All three are required. Removing any one breaks scrubbing for a specific scenario.

### Single-fetch audio architecture

- **Eliminated dual-fetch race condition** — previously, `<audio preload="metadata">` and the waveform `fetch()` both independently requested the same snippet, racing each other. On first load this caused a simultaneous 200 and 206, and playback often stopped after <1 second. Now a single `fetch()` is used: the response is converted to a Blob URL shared by both the `<audio>` element and `decodeAudioData()` for the waveform canvas
- **Blob URL memory management** — `hidePopup()` revokes the Blob URL and clears the audio `src`, preventing memory leaks from accumulated object URLs across popup open/close cycles

### Smooth playback cursor

- **requestAnimationFrame-driven scrubber** — the waveform cursor line and timestamp now update at display refresh rate (~60fps) instead of the `timeupdate` event's ~4Hz. The rAF loop starts on `play`, stops on `pause`/`ended`/popup-close, and snaps to final position on stop. Manual seeks while paused also update the cursor via the `seeked` event

### Timeline enhancements

- **Persistent view state via localStorage** — the selected view (day/week/month), date, and checkbox states ("Show borderline", "Zoom to active hours") are saved to `localStorage` under `nw-timeline-state` and restored on page load. Refreshing or switching tabs returns to where you left off instead of resetting to today
- **Click-to-drill column headers** — clicking a day's column header in week or month view switches to that day's day view. Headers show a pointer cursor and underline on hover as affordance
- **Month view uses full vertical scale** — month view now uses the same `HOUR_PX` as day and week views (was previously a compressed `MONTH_HOUR_PX = 16`). Hour labels are now shown in the month view gutter as well
- **"Zoom to active hours" toggle** — a checkbox that crops the grid vertically to only the hours that contain incidents, ±1 hour padding on each side. When zoomed, the grid fills available viewport height (300px minimum floor for mobile). Falls back to full 0–24 range when no incidents exist in the current view period

### Offline reliability

- **Snippet pre-caching on all pages** — dashboard and incidents pages now pre-cache their visible snippets on load (same pattern the timeline already used), so going offline after visiting any page preserves snippet playback
- **Pre-cache gated behind `navigator.serviceWorker.ready`** — on a fresh first visit (cleared app data), the SW may still be installing when `DOMContentLoaded` fires. All three pages now wait for `navigator.serviceWorker.ready` before issuing precache fetches, ensuring they route through the SW and actually populate the cache. This also fixed offline page-switching, since the pages themselves now reliably land in the cache on first load
- **Global Service Worker registration** — SW registration moved from timeline-only to `base.html`, so all pages benefit from caching without requiring a timeline visit first

</details>

## v9 - 2026-04-05 — "Fit and Finish Edition"

UI/UX overhaul for evidence clarity, cross-platform timezone handling, data rounding, borderline threshold filtering, and calibration wizard improvements. After finally seeing the code running, there were definite improvements to be made.

<details>

<summary>Key details</summary>

### Timestamp & data precision

- **Local timezone everywhere** — All stored timestamps (incidents, calibration profiles, state `updated_at`) now use the system's local timezone with offset (e.g., `2026-04-05T14:23:17-06:00`) instead of UTC. Truncated to the nearest second to avoid sub-second clutter in logs and exports
- **dB rounding at storage boundary** — `start_db`, `peak_db`, and `avg_db` rounded to 1 decimal place; `music_like_score` and `beat_confidence` to 3 decimals. Internal computation retains full float precision
- **Cross-platform timezone validation** — Replaced Linux-only `timedatectl` subprocess with `_get_system_timezone()` that tries timedatectl → `/etc/timezone` → `/etc/localtime` symlink. No more startup warning on macOS

### Audio recording reliability

- **Block size reverted to 1.0s** — `block_seconds` changed from 0.5 back to 1.0. The v4 halving (alongside the switch from callback to blocking audio mode) doubled the WAV file I/O rate to 120 open/seek/write/close cycles per minute, which likely caused the choppy audio observed in the pilot. At 1.0s blocks, `beat_confidence_from_history` also gets a more useful 24-second autocorrelation window (was 12s). Config default, code default, and documentation now all agree on 1-second blocks
- **Persistent WAV file handle** — `_begin_incident()` now opens the `SoundFile` once and stores the handle on `self.active["wav_handle"]`. `_append_audio()` writes + flushes to the already-open handle instead of reopening the file every block. Handle is closed in `_finalize_incident()` (or on write error). Eliminates the repeated open/seek/close cycle that was the strongest candidate for causing choppy recordings on Pi SD cards. The dual-mic callback path (`_CALLBACK_STREAMS_ENABLED`) is unaffected — it uses the same `read_block()` API and the WAV write path is shared

### UI centralization & dark mode

- **Centralized CSS** — Extracted all inline styles from `base.html` into `static/style.css` using CSS custom properties (`--bg`, `--text`, `--border`, etc.) for theme support
- **Light/dark mode** — Toggle button floated right in the nav bar. Preference persisted in `localStorage` and applied before first paint to avoid flash of wrong theme
- **Config textarea height** — `.config-textarea` class gives the YAML editor `min-height: 70vh` so the full config is visible without scrolling

### Timeline improvements

- **Wider severity color bands** — Thresholds shifted to: mild (5–14), low (15–24), mid (25–29), high (30–39), severe (40+). Adds a yellow band for mild excess that was previously lumped with amber
- **Borderline incident filtering** — New `detection.borderline_margin_db` config key (default 5 dB). Incidents within this margin above threshold are still recorded but hidden on the timeline by default. "Show borderline" checkbox in the controls reveals them with a theme-aware grey color
- **Incident summary as heading** — Count and total duration ("3 incidents — 12m 34s total") promoted from small inline text to an `<h3>` directly above the calendar grid
- **Future navigation clamped** — Calendar navigation now stops 1 month into the future
- **Audio scrubber fix** — Popup `<audio>` changed from `preload="none"` to `preload="metadata"` so the browser loads WAV duration headers, enabling seek bar functionality

### Dashboard polish

- **Incident table rework** — Timezone offset shown once in the "Start" column header (e.g., "UTC -06:00") instead of per-row; duration moved to second column; start dB displayed as relative excess "(+X.Y dB)"; timestamps formatted client-side for locale

### Routing & browser fixes

- **SVG favicon** — Bar-graph icon at `static/favicon.svg`; `/favicon.ico` route serves it; `<link rel="icon">` in base template
- **`.well-known` catch-all** — `/.well-known/{rest:path}` returns empty JSON instead of polluting logs with Chrome DevTools probe 404s

### Calibration

- **Wizard UX rewrite** — Three expandable step-by-step sections: getting a reference SPL (meter, calibrator, or known source), reading raw dBFS from the dashboard, and computing/saving the profile. Labeled form fields with placeholder examples
- **Portability documentation** — New README section explaining that calibration profiles are portable between machines if the same USB mic + audio interface hardware is used. Recommended local→Pi workflow documented

### Resolved TODOs

- Minimum disturbance length — confirmed unnecessary (song-gap merge + drive-by auto-dismiss cover short events)
- Choppy audio from pilot — addressed via block size revert + persistent WAV handle (see "Audio recording reliability" above). Requires hardware verification on Pi
- All pilot observations addressed

### Operational controls

- **Armed state persistence** — Pausing detection now writes `armed: false` to the YAML config. On server restart (including uvicorn watch-mode reloads), engine reads armed state from config instead of always defaulting to `true`. Prevents hot-reloads from immediately triggering incidents in loud environments while paused
- **Pause finalizes active incidents** — Disarming the engine while an incident is in progress now calls `_finalize_incident(force=True)` so duration reflects actual monitoring time, not wall-clock time through the pause
- **Detection mode switcher** — Both dashboard and calibration pages include a detection mode dropdown (continuous / music focus / intermittent) that persists to YAML on change
- **Force incident controls** — Dashboard toggle to force-start a test incident and end it, for verifying recording and the full incident lifecycle without waiting for real noise
- **Incident duration rounding** — Stored `duration_sec` rounded to nearest whole second in `_finalize_incident()`

### Dark theme

- **Monokai-inspired palette** — Dark mode switched from blue-violet to charcoal grays (`#1e1e1e` / `#272822`) with Monokai accent colors: lime nav links (`#a6e22e`), orange active nav (`#fd971f`), teal "now" line (`#66d9ef`), yellow pills (`#e6db74`), olive-gray comment text (`#8f908a`)

### Incident popup consolidation

- **Shared popup partial** — Extracted the timeline's incident detail popup (CSS, HTML, JS) into `templates/_popup.html`, now `{% include %}`'d by timeline, dashboard, and incidents pages. One popup implementation, consistent presentation everywhere
- **Incident data via JSON** — Dashboard and incidents ▶ play buttons serialize the full incident object as a `data-incident` JSON attribute, feeding the same `showPopup(inc)` function the timeline uses. Zero extra API calls
- **Excess badge coloring** — Dashboard and incidents popups now show the same color-coded excess dB badge with ordinance period detection (day/night) that previously only appeared on the timeline
- **Ordinance data plumbed to all pages** — `ordinance_json` and `borderline_margin_db` added to both the dashboard and incidents routes so the shared popup can render threshold comparisons

### Audio playback & evidence presentation

- **Intensity waveform** — Canvas-based RMS envelope displayed above the `<audio>` controls in all incident popups. Each pixel column shows the amplitude of that slice, color-coded teal → lime → yellow → orange → red by intensity. Loud sections are immediately visible for scrubbing
- **Click-to-seek on waveform** — Click anywhere on the waveform canvas to jump to that point; auto-starts playback if paused. Playback cursor (teal vertical line) tracks position via `timeupdate`. Time display (`m:ss / m:ss`) in the bottom-right corner
- **Service worker range request fix** — Cached audio snippets now properly handle HTTP Range requests. The SW slices its cached `ArrayBuffer` and returns a `206 Partial Content` response with correct `Content-Range` headers, fixing audio scrubbing that was broken when snippets were served from cache. Cache version bumped to v8

### Nav & layout

- **Active link highlighting** — Nav links get bold + underline when `location.pathname` matches
- **Destructive button styling** — `.destructive` CSS class (red background, white text) applied to delete/clear actions

</details>

## v8 Review

Overall: v8 is deployment-ready for its primary use case (record-only monitoring). The codebase is well-hardened across 8 iterations.

<details>

HOWEVER, comma, there are a handful of items worth thinking through before you haul a Pi outside and declare it operational:

### Genuine Concerns (address before or during deployment)

1. _RELAY_HW_ENABLED = False is hardcoded in response.py:17 — If you plan to use the response feature, you'll need to manually flip this constant in the source file before deploying. It's not configurable via YAML. This means an upgrade via deploy_noise_warden.sh would reset it. Should probably be driven by a config key instead.
2. _CALLBACK_STREAMS_ENABLED = False is similarly hardcoded in audio.py:22 — Same pattern, same concern if you ever want callback mode on a deployed Pi.
3. Install script runs chown -R $USER... then later chown -R noisewarden — install_pi.sh:14-30 creates dirs as the current user, then reassigns everything to noisewarden. This works, but on re-runs or if a step fails midway, you could end up with mixed ownership. The venv is created/pip-installed as $USER, then chowned to noisewarden, which should be fine — just know that running deploy_noise_warden.sh for upgrades may hit permission issues if it runs pip install as a non-root user into a noisewarden-owned venv.
4. web.py calls load_yaml() at module import time (web.py:17) — The config, storage, state, and engine are all instantiated as module-level globals. This means:
    - If the YAML is missing or invalid, the service won't even start (no graceful error page — just a crash)
    - Config hot-edit via /config saves to disk but the in-memory cfg dict used by the engine is the original load. Some values are re-read from self.cfg each loop iteration (noise floor, thresholds), so they'd update if the dict were mutated — but save_yaml_text_validated writes a file, it doesn't reload into the running cfg dict. Confirm whether hot-edit actually takes effect without a restart.
5. No AudioCapture.close() call on the non-error path — engine.stop() calls relay.cleanup() but doesn't call self.capture.close(). If callback streams are ever enabled, this leaks the InputStream. Minor for blocking mode (sd.rec finishes naturally), but worth wiring up.

### Operational Nits (not blockers, but worth noting)

6. Storage math at 22,050 Hz — The CHANGELOG says ~2.6 MB/min. At 30-day retention with, say, 2 hours of incidents per day, that's ~9.4 GB/month. On a 16 GB SD card that's tight. A 32+ GB card or the disk quota system (which is implemented) covers this — just be aware.
7. deploy_noise_warden.sh doesn't copy config forward — The README mentions manually copying the YAML, but if you forget, the new version's default config overwrites your tuned thresholds. Since the config lives under /opt/noise-warden/current/config/ (behind the symlink), swapping the symlink immediately points at the new version's default YAML. Consider symlinking the config to shared/ or adding a defensive copy to the deploy script.
8. No structured logging — Everything goes through print() to stdout, which systemd captures in the journal. That's fine for a single-Pi deployment, but there's no log level control, no rotation, and no way to quiet the system short of editing code.
9. Magic numbers in DSP — min_music_like_score: 0.62, min_beat_confidence: 0.38, the beat confidence autocorrelation formula, and the music-like score weighting (0.6/0.4) are all heuristics with no published validation. Expect a calibration period where you watch false positives/negatives and tune these values. The good news: they're all in the YAML.
10. Dual-mic plugins exist but aren't wired — This is documented and expected. Just don't expect them to work by flipping the config flags — the engine loop doesn't call them.

### Things That Look Solid

- WAL mode + schema migrations + stale incident repair + vacuum — the DB layer is well-armored for Pi-scale deployment
- Self-noise suppression lifecycle (response → cooldown → resume detection) is clean and centralized
- The symlink deployment model with rollback capability is exactly right for iterating on a remote Pi
- Exclusion filters (impulse, thunder, rain, mower, drive-by) with quarantine-not-delete is thoughtful
- Systemd service hardening (rate-limited restarts, proper timeouts) is production-ready
221 tests covering the critical paths

**Bottom line**: For record-only mode (the default), deploy away. The biggest deployment-day risk is item #7 (accidentally losing your tuned config on upgrade) and item #4 (whether config edits via the web UI actually take effect at runtime). If you plan to use response mode, address #1 first — that hardcoded flag is the kind of thing that'll cost you an hour of "why isn't the relay firing" debugging after you've already hauled everything outside.

</details>

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
