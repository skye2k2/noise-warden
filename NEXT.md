# Next Steps

Remaining work only. Completed items live in the [CHANGELOG](docs/CHANGELOG.md); the rationale behind rejected approaches lives in [docs/DECISIONS.md](docs/DECISIONS.md). This file was consolidated from the former `NEXT.md` (stability analysis, v15/16) and `NEXT_RELEASE.md` (v17 planning) once their completed and rejected items had been recorded elsewhere.

Current deployment: **v21 "True-Rate Edition"** (2026-10-21) at https://pi.lan:8787/incidents. Priorities below are in recommended execution order.

## Priority 1 — v21 observation window (passive, ~1–2 weeks)

v21's new behaviors need live validation before anything gates on them:

- **Dismissal correctness:** confirm the "uncorroborated music" dismissals only eat junk (journal corroboration, `music_focus_shield_min_music_sec`) and that no genuine session start gets dismissed. Confirm windy nights now close incidents ~two gap-merge windows after the last music block instead of running to the 2-hour cap.
- **D13 envelope metrics on live incidents:** the popup now shows depth/swings per incident. Verify real music reads deep-and-alternating and machines read flat, as the labeled corpus predicted (music median 16.4 dB / ~8 swings; machines 9.7 dB / ~3). Once confirmed, wire depth+swing into auto-dismiss decisions (v22+): protect deep-and-alternating incidents, flag shallow high-duty "music" as suspect machine.
- **Spot-listen a sample of the 114 resurrected incidents** (search notes for `[resurfaced`) and re-exclude any the ear rejects.

## Priority 2 — Tagging system (dataset quality + scaling) — pulled forward: it is the tooling every later priority needs

The free-text notes workflow does not scale to the current corpus (~17 GB snippets). The 2026-10-21 mining session demonstrated both the value and the gap: the migration review was only possible because 111 incidents happened to have notes, and keyword matching on free text mislabeled "IDIOT'S FREAKING DUMP TRUCK" as music. Structured tags are how the labeled set gets big and trustworthy.

<details>

**Schema:**
- `tags(id, name, kind, created_ts)`
- `incident_tags(incident_id, tag_id, created_ts, source)` — many-to-many

**Tag kinds (examples):**
- `kind=truth`: `confirmed_music`, `confirmed_non_music`
- `kind=source`: `plane`, `diesel_truck`, `birdsong`, `children_screaming`, `chickens`
- `kind=action`: `exclude_candidate`, `keep_for_training`, `reportable`

**UI:** bulk tagging on the incidents page; saved views (e.g. `confirmed_music`, `known_false_positive`, `training_exclude`) layered on the existing per-classification filter.

**Optional WAV metadata sync:** mirror selected tags into WAV file metadata so the labels travel with the audio. DB tags remain the source of truth; if a metadata write fails or the format is inconsistent, fall back to a sidecar JSON to avoid data loss. `ffprobe` is available; `mutagen` would need adding.

**Justification:** This is the prerequisite for ever revisiting automated music classification (DECISIONS D1) — a spectral/harmonic classifier needs a labeled training set, and tagging is how that set gets built. The author also explicitly wants tag-based management for the large dataset rather than re-reading notes.

</details>

## Priority 3 — Per-type deep analysis + curated reference library, then purge non-nuisance types from authority-facing data

The operator does not want flyover, drive-by, lawn-mower, or storm data kept as part of the incident data shown to authorities. For each non-nuisance type: pull everything recorded of that type (direct SSH sync makes including the freshest Pi data easy), run deep analysis to identify its key separating characteristics (the D13 depth/swing method is the template), and keep a set of nicely-clipped representative 48 kHz samples to expand `tests/classification_data/` and the calibration reference set. Then the bulk recordings of those types can be dropped from the incident DB with their characteristics preserved.

Absorbed from the completed sample-rate work (see CHANGELOG v21 / DECISIONS D15): genuine 48 kHz regression clips must be cut from the production corpus (44100 sources can never be 48 kHz baselines); comprehensive flyover-threshold recalibration (the 10 user-tagged genuine flyovers still classify as wind/thunder/diesel/amplified_bass); re-evaluate `holdover_priority_breakers` once recalibrated. The 2026-10-21 migration review quantified the two failure modes to target: machines claiming music-family scores, and wind claiming music that the flyover guard evicts.

## Priority 4 — Historical reclassify migration: BLOCKED until Priority 3 lands

**The v21 dry run failed its ground-truth review (2026-10-21); do not run `reclassify --update` until the per-type recalibration improves the classifier.** The dry run proposed changes to 253 of 326 kept incidents; aggregate transitions looked plausible, but 35 of the 111 hand-tagged incidents among them flipped *against* ground truth in both directions (genuine planes → `amplified_bass`/`music_like`; confirmed music → `wind`/`diesel`). Swapping one ~30%-wrong labeling for another would mutate evidence rows without improving dashboard trust. Procedure when unblocked: backup first (`shared/noise_warden.pre-v21-migration.db` exists on the Pi and in `local_data/`), `reclassify --all` as the read-only preview, re-run the tagged-ground-truth cross-check, then `--update`. The batch is resilient to unreadable WAVs as of v21.1.

## Priority 5 — Operational hardening remainder

