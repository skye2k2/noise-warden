# Design Decisions (ADR-lite)

This file records the **why** behind noise-warden's significant and counter-intuitive design choices — the decisions that cost real investigation to reach and that a future contributor (or a future version of the author) might otherwise be tempted to "fix" by re-introducing the very thing that was deliberately removed.

The [CHANGELOG](CHANGELOG.md) records *what* changed and *when*. This file records *why*, especially for things that were **tried and rejected**. If you are about to add beat detection back, or wonder why classification is hidden by default, read here first.

---

## D1. Beat confidence was removed — rhythm does not separate music from engines

**Decision:** All beat-confidence calculation (`beat_confidence`, the intra-block autocorrelation, and an experimental whole-body `beat_confidence_v2`) was removed from the DSP, engine, storage, UI, and config in v17. Do not re-introduce a rhythm-based "is this music?" signal for this deployment.

<details>

**Why:** The theory was sound — amplified music has a rhythmic bass beat (~50–250 BPM) that engine noise lacks. The data disproved it. Validated against three months of hand-labeled recordings (29 confirmed-music incidents vs. 30 confirmed non-music):

- Aircraft, diesel trucks, and helicopters are **rhythmic machines**. Propeller blade-pass and engine firing cycles land squarely in the musical tempo range. Their per-block beat scores (0.87–0.98) were **as high as or higher than** confirmed loud music.
- Through-wall / through-distance music **loses its rhythm at the microphone**. Walls and distance act as a low-pass filter on the amplitude *envelope*, smearing the kick transients. Many hand-confirmed music incidents measured beat ≈ 0 even on the raw live signal.
- **No aggregation rescues a non-separating feature.** First-block, median, max, p90, whole-body autocorrelation, and a widened 50–250 BPM window were all tested. None separated classes that genuinely overlap on the underlying measurement.

**Consequence:** A displayed number that is wrong most of the time is worse than no number. The spectral `music_like_score` (bass + tonal balance + harmonic structure) is retained as a *tentative* hint, paired with an engine/midband veto. A genuine music classifier, if ever pursued, must be spectral/harmonic and trained on labeled data — **not** rhythm-based, because the dominant false positives in this environment are themselves periodic machines.

</details>

## D2. Infraction-first, not classification-first

**Decision:** The product leads with **loudness over the ordinance threshold, sustained over time**. Sound-source classification is a secondary, tentative hint. The incidents table hides the Class column by default behind a "Show diagnostics" toggle.

<details>

**Why:** The legal/practical goal is evidence of an ordinance violation — dBA over the limit, for long enough, often enough. That is measurable and defensible. "What kind of sound was it?" is interesting for triage but is *not* the basis of an enforcement claim, and (see D1, D3) cannot be made reliable with the available hardware. Presenting an unreliable label as the primary signal erodes trust in the whole tool. Loudness + duration + recurrence + time-of-day is the trustworthy spine; classification rides along as a hint.

</details>

## D3. In-house mid-range mic ≠ studio recordings — calibrate against reality

**Decision:** All filter thresholds and the `music_like_score` formula are tuned against **real recordings captured by the deployed microphone in the deployed location**, stored in `tests/classification_data/`. YouTube-sourced audio is explicitly rejected as calibration input.

<details>

**Why:** Two compounding realities:

1. **Distance and walls destroy spectral structure.** A neighbor's amplified bass arrives through a wall as a smeared low-frequency rumble, not the clean kick-and-bass of the source. Classification heuristics tuned on clean audio simply do not fire on the attenuated, reverberant version that actually reaches an attic-mounted mic.
2. **Lossy compression inverts the data we rely on.** Extensive analysis showed that YouTube's compression *washes out* exactly the low-frequency energy of diesel engines and bass music — to the point that a YouTube diesel clip can never trigger detection, the **opposite** of real life. High-fidelity FLAC (e.g. from freesound.org), downsampled to a mono WAV, preserves the needed structure; YouTube does not.

This is why the CHANGELOG's "Testing Resources" section warns, in capital letters, never to calibrate from YouTube. The regression suite replays the real-world WAVs through the DSP pipeline so a threshold tweak that breaks a known-good calibration fails loudly in CI rather than silently in the attic.

</details>

## D4. Classification metrics are computed once at finalize via the reclassify path

**Decision:** When an incident finalizes, its stored classification, journal, and `music_like_score` are computed by running the **same `analyze_clip()` code the `reclassify` tool uses**, on the final (trimmed → denoised → normalized) WAV. The live per-block scores are a trigger preview only.

<details>

**Why:** Two problems solved at once:

- **Parity.** Re-running `reclassify` after any engine change now reproduces the engine's own stored numbers exactly (by construction — same code, same file). Previously the engine and the reclassify tool had independent, drifting implementations of the journal→dominant logic.
- **Honest summary metrics.** The engine used to store the *first block's* `music_like_score` — taken before the spectrum was representative — which is why confirmed incidents could show a contradictory score. The stored value is now the **median over the incident body** (lead-in / lead-out excluded), which resists transient spikes and ignores quiet preroll/tail.

**Caveat:** Loudness (`avg_db`, `peak_db`) is deliberately computed from the **raw** dbs, never the processed WAV — normalization rewrites peak amplitude and would corrupt the calibrated dBA. Only the amplitude-invariant spectral metrics go through the WAV.

