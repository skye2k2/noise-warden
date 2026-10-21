# Next Steps

Remaining work only. Completed items live in the [CHANGELOG](docs/CHANGELOG.md); the rationale behind rejected approaches lives in [docs/DECISIONS.md](docs/DECISIONS.md). This file was consolidated from the former `NEXT.md` (stability analysis, v15/16) and `NEXT_RELEASE.md` (v17 planning) once their completed and rejected items had been recorded elsewhere.

## NEW Items needing prioritization

Findings behind these items are recorded in CHANGELOG v20 and DECISIONS D1 (addendum), D13, and D14 — this list is the remaining work only. Current code (v20) is deployed at https://pi.lan:8787/incidents.

- **Engine fix: music-shield corroboration.** A single false-positive `music_like` block spawns an incident that the D5 shield then exempts from the `too_short` check when finalize still calls it music-ish (engine.py ~line 954; e.g. incidents 9016/9017 — 3-second keepers). Require corroboration (≥2 music-classified journal blocks, or a minimum active duration) before the shield applies.
- **Engine fix: excluded-class continuation cutoff.** Excluded-class blocks refresh `last_above` on an active incident, so one music block on a windy night records wind to the 2-hour cap (incident 9025). In music-focus mode, stop refreshing after roughly a gap-merge window of consecutive excluded blocks so the normal gap-close finalizes the incident.
- **Candidate feature: depth + swing-rate music confirmer (DECISIONS D13).** Per-window envelope depth with ~1 Hz alternation, computed at finalize — protects deep-and-alternating incidents from non-music auto-dismiss and flags shallow high-duty "music" as suspect machine noise.
- **Optional audio cleanliness pass (DECISIONS D14).** High-pass before normalization and/or a normalization gain cap, if harsh playback on bass-heavy clips is worth addressing.
- **Resurface triaged auto-dismissed incidents — EXECUTED 2026-10-21.** The resurface scan re-ran the v20 pipeline + D13 depth/swing over every excluded row with surviving audio (752 clips): the strong + likely cohorts (score ≥ 3, overwhelmingly `flyover`/`drive_by` → `music_like`) were restored into the production DB — 67 in-place un-exclusions and 47 re-insertions from the July backup, WAVs re-seeded, every row carrying a `[resurfaced 2026-10-21 v20]` provenance note. Pre-mutation DB backup: `shared/noise_warden.pre-resurrect-2026-10-21.db` on the Pi and in `local_data/`. Audio-less metadata-flagged rows (568: 2026-04: 185, 2026-05: 383) were dropped by decision — the counts above are their durable record. Remaining follow-up: eyeball/listen to a sample of the 114 via the UI and demote any the ear rejects (they are one click from re-exclusion).
- **Unbuffered engine logging.** Engine `print()` output is block-buffered to journald, so log lines can lag days behind events (D15: the Sep 19–23 buffer flushed in one burst). Add `Environment=PYTHONUNBUFFERED=1` to the systemd unit (or `print(..., flush=True)` in the engine hot paths), alongside the Priority 3b rotating file log.

**Status at consolidation (2026-06-11):** v17 is feature-complete (beat removal, finalize keystone + parity, body-windowed metrics, engine midband veto, infraction-first UI, mode-aware auto-dismiss, music-focus gap merge, portable snippet paths, version display). v18 stabilized sample-rate negotiation and the `_compute_dominant` lead-in leak. v19 eliminated the OOM crash pattern via streaming WAV finalization, raised `min_incident_seconds` to 20, reduced `max_incident_record_hours` to 2, added drive-by sticky threshold, cosine fade-out on trimmed endings, preroll warmup guard, and music-focus auto-dismiss refinement for reclassified-as-non-music short incidents. The items below are what still remains.

---

## Priority 1 — Data migration after v17 (do before trusting the dashboard)

**Run `reclassify --update` against the production database to migrate historical rows to the v17 logic.** The v17 changes (beat removal collapsing `music`→`music_like`, the tightened midband veto, body-median `music_like_score` replacing the first-block snapshot) only affect *new* incidents at finalize. Existing rows keep their old classifications/scores until reprocessed.

<details>

- **Justification:** Without this, the dashboard shows a mix of old- and new-logic classifications, which is confusing and undermines the trust the infraction-first redesign was meant to build.
- **Caution:** This is a bulk mutation of thousands of rows. Run it against a **copy** first and eyeball the diff (`reclassify --all` dry-run shows what would change). Confirm the diff is sane before `--update` on the real DB.
- **Note:** `reclassify --repair-snippet-paths` may be needed first if working from a database copied off the Pi (see DECISIONS D6).

</details>

## Priority 2 — Sample-rate normalization: DONE in v21 (min-rule); remaining piece folded into Priority 4

**Resolved 2026-10-21 (v21):** `analyze_clip` now analyzes at min(file rate, configured rate) — files above canonical are FFT-downsampled per block, sub-rate files run at native rate, and upsampling is deliberately never done (it fabricates an empty top band that crushed flatness and classified rain as diesel — see DECISIONS D15 v21 addendum). The regression suite pins the canonical 48000 for determinism; the 44100 clips therefore analyze at native rate and keep their validated expectations.