- **systemd watchdog (deferred until a hang is actually observed).** `Type=notify` + `WatchdogSec=120` + `sd_notify("WATCHDOG=1")` in the engine loop would detect *deadlocks* (SQLite lock contention, GIL starvation, audio device hang) where the process is alive but non-functional. The existing crash guard (engine thread death → SIGTERM → `Restart=always`, DECISIONS D8) already handles thread *death*. Costs a new dependency (`systemd-python`, needs `libsystemd-dev` at build time).
- **Prefer a wired/powerline connection to the attic if at all feasible** (WiFi power-save is now disabled, but the radio link remains the deployment's single point of failure).

## Priority 6 — Smaller carried-forward items

These are lower-value polish items noted across prior analysis and the README TODO list. None are blocking.

<details>

- **Optional audio cleanliness pass (DECISIONS D14).** High-pass before normalization and/or a normalization gain cap, if harsh playback on bass-heavy clips is worth addressing (capture clipping is ruled out; the harshness is normalization boost).
- **Minority journal entries and the `(multiple)` suffix.** A classification held for only a block or three against a sweeping majority probably should not earn a `(multiple)` suffix or be bolded — consider weighting/backtracking, or excluding minority `impulse`/`unknown` entries from the `(multiple)` test. Related: thunder-rumble tail-off looks like a diesel, but a diesel starting *exactly* as a thunderclap hits is vanishingly unlikely, so such single-block reclassifications should be clamped. (Mutual exclusion was *partially* addressed in v14 via thunder holdover; explicit post-processing remains.)
- **Externalize ordinance thresholds from `ordinance.py` into the YAML.** The Pleasant Grove UT thresholds are hardcoded as a Python dict referenced by the thresholds page, config page, and engine. A proper `ordinance:` YAML section (city, section reference, day/night hours, per-zone dB thresholds, measurement guidance, legal notes) would make thresholds user-editable, remove city-specific magic numbers, and make evidence logs more credible. All `ORDINANCE`-based test assertions would move to fixture-injected config.
- **`music_like_score` formula still uses unvalidated magic numbers** (the `0.6 * low + 0.4 * tonal_window` weighting and the 1.6 / 0.35 constants). Documented inline but never empirically validated. The tagging dataset (Priority 2) would enable proper tuning.
- **`music_focus_gap_merge_sec` default (45s) is a guess.** Watch real sessions: if incidents still fragment, raise it; if it bridges genuinely separate events, lower it. One-line YAML change.
- **Build page stores only one photo** — uploading a new photo overwrites the previous one. Minor usability.
- **Backup guidance is DB-only.** The recommended cron backup grabs only `noise_warden.db`; the snippets directory (the actual evidence) is not included. Document that a full backup needs both, and surface a "download all" affordance in the UI rather than right-click-save per incident.
- **Add a "Save Audio as…" export hint** — document that right-clicking the player exports the snippet; ideally pair with the "download all" affordance.
- **Speculative use cases** (not committed): per-dog-bark spectral identification/tagging (`dog1`, `dog2`); positive bird-species identification/exclusion; a nighttime-only snoring monitor variant.

</details>

## Unprioritized/Unverified

- **YAML grouping is a little arbitrary** — `audio` vs. `detection` placement is inconsistent; e.g. `noise_floor_db` and `calibration_offset_db` arguably belong under a renamed `recording` section. Additionally, `min_incident_seconds` and `max_incident_record_hours` live under `audio` even though they are behavioral/detection settings. Cosmetic; defer until a config-schema pass.
- **Dual-mic plugins not wired into engine** — `ReferenceSubtractor` (NLMS) and `DualMicDifferential` (spectral subtraction) are implemented in `plugins.py` with documented algorithms but not yet called from the engine loop. Requires a second `AudioCapture` instance for the reference device.
- **Install script enforces `/opt/noise-warden/` but the YAML hardcodes paths** — both `install_pi.sh` and `noise_warden.yaml` assume `/opt/noise-warden/` paths. The `NOISE_WARDEN_CONFIG` env var and the snippet-path resolver (DECISIONS D6) cover the common cases, but `shared_dir`/`base_dir` inside the YAML are still literal. Changing one without the other can break silently.
- **`/api/state` and `/api/health` have no auth** — these endpoints bypass `must_auth()`. Low-risk read-only data on LAN, but noted for awareness. (Auth token itself is now encrypted in transit over the self-signed TLS cert.)

## Physical deployment notes (site-specific)

Hardware/site quirks that affect detection but are not code changes.

<details>

- Under windy conditions, that portion of the attic has a few locations that creak/rattle. Shim/reattach/glue/foam insulate as best as possible.

</details>

---

## Rejected approaches (do not re-attempt without new evidence)

Recorded in full in [docs/DECISIONS.md](docs/DECISIONS.md). Summary so they are not re-proposed here.

<details>

- **Beat/rhythm-based music detection** — rhythm does not separate music from rhythmic engine noise in this environment, re-confirmed with a corrected 40–140 BPM detector in 2026-10 (DECISIONS D1 + addendum); envelope modulation *depth* is the feature that works (D13).
- **Upsampling audio to the canonical rate for analysis** — fabricates an empty top band that crushes spectral flatness and misfires the filter chain; normalization is min(file rate, config rate) only (DECISIONS D15 addendum).
- **Multiple uvicorn workers** — architecturally impossible while the engine is an in-process daemon thread owning the audio device + DB (DECISIONS D8).
- **`deque` for `db_history`/`feature_history`** — DSP slices these; converting at each call site costs more than the current re-slice. Allocation volume is within GC's comfort zone.
- **Persistent SQLite connection** — real but unprofitable refactor; WAL mode already handles the concurrent reader/writer pattern. Revisit only if profiling shows connection churn is a bottleneck.
- **Full-file STFT denoising for large WAVs** — streaming STFT denoising is architecturally complex and the benefit is marginal (normalization handles audibility). v19 skips files >100 MB; at the true 48 kHz rate that is any clip longer than ~17 minutes (DECISIONS D11 addendum).
- **Pop filter / mic accessory for "blown-out" bass** — capture clipping is ruled out; the harshness is normalization boost (DECISIONS D14).

</details>