</details>

## D5. Music-focus mode is policy-aware about auto-dismissal

**Decision:** In `continuous_music_focus` mode, the `drive_by` and `too_short` auto-dismiss checks are **skipped**. A longer `music_focus_gap_merge_sec` (default 45s vs. the 12s `song_gap_merge_sec`) governs when an active incident finalizes.

<details>

**Why:** In music-focus mode an incident only exists because a block classified as `music_like` — so it is, by construction, a music detection, which is exactly what the mode is hunting. The general-purpose auto-dismiss checks were designed for all-sound monitoring and actively work against the mode's purpose: a brief music burst that dips below threshold and rises again was being hidden as a "drive-by" (this is the 2026-05-31 21:55 incident-8369 case). Music naturally dips below threshold between songs, during quiet passages, or when a passing vehicle masks the bass; a longer merge window keeps one nuisance session as a single incident instead of fragmenting it. Borderline (loudness-margin) dismissal still applies in all modes — that is about whether the event was loud enough to matter, which is mode-independent.

</details>

## D6. Snippet paths are resolved by basename, not trusted absolutely

**Decision:** The DB stores absolute snippet paths from the recording machine (`/opt/noise-warden/shared/snippets/...`), but every consumer resolves them through `config.resolve_snippet_path()`, which falls back to matching the **basename** against the currently-configured snippets directory.

<details>

**Why:** A database copied from the Pi to a dev machine (`./local_data/`) carries the Pi's absolute paths, which don't exist locally. The filename (`incident_{id}_{token}.wav`) is the stable identity; the directory is environment-specific. Resolving by basename makes the database portable across machines with zero data migration, and the Pi's deployed behavior is unchanged (its stored path exists, so it wins the first resolution check).

**Hard-won corollary:** `purge_orphaned_incidents()` must use this same resolver. An earlier version checked the raw stored path with `os.path.exists()` and, run against a copied database, NULLed `snippet_path` for **every** row (the `/opt/...` paths were all "missing"). That regression silently wiped snippet references on a 7,600-row database. The repair (`repair_snippet_paths()`, exposed as `reclassify --repair-snippet-paths`) reconstructs them from the filenames on disk.

</details>

## D7. Offline-capable UI requires HTTPS and server-reachability detection

**Decision:** The UI uses a Service Worker for offline page/snippet caching, which **requires HTTPS** — the install script generates a self-signed TLS certificate for the Pi. Connectivity is judged by **active server-reachability polling**, not `navigator.onLine`, with the result persisted in `sessionStorage` across page navigations.

<details>

**Why:** The dashboard is accessed from a phone or tablet over LAN, often while standing at a fence line — exactly when connectivity is flaky. `navigator.onLine` only reports whether the device has *a* network connection, not whether *this Pi* is reachable, so it cheerfully reports "online" when the Pi's WiFi has dropped. Active polling against a health endpoint reflects the real state: pills gray out and mutation controls disable when the server is genuinely unreachable. `localhost` is exempt from the HTTPS requirement so local development needs no certificate.

</details>

## D8. Single process, engine as a daemon thread inside uvicorn

**Decision:** uvicorn runs the FastAPI web server; the audio engine runs as a **daemon thread within the same process**. There is no multi-process IPC, and running multiple uvicorn workers (`--workers 2`) is explicitly avoided.

<details>

**Why:** The engine owns exclusive hardware: one audio capture device and one SQLite database (WAL mode). Duplicating the process would create two engine threads fighting over the same microphone and database. A single process keeps state coherent and the architecture simple. The cost — a CPU-heavy endpoint (e.g. `reclassify-all`) can momentarily block request handling — is acceptable for a single-user LAN tool. The crash-guard (engine thread death → SIGTERM → systemd `Restart=always`) provides recovery without the complexity of a watchdog/IPC layer.

</details>

## D9. Hard memory limits convert whole-system OOM into a recoverable restart

**Decision:** The systemd unit sets `MemoryMax`/`MemoryHigh`, and the engine self-monitors RSS and self-terminates above a threshold.

<details>

**Why:** A memory leak in an early version reached 7.6 GB RSS and triggered the kernel OOM killer, which starved the *whole Pi* — including journald, SSH, and NetworkManager — leaving the device unreachable for days until a physical power cycle. With a per-service memory ceiling, the same leak now OOM-kills only noise-warden, and `Restart=always` brings it back clean. (The systemd `StartLimitIntervalSec`/`StartLimitBurst` directives must live in `[Unit]`, not `[Service]`, on systemd 252 — placed wrong, restart rate-limiting silently does not work.)

</details>

## D10. Configuration values with a constrained domain are enforced at parse time

**Decision:** When a config/env value has a bounded domain (a fraction, a tempo range, an enum, a dB threshold), the code clamps or rejects out-of-range input rather than trusting it.

<details>

**Why:** Documenting a valid range in a comment and then accepting any value is a recurring source of silent misbehavior. If the YAML comment says a percentile is 0–100 or a gap is in seconds, the loader should enforce that. This keeps a fat-fingered config edit from producing baffling runtime behavior far from its cause.

</details>