**Remaining, moved to Priority 4 (reference library):** genuine 48 kHz regression clips cut from the production corpus (44100 sources can never be 48 kHz baselines), the comprehensive flyover-threshold recalibration (the 10 user-tagged genuine flyovers still classify as wind/thunder/diesel/amplified_bass), and the `holdover_priority_breakers` re-evaluation that depends on it.

## Priority 3 — Operational hardening (attic deployment reliability)

These are carried forward from the v15 stability analysis. The critical OOM and restart-rate fixes are already shipped (see DECISIONS D9); these are the remaining lower-severity items.

<details>

### 3a. Disable WiFi power saving on the Pi
The attic deployment logged repeated WiFi link timeouts; the radio sleeping is a likely contributor. Operational fix (not code):
```bash
sudo iw wlan0 set power_save off
# Persist via /etc/NetworkManager/conf.d/wifi-powersave.conf:
#   [connection]
#   wifi.powersave = 2
```
Prefer a wired/powerline connection to the attic if at all feasible.

### 3b. Rotating application-level log file
Logging currently goes only to stdout/journald. If journald retention expires or the journal corrupts during a memory-starved episode (as happened during the original outage), diagnostic history is lost. Add a `RotatingFileHandler` (~5 MB × 3) writing to `shared/noise_warden.log` alongside stdout. ~10 lines in `web.py`.

### 3c. systemd watchdog (lower priority — crash guard already covers ~90%)
`Type=notify` + `WatchdogSec=120` + `sd_notify("WATCHDOG=1")` in the engine loop would detect *deadlocks* (SQLite lock contention, GIL starvation, audio device hang) where the process is alive but non-functional. The existing crash guard (engine thread death → SIGTERM → `Restart=always`, DECISIONS D8) already handles thread *death*; the watchdog only adds *hang* detection. Costs a new dependency (`systemd-python` / `python3-systemd`, needs `libsystemd-dev` at build time), so it is deferred until a hang is actually observed.

</details>

## Priority 4 (post-v21) — Per-type deep analysis + curated reference library, then purge non-nuisance types from authority-facing data

Once v21's instrumentation is trusted: the operator does not want flyover, drive-by, lawn-mower, or storm data kept as part of the incident data shown to authorities. For each non-nuisance type: pull everything recorded of that type (direct SSH sync makes including the freshest Pi data easy now — see the 2026-10-21 session tooling), run deep analysis to identify its key separating characteristics (the D13 depth/swing method is the template), and keep a set of nicely-clipped representative samples to expand `tests/classification_data/` and the calibration reference set. Then the bulk recordings of those types can be dropped from the incident DB with their characteristics preserved. Big and partly manual; previously blocked on the awkward remote-analysis workflow, which is now solved.

## Priority 5 — Tagging system (v18: dataset quality + scaling)

The free-text notes workflow does not scale to the current corpus (~13 GB snippets, thousands of rows). Tags are the way to curate at this size and to build a labeled training set for any future spectral classifier.

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

## Priority 6 — Smaller carried-forward items

These are lower-value polish items noted across prior analysis and the README TODO list. None are blocking.

<details>

- **Minority journal entries and the `(multiple)` suffix.** A classification held for only a block or three against a sweeping majority probably should not earn a `(multiple)` suffix or be bolded — consider weighting/backtracking, or excluding minority `impulse`/`unknown` entries from the `(multiple)` test. Related: thunder-rumble tail-off looks like a diesel, but a diesel starting *exactly* as a thunderclap hits is vanishingly unlikely, so such single-block reclassifications should be clamped. (Mutual exclusion was *partially* addressed in v14 via thunder holdover; explicit post-processing remains.)
- **Externalize ordinance thresholds from `ordinance.py` into the YAML.** The Pleasant Grove UT thresholds are hardcoded as a Python dict referenced by the thresholds page, config page, and engine. A proper `ordinance:` YAML section (city, section reference, day/night hours, per-zone dB thresholds, measurement guidance, legal notes) would make thresholds user-editable, remove city-specific magic numbers, and make evidence logs more credible. All `ORDINANCE`-based test assertions would move to fixture-injected config.
- **`music_like_score` formula still uses unvalidated magic numbers** (the `0.6 * low + 0.4 * tonal_window` weighting and the 1.6 / 0.35 constants). Documented inline but never empirically validated. A tagging dataset (Priority 4) would enable proper tuning.
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

- **Beat/rhythm-based music detection** — rhythm does not separate music from rhythmic engine noise in this environment (DECISIONS D1).
- **Multiple uvicorn workers** — architecturally impossible while the engine is an in-process daemon thread owning the audio device + DB (DECISIONS D8).
- **`deque` for `db_history`/`feature_history`** — DSP slices these; converting at each call site costs more than the current re-slice. Allocation volume is within GC's comfort zone.
- **Persistent SQLite connection** — real but unprofitable refactor; WAL mode already handles the concurrent reader/writer pattern. Revisit only if profiling shows connection churn is a bottleneck.
- **Full-file STFT denoising for large WAVs** — streaming STFT denoising is architecturally complex and the benefit is marginal (normalization handles audibility). v19 skips files >100 MB; incidents are now capped at 2 hours (~280 MB) so this limit is rarely hit.

</details>
