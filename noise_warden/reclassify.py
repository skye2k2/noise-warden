"""Re-run DSP analysis on captured incident snippets and regenerate classification.

Replays the full DSP pipeline block-by-block against a WAV file, producing
a new classification journal and dominant classification. Useful for verifying
whether config threshold changes would have produced a different result.

Usage:
  python -m noise_warden.reclassify 63                   # by incident ID
  python -m noise_warden.reclassify path/to/clip.wav     # by file path
  python -m noise_warden.reclassify 63 --update           # write result back to DB
  python -m noise_warden.reclassify 63 --verbose          # full block-by-block table
  python -m noise_warden.reclassify --all                 # batch all incidents with snippets
  python -m noise_warden.reclassify --all --update        # batch reclassify and update DB
"""
# eslint-disable -- node scripts use the console

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import soundfile as sf
import yaml

from noise_warden.dsp import (
    apply_filter_holdover,
    beat_confidence,
    dba_estimate,
    get_filter_detection_latency,
    identify_filter,
    music_like_score,
    resample_audio,
    rms_dbfs,
    spectrum_features,
)

# ---------------------------------------------------------------------------
# Snippet audio processing (shared with engine.py)
# ---------------------------------------------------------------------------


def denoise_snippet(wav_path, percentile=10, alpha=1.0, beta=0.02,
                    fft_size=1024, hop_size=256):
    """Remove ambient background hiss from a WAV snippet using per-snippet
    minimum-statistics noise estimation.

    Rather than requiring a separate noise profile capture, this function
    estimates the noise floor *from the snippet itself* by treating the
    quietest spectral bins (by percentile) as the ambient noise signature.
    This works because most snippets contain a mix of noise events and
    brief quiet gaps — the low-percentile magnitudes across all frames
    approximate the stationary noise floor.

    Algorithm (spectral subtraction with minimum-statistics estimation):
      1. STFT the signal into overlapping windowed frames
      2. Per-frequency-bin, take the Nth percentile of magnitudes across
         all frames as the noise floor estimate (Martin 1994 simplified)
      3. Subtract alpha * noise_floor from each frame's magnitude,
         clamping to beta * original (spectral floor prevents musical noise)
      4. Reconstruct via inverse STFT with overlap-add

    Args:
        wav_path:    path to the WAV file (modified in place)
        percentile:  percentile for noise floor estimation (lower = more
                     conservative; 10 catches steady hiss without eating transients)
        alpha:       oversubtraction factor (1.0 = subtract exactly the
                     estimated noise; >1.0 = more aggressive denoising)
        beta:        spectral floor as fraction of original magnitude
                     (prevents "musical noise" artifacts from zero-magnitude bins)
        fft_size:    FFT window size in samples (1024 ≈ 46ms at 22050 Hz)
        hop_size:    hop between frames (256 = 75% overlap for smooth reconstruction)

    Returns:
        dict with keys {action, noise_floor_db, snr_improvement_db}
        or None if the file was skipped (too short or I/O error).
    """
    try:
        data, sr = sf.read(wav_path, dtype="float32")

        # Mono only — collapse if stereo
        if len(data.shape) > 1:
            data = data[:, 0]

        # Need at least one full FFT frame to do anything useful
        if len(data) < fft_size:
            return None

        # ── Forward STFT ──────────────────────────────────────────────
        window = np.hanning(fft_size).astype(np.float32)
        n_frames = 1 + (len(data) - fft_size) // hop_size
        # Pre-allocate complex STFT matrix
        stft = np.zeros((n_frames, fft_size // 2 + 1), dtype=np.complex64)

        for i in range(n_frames):
            start = i * hop_size
            frame = data[start:start + fft_size] * window
            stft[i] = np.fft.rfft(frame)

        magnitudes = np.abs(stft)
        phases = np.angle(stft)

        # ── Noise floor estimation (minimum statistics) ───────────────
        # Per-bin percentile across all frames. Stationary hiss will have
        # consistent energy in every frame; transient signals (speech,
        # music, bangs) will only appear in some frames, pushing their
        # percentile above the noise floor.
        noise_floor = np.percentile(magnitudes, percentile, axis=0)

        # Diagnostic: average noise floor in dB (for logging)
        mean_noise_mag = float(np.mean(noise_floor))
        if mean_noise_mag > 1e-10:
            noise_floor_db = 20.0 * np.log10(mean_noise_mag)
        else:
            noise_floor_db = -100.0

        # Measure pre-denoise RMS for SNR comparison
        pre_rms = float(np.sqrt(np.mean(data ** 2)))

        # ── Spectral subtraction ──────────────────────────────────────
        # Subtract the noise floor from each frame's magnitude. The beta
        # spectral floor prevents complete zeroing of bins, which causes
        # annoying "musical noise" (tinkly artifacts from phase-only bins).
        cleaned_mag = magnitudes - alpha * noise_floor[np.newaxis, :]
        floor = beta * magnitudes
        cleaned_mag = np.maximum(cleaned_mag, floor)

        # ── Inverse STFT (overlap-add) ────────────────────────────────
        cleaned_stft = cleaned_mag * np.exp(1j * phases)
        output_len = len(data)
        output = np.zeros(output_len, dtype=np.float32)
        window_sum = np.zeros(output_len, dtype=np.float32)

        for i in range(n_frames):
            start = i * hop_size
            frame = np.fft.irfft(cleaned_stft[i]).astype(np.float32)
            # irfft may return fft_size or fft_size+1 samples; truncate
            frame = frame[:fft_size]
            end = min(start + fft_size, output_len)
            actual_len = end - start
            output[start:end] += frame[:actual_len] * window[:actual_len]
            window_sum[start:end] += window[:actual_len] ** 2

        # Normalize by window overlap (avoid division by zero in
        # regions with insufficient overlap at the very end)
        nonzero = window_sum > 1e-8
        output[nonzero] /= window_sum[nonzero]

        # Clamp to valid audio range
        output = np.clip(output, -1.0, 1.0)

        # Measure post-denoise RMS for SNR improvement estimate
        post_rms = float(np.sqrt(np.mean(output ** 2)))
        if pre_rms > 1e-10 and post_rms > 1e-10:
            snr_improvement = 20.0 * np.log10(post_rms / pre_rms)
        else:
            snr_improvement = 0.0

        sf.write(wav_path, output, sr, subtype="PCM_16")

        return {
            "action": "denoised",
            "noise_floor_db": round(noise_floor_db, 1),
            "snr_improvement_db": round(snr_improvement, 1),
        }
    except (OSError, RuntimeError) as exc:
        print(f"[reclassify] Snippet denoising failed for {wav_path}: {exc}")
        return None


def normalize_snippet(wav_path, target_peak_dbfs=-6.0):
    """Normalize a WAV snippet's peak amplitude to a target dBFS level.

    USB microphones typically produce very low digital levels (-30 to
    -50 dBFS for sounds that are loud in real life). Normalizing to a
    target peak (default -6 dBFS, standard broadcast headroom) makes
    audio immediately audible on consumer playback devices.

    Only boosts — never attenuates. If the recording is already louder
    than the target, it is left untouched.

    Args:
        wav_path: path to the WAV file (modified in place)
        target_peak_dbfs: desired peak level in dBFS (default -6.0)

    Returns:
        dict with keys {action, gain_db, old_peak_dbfs, new_peak_dbfs}
        or None if the file was skipped (already loud enough or silent).
    """
    try:
        data, sr = sf.read(wav_path, dtype="float32")
        peak = float(np.max(np.abs(data)))

        if peak < 1e-10:
            # Essentially silent — skip to avoid extreme amplification
            return None

        current_peak_dbfs = 20.0 * np.log10(peak)
        gain_db = target_peak_dbfs - current_peak_dbfs

        # Only boost, never attenuate — protects high-quality recordings
        # (freesound, etc.) from being reduced.
        if gain_db <= 0:
            return None

        gain_linear = 10.0 ** (gain_db / 20.0)
        normalized = data * gain_linear

        # Clamp to [-1, 1] as a safety net (shouldn't be needed since
        # we target below 0 dBFS, but protects against edge cases)
        normalized = np.clip(normalized, -1.0, 1.0)

        sf.write(wav_path, normalized, sr, subtype="PCM_16")
        return {
            "action": "normalized",
            "gain_db": round(gain_db, 1),
            "old_peak_dbfs": round(current_peak_dbfs, 1),
            "new_peak_dbfs": round(target_peak_dbfs, 1),
        }
    except (OSError, RuntimeError) as exc:
        # Non-fatal — the un-normalized snippet is still usable.
        print(f"[reclassify] Snippet normalization failed for {wav_path}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def analyze_clip(wav_path, detection_cfg, audio_cfg, engine_captured=False):
    """Run the full DSP pipeline block-by-block on a WAV file.

    When engine_captured=True, mirrors the engine's live classification as
    closely as possible, including lead-in / lead-out markers for preroll
    and post-trigger tail blocks. The engine's WAV includes
    snippet_pre_seconds of audio BEFORE the trigger and snippet_post_seconds
    AFTER the last above-threshold block. Those bookend blocks are not part
    of the incident proper — they appear in the journal as "lead-in" /
    "lead-out" rather than being classified through the DSP pipeline (which
    would label them "unknown" and skew the dominant-classification calc).

    When engine_captured=False (default), the entire WAV is DSP-classified
    end-to-end — appropriate for standalone recordings, test clips, and
    source audio not captured by the engine.

    Returns a dict with:
      blocks       — list of per-block result dicts (dba, features, filter, classification)
      journal      — classification journal (transitions only, as [(sec, class), ...])
      dominant     — dominant classification string (with "(multiple)" if applicable)
      db_history   — list of dBA values per block
      peak_db      — maximum dBA observed
      avg_db       — exponentially-weighted average dBA (matches engine logic)
      filter_counts — dict of {filter_name: count}
    """
    cal_offset = float(detection_cfg["calibration_offset_db"])
    min_music = float(detection_cfg["min_music_like_score"])
    min_beat = float(detection_cfg.get("min_beat_confidence", 0.38))

    data, sr = sf.read(wav_path, dtype="float32")
    if len(data.shape) > 1:
        data = data[:, 0]

    # NOTE: If the file's native sample rate differs from the config's
    # audio.sample_rate, spectral features (centroid, band ratios) will
    # vary. The resample_audio() utility in dsp.py can normalize this,
    # but all filter thresholds and regression test expectations would
    # need recalibration at the canonical rate first. See NEXT.md §5.

    block_seconds = float(audio_cfg.get("block_seconds", 1.0))
    block_size = int(sr * block_seconds)
    n_blocks = len(data) // block_size

    # Determine lead-in / lead-out boundaries to match the engine's behavior.
    # Only applied for engine-captured snippets — standalone recordings (test
    # clips, freesound downloads, etc.) should be classified end-to-end.
    if engine_captured:
        pre_sec = float(audio_cfg.get("snippet_pre_seconds", 0))
        post_sec = float(audio_cfg.get("snippet_post_seconds", 0))
        lead_in_blocks = max(0, round(pre_sec / block_seconds))
        lead_out_blocks = max(0, round(post_sec / block_seconds))

        # Clamp: lead-in + lead-out can't consume the entire clip
        if lead_in_blocks + lead_out_blocks >= n_blocks:
            lead_in_blocks = 0
            lead_out_blocks = 0
    else:
        lead_in_blocks = 0
        lead_out_blocks = 0

    # The lead-out starts at (n_blocks - lead_out_blocks).
    lead_out_start = n_blocks - lead_out_blocks

    db_history = []
    blocks = []
    journal = []
    filter_counts = {}

    # Holdover state — mirrors engine._prev_filter / _prev_filter_run / _holdover_gap
    prev_filt = None
    prev_filt_run = 0
    holdover_gap = 0
    feature_history = []

    for i in range(n_blocks):
        block = data[i * block_size : (i + 1) * block_size]

        dbfs = rms_dbfs(block)
        db_now = dba_estimate(dbfs, cal_offset)
        db_history.append(db_now)

        feats = spectrum_features(block, sr)
        feature_history.append(feats)
        feature_history = feature_history[-24:]
        bconf = beat_confidence(block, sr, db_history)
        mscore = music_like_score(feats)

        # Lead-in blocks: mark as "lead-in" without running the filter chain.
        # The engine labels these with a negative timestamp; reclassify uses
        # a positive block index but the same label so journals compare equal
        # after the negative→positive conversion below.
        if i < lead_in_blocks:
            final = "lead-in"
            filt = None
        # Lead-out blocks: mark as "lead-out" — these are the post-trigger
        # tail that the engine preserves for recording context but never
        # classifies in the journal.
        elif i >= lead_out_start:
            final = "lead-out"
            filt = None
        else:
            # Normal DSP classification for the incident body
            prev = db_history[-2] if len(db_history) > 1 else db_now
            raw_filt = identify_filter(feats, db_history, db_now, prev, detection_cfg,
                                       feature_history=feature_history,
                                       beat_confidence=bconf)

            # Apply holdover (same as engine._identify_filter)
            filt, prev_filt, prev_filt_run, holdover_gap = apply_filter_holdover(
                raw_filt, prev_filt, prev_filt_run, holdover_gap, detection_cfg,
            )

            # Replicate engine._classify_sound logic (with engine_noise detection)
            if feats.get("midband_ratio", 0) > 0.50:
                classify = "engine_noise"
            elif (feats.get("lowband_ratio", 0) > 0.10 and
                  feats.get("envelope_cv", 0.5) < 0.10 and
                  feats.get("harmonic_ratio", 0.5) < 0.40):
                classify = "engine_noise"
            elif mscore >= min_music and bconf >= min_beat:
                classify = "music"
            elif mscore >= min_music:
                classify = "music_like"
            else:
                classify = "unknown"

            final = filt if filt else classify

        # Build journal (transitions only, like engine._update_class_journal).
        # When a filter first identifies a sound, backdate the entry by the
        # filter's detection latency — the pattern was present before we had
        # enough history to confirm it.
        if not journal or journal[-1][1] != final:
            entry_block = i
            if filt:
                latency = get_filter_detection_latency(filt, detection_cfg)
                backdated = i - latency

                # If backdating would overlap with or precede a trailing
                # "unknown" entry, replace it — that unknown was really the
                # lead-in to this filter's detection window.
                if (journal and journal[-1][1] == "unknown" and
                        backdated <= journal[-1][0]):
                    journal.pop()

                earliest = (journal[-1][0] + 1) if journal else 0
                entry_block = max(earliest, backdated)

                # After replacing, check if we'd duplicate the previous entry
                if journal and journal[-1][1] == final:
                    continue
            journal.append((entry_block, final))

        # Env std for display
        if len(db_history) >= 2:
            window = db_history[-12:]
            env_std = float(np.std(np.array(window, dtype=float)))
        else:
            env_std = 0.0

        filter_counts[filt or "none"] = filter_counts.get(filt or "none", 0) + 1

        blocks.append({
            "block": i,
            "dba": round(db_now, 1),
            "centroid_hz": round(feats["centroid_hz"]),
            "envelope_cv": round(feats.get("envelope_cv", 0.0), 3),
            "flatness": round(feats["flatness"], 3),
            "harmonic_ratio": round(feats.get("harmonic_ratio", 0.0), 3),
            "lowband": round(feats["lowband_ratio"], 3),
            "midband": round(feats["midband_ratio"], 3),
            "highband": round(feats["highband_ratio"], 3),
            "mscore": round(mscore, 2),
            "bconf": round(bconf, 2),
            "env_std": round(env_std, 2),
            "filter": filt,
            "classification": final,
        })

    # Convert reclassify journal timestamps to match the engine's convention:
    # the engine uses a negative timestamp for lead-in (round(-preroll_sec))
    # and elapsed seconds from start_ts for everything else. To produce
    # comparable journals, shift block indices so lead-in blocks are negative
    # and the first incident-body block is 0.
    if lead_in_blocks > 0 and journal:
        shifted = []
        for sec, cls in journal:
            shifted.append((sec - lead_in_blocks, cls))
        journal = shifted

    # Compute dominant classification (replicates engine._finalize_incident journal logic)
    # Duration is only the incident body — excludes lead-in and lead-out.
    body_blocks = n_blocks - lead_in_blocks - lead_out_blocks
    dominant = _compute_dominant(journal, body_blocks)

    # Exponentially-weighted average dB (matches engine)
    if db_history:
        n = len(db_history)
        decay = 0.95
        weights = np.array([decay ** (n - 1 - i) for i in range(n)])
        weights /= weights.sum()
        avg_db = float(np.dot(weights, db_history))
    else:
        avg_db = 0.0

    return {
        "blocks": blocks,
        "journal": journal,
        "dominant": dominant,
        "db_history": db_history,
        "peak_db": round(max(db_history), 1) if db_history else 0.0,
        "avg_db": round(avg_db, 1),
        "filter_counts": filter_counts,
        "n_blocks": n_blocks,
    }


# Classifications that represent background noise or unidentified sound.
# These should not win the dominant-classification contest — a 60-second
# recording with 50 seconds of white noise and 10 seconds of thunder
# should report "thunder", not "unknown".
# Classifications that shouldn't compete in the dominant contest.
# "unknown" and "none" are ambient noise / no-match. "engine_noise" is the
# fallback catch-all for mechanical sounds that slipped past specific filters
# (flyover, mower, diesel) — when a specific filter *does* match, it should
# win rather than splitting the vote with the generic supercategory.
# "lead-in" and "lead-out" are bookend context (preroll / post-trigger tail)
# that were never part of the incident proper.
_IGNORABLE_CLASSES = {"engine_noise", "lead-in", "lead-out", "none", "unknown"}


def _compute_dominant(journal, duration):
    """Derive the dominant classification from a journal, matching engine logic.

    For single-source journals, returns that classification directly.
    For multi-source journals:
      - If only one meaningful class (plus ignorable ones like unknown/none),
        returns "class+" — the "+" indicates some unclassified blocks were
        present but only one real source was identified.
      - If 2+ meaningful classes, returns the longest-running one with
        " (multiple)" appended.
    Background noise ("unknown", "none") and structural bookends ("lead-in",
    "lead-out") are excluded from the duration contest and the suffix
    decision. A journal of [lead-in, mower, lead-out] returns plain "mower"
    — the bookends are invisible. A journal of [unknown, mower] returns
    "mower+" because the unknown blocks represent genuinely ambiguous audio.
    Falls back to "unknown" only when every journal entry is ignorable.
    """
    if not journal:
        return "unknown"

    unique_classes = set(entry[1] for entry in journal)

    # Structural bookends: completely invisible to classification logic.
    # They don't add "+" and don't count as multiple sources.
    _BOOKENDS = {"lead-in", "lead-out"}
    non_bookend_classes = unique_classes - _BOOKENDS

    if len(non_bookend_classes) > 1 and len(journal) > 1:
        durations = {}
        for idx in range(len(journal)):
            cls = journal[idx][1]
            start_sec = journal[idx][0]
            if idx + 1 < len(journal):
                end_sec = journal[idx + 1][0]
            else:
                end_sec = duration
            durations[cls] = durations.get(cls, 0) + (end_sec - start_sec)

        # Filter out ignorable classifications before picking the winner.
        # If all entries are ignorable, fall back to the longest one anyway.
        meaningful = {k: v for k, v in durations.items() if k not in _IGNORABLE_CLASSES}
        if meaningful:
            dominant = max(meaningful, key=meaningful.get)
        else:
            dominant = max(durations, key=durations.get)

        # "+" suffix when only one real source was identified alongside
        # ignorable ambient blocks (unknown, engine_noise); "(multiple)"
        # when 2+ distinct real sources.
        if len(meaningful) <= 1:
            return f"{dominant}+"
        return f"{dominant} (multiple)"

    # Single source (possibly with ignorable bookends) — return it directly.
    meaningful_classes = unique_classes - _IGNORABLE_CLASSES
    if meaningful_classes:
        return next(iter(meaningful_classes))
    return journal[0][1]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def print_block_table(blocks):
    """Print the full block-by-block analysis table."""
    header = (f"{'Blk':>3}  {'dBA':>6}  {'Centroid':>8}  {'Flat':>5}  "
              f"{'Low':>5}  {'Mid':>5}  {'High':>5}  {'MScore':>6}  "
              f"{'BConf':>5}  {'EnvStd':>6}  {'Filter':>12}  Class")
    print(header)
    print("-" * 110)
    for b in blocks:
        filt_str = b["filter"] or "-"
        print(f"{b['block']:3d}  {b['dba']:6.1f}  {b['centroid_hz']:8d}  "
              f"{b['flatness']:5.3f}  {b['lowband']:5.3f}  {b['midband']:5.3f}  "
              f"{b['highband']:5.3f}  {b['mscore']:6.3f}  {b['bconf']:5.3f}  "
              f"{b['env_std']:6.2f}  {filt_str:>12}  {b['classification']}")


def print_summary(result, old_class=None, old_journal=None):
    """Print the analysis summary, with optional comparison to stored data."""
    print()
    print("--- Summary ---")
    print(f"  Blocks: {result['n_blocks']}")
    print(f"  dB range: {min(result['db_history']):.1f} – {max(result['db_history']):.1f}")
    print(f"  Peak dB: {result['peak_db']}  |  Avg dB: {result['avg_db']}")
    print(f"  Filter distribution: {result['filter_counts']}")

    # Music/beat scores from the last block (what gets stored in DB)
    if result["blocks"]:
        last = result["blocks"][-1]
        print(f"  Music score: {last.get('mscore', 0.0):.2f}  |  "
              f"Beat confidence: {last.get('bconf', 0.0):.2f}")

    # Journal
    print(f"  Journal ({len(result['journal'])} entries):")
    for sec, cls in result["journal"]:
        print(f"    {sec:3d}s → {cls}")

    # Dominant classification
    print(f"  Dominant classification: {result['dominant']}")

    # Comparison with stored data
    if old_class is not None:
        if old_class == result["dominant"]:
            print(f"  ✓ Matches stored classification: {old_class}")
        else:
            print(f"  ✗ CHANGED: {old_class} → {result['dominant']}")

    if old_journal is not None:
        try:
            old_j = json.loads(old_journal) if isinstance(old_journal, str) else old_journal
            if old_j == result["journal"]:
                print("  ✓ Journal unchanged")
            else:
                print(f"  ✗ Journal changed (was {len(old_j)} entries, now {len(result['journal'])})")
        except (json.JSONDecodeError, TypeError):
            pass


# ---------------------------------------------------------------------------
# DB integration
# ---------------------------------------------------------------------------


def reclassify_incident(storage, incident_id, detection_cfg, audio_cfg,
                        verbose=False, update=False, normalize=False,
                        denoise=False, target_peak_dbfs=-6.0,
                        denoise_percentile=10, denoise_alpha=1.0,
                        denoise_beta=0.02):
    """Re-analyze a single incident from the database.

    Looks up the incident, reads its snippet WAV, runs the DSP pipeline,
    prints results, and optionally updates the DB with the new classification.
    With denoise=True, applies spectral denoising AFTER analysis but BEFORE
    normalization (so DSP sees the original signal, and gain boost amplifies
    clean audio). With normalize=True, normalizes the snippet WAV peak
    amplitude after denoising.

    Returns the analysis result dict, or None if the incident has no snippet.
    """
    inc = storage.get_incident(incident_id)
    if not inc:
        print(f"[reclassify] Incident {incident_id} not found (or soft-deleted)")
        return None

    wav_path = inc.get("snippet_path")
    if not wav_path or not os.path.exists(wav_path):
        print(f"[reclassify] Incident {incident_id}: no snippet at {wav_path}")
        return None

    print(f"=== Incident {incident_id} ===")
    print(f"  File: {wav_path}")
    print(f"  Stored classification: {inc.get('classification', '?')}")
    print()

    result = analyze_clip(wav_path, detection_cfg, audio_cfg, engine_captured=True)

    if verbose:
        print_block_table(result["blocks"])

    print_summary(result, inc.get("classification"), inc.get("class_journal"))

    if update:
        journal_json = json.dumps(result["journal"])
        last_block = result["blocks"][-1] if result["blocks"] else {}
        with storage.conn() as c:
            c.execute(
                "UPDATE incidents SET classification=?, class_journal=?, "
                "beat_confidence=?, music_like_score=? WHERE id=?",
                (result["dominant"], journal_json,
                 last_block.get("bconf", 0.0),
                 last_block.get("mscore", 0.0),
                 incident_id)
            )
        print(f"  ★ DB updated: classification={result['dominant']}")

    # Denoise snippet AFTER analysis but BEFORE normalization — DSP needs
    # the original signal, and normalize should boost clean audio, not hiss.
    if denoise:
        denoise_result = denoise_snippet(
            wav_path,
            percentile=denoise_percentile,
            alpha=denoise_alpha,
            beta=denoise_beta,
        )
        if denoise_result:
            print(f"  🔇 Denoised: noise floor {denoise_result['noise_floor_db']} dBFS, "
                  f"SNR change {denoise_result['snr_improvement_db']:+.1f} dB")
        result["denoised"] = denoise_result

    # Normalize snippet AFTER analysis — DSP needs the original signal levels
    if normalize:
        norm_result = normalize_snippet(wav_path, target_peak_dbfs)
        if norm_result:
            print(f"  ♪ Normalized: {norm_result['old_peak_dbfs']} → "
                  f"{norm_result['new_peak_dbfs']} dBFS "
                  f"(+{norm_result['gain_db']} dB)")
        result["normalized"] = norm_result

    return result


def reclassify_all(storage, detection_cfg, audio_cfg, verbose=False, update=False,
                   normalize=False, denoise=False, target_peak_dbfs=-6.0,
                   denoise_percentile=10, denoise_alpha=1.0,
                   denoise_beta=0.02):
    """Batch-reclassify all incidents that have snippet files.

    Compares both dominant classification AND journal timeline against stored
    values. An incident is considered "changed" if either differs — journal-only
    changes (e.g. engine_noise blocks appearing, filter backdating shifts) are
    just as meaningful as a classification flip and should be persisted.

    With denoise=True, applies spectral denoising to each snippet AFTER DSP
    analysis but BEFORE normalization. With normalize=True, normalizes each
    snippet WAV's peak amplitude after denoising.

    Prints a summary table of all changes at the end.

    Returns:
        dict with keys: total, processed, skipped, changed (list of
        {id, old, new, change_type} dicts), normalized (int), and applied (bool).
        change_type is "class+journal", "class", or "journal".
    """
    with storage.conn() as c:
        rows = c.execute(
            "SELECT id, classification, class_journal, snippet_path "
            "FROM incidents "
            "WHERE deleted=0 AND snippet_path IS NOT NULL "
            "ORDER BY id"
        ).fetchall()

    total = len(rows)
    changed = []
    denoised_count = 0
    normalized_count = 0
    skipped = 0
    processed = 0

    print(f"[reclassify] Batch processing {total} incidents with snippets...")
    print()

    for row in rows:
        iid = row["id"]
        old_class = row["classification"]
        old_journal_raw = row["class_journal"]
        wav_path = row["snippet_path"]

        if not wav_path or not os.path.exists(wav_path):
            skipped += 1
            continue

        result = analyze_clip(wav_path, detection_cfg, audio_cfg, engine_captured=True)
        processed += 1

        # Compare classification
        class_changed = result["dominant"] != old_class

        # Compare journal — normalize new journal entries to lists (analyze_clip
        # returns tuples, but JSON round-trips them as lists)
        new_journal = [list(entry) for entry in result["journal"]]
        try:
            old_journal = json.loads(old_journal_raw) if old_journal_raw else []
        except (json.JSONDecodeError, TypeError):
            old_journal = []
        journal_changed = old_journal != new_journal

        if class_changed and journal_changed:
            change_type = "class+journal"
        elif class_changed:
            change_type = "class"
        elif journal_changed:
            change_type = "journal"
        else:
            change_type = None

        if change_type:
            changed.append((iid, old_class, result["dominant"], change_type))

            if verbose:
                print(f"=== Incident {iid} ({change_type}) ===")
                print_block_table(result["blocks"])
                print_summary(result, old_class, old_journal_raw)
                print()

        if update and change_type:
            journal_json = json.dumps(new_journal)
            last_block = result["blocks"][-1] if result["blocks"] else {}
            with storage.conn() as c:
                c.execute(
                    "UPDATE incidents SET classification=?, class_journal=?, "
                    "beat_confidence=?, music_like_score=? WHERE id=?",
                    (result["dominant"], journal_json,
                     last_block.get("bconf", 0.0),
                     last_block.get("mscore", 0.0),
                     iid)
                )

        # Denoise snippet AFTER analysis but BEFORE normalization
        if denoise:
            denoise_result = denoise_snippet(
                wav_path,
                percentile=denoise_percentile,
                alpha=denoise_alpha,
                beta=denoise_beta,
            )
            if denoise_result:
                denoised_count += 1

        # Normalize snippet AFTER analysis — DSP needs the original signal
        if normalize:
            norm_result = normalize_snippet(wav_path, target_peak_dbfs)
            if norm_result:
                normalized_count += 1

        # Progress logging every 25 incidents
        if processed % 25 == 0:
            print(f"  Processed {processed}/{total} ({len(changed)} changed so far)...")

    # Summary table
    print()
    print(f"--- Batch Summary ---")
    print(f"  Processed: {processed}")
    print(f"  Skipped (missing file): {skipped}")
    print(f"  Changed: {len(changed)}")
    if denoised_count:
        print(f"  Denoised: {denoised_count}")
    if normalized_count:
        print(f"  Normalized: {normalized_count}")

    if changed:
        # Tally by change type for the summary header
        class_count = sum(1 for _, _, _, ct in changed if "class" in ct)
        journal_only = sum(1 for _, _, _, ct in changed if ct == "journal")
        if journal_only:
            print(f"    ({class_count} classification, {journal_only} journal-only)")

        print()
        print(f"  {'ID':>5}  {'Old':>25}  →  {'New':<25}  Type")
        print(f"  {'—'*5}  {'—'*25}     {'—'*25}  {'—'*14}")
        for iid, old, new, change_type in changed:
            marker = "★" if update else " "
            old_display = old or "?"
            # For journal-only changes, show the classification as unchanged
            if change_type == "journal":
                print(f"  {iid:5d}  {old_display:>25}       {'(unchanged)':<25}  {change_type} {marker}")
            else:
                print(f"  {iid:5d}  {old_display:>25}  →  {new:<25}  {change_type} {marker}")

    if update:
        print(f"\n  ★ = updated in DB")
    else:
        print(f"\n  (dry run — use --update to write changes)")

    return {
        "total": total,
        "processed": processed,
        "skipped": skipped,
        "changed": [{"id": iid, "old": old, "new": new, "change_type": ct}
                     for iid, old, new, ct in changed],
        "denoised": denoised_count,
        "normalized": normalized_count,
        "applied": update,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Re-run DSP analysis on incident snippets with current config.",
        epilog="Examples:\n"
               "  python -m noise_warden.reclassify 63\n"
               "  python -m noise_warden.reclassify 63 --verbose --update\n"
               "  python -m noise_warden.reclassify path/to/clip.wav\n"
               "  python -m noise_warden.reclassify --all\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target", nargs="?", default=None,
        help="Incident ID (integer) or path to a WAV file. "
             "Omit when using --all.",
    )
    parser.add_argument(
        "-c", "--config", default=None,
        help="Path to YAML config file. Defaults to config/noise_warden_local.yaml "
             "if it exists, otherwise config/noise_warden.yaml.",
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to SQLite database. Defaults to the config's shared_dir/noise_warden.db.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print full block-by-block analysis table.",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Write the new classification and journal back to the database.",
    )
    parser.add_argument(
        "--all", action="store_true", dest="batch_all",
        help="Reclassify all incidents that have snippet files.",
    )
    parser.add_argument(
        "--normalize", action="store_true",
        help="Normalize snippet WAV peak amplitude after analysis. "
             "Only boosts quiet recordings; never attenuates loud ones. "
             "Uses snippet_normalize_peak_dbfs from config (default -6 dBFS).",
    )
    parser.add_argument(
        "--denoise", action="store_true",
        help="Apply spectral denoising to remove ambient hiss before normalization. "
             "Uses minimum-statistics noise estimation (no manual noise profile needed). "
             "Reads denoise_percentile / denoise_alpha / denoise_beta from config.",
    )
    parser.add_argument(
        "--purge-orphans", action="store_true", dest="purge_orphans",
        help="NULL out snippet_path for DB rows whose WAV file no longer exists "
             "on disk. Run this after manually deleting snippet files to clean up "
             "stale references. Can be combined with --all.",
    )
    args = parser.parse_args()

    # Resolve config path
    if args.config:
        config_path = args.config
    elif os.path.exists("config/noise_warden_local.yaml"):
        config_path = "config/noise_warden_local.yaml"
    elif os.path.exists("config/noise_warden.yaml"):
        config_path = "config/noise_warden.yaml"
    else:
        print("[reclassify] ERROR: No config file found. Use -c to specify one.")
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    print(f"[reclassify] Config: {config_path}")
    detection_cfg = cfg["detection"]
    audio_cfg = cfg["audio"]
    target_peak_dbfs = audio_cfg.get("snippet_normalize_peak_dbfs", -6.0)
    denoise_percentile = float(audio_cfg.get("denoise_percentile", 10))
    denoise_alpha = float(audio_cfg.get("denoise_alpha", 1.0))
    denoise_beta = float(audio_cfg.get("denoise_beta", 0.02))

    # Standalone WAV file analysis (no DB needed)
    if args.target and not args.target.isdigit() and not args.batch_all:
        wav_path = args.target
        if not os.path.exists(wav_path):
            print(f"[reclassify] ERROR: File not found: {wav_path}")
            sys.exit(1)

        print(f"[reclassify] Analyzing: {wav_path}")
        print()
        result = analyze_clip(wav_path, detection_cfg, audio_cfg)

        if args.verbose:
            print_block_table(result["blocks"])

        print_summary(result)
        return

    # DB-backed operations (incident ID or batch)
    from noise_warden.storage import Storage

    shared_dir = cfg["app"].get("shared_dir", "./local_data")
    db_path = args.db or os.path.join(shared_dir, "noise_warden.db")

    if not os.path.exists(db_path):
        print(f"[reclassify] ERROR: Database not found: {db_path}")
        sys.exit(1)

    storage = Storage(db_path)
    print(f"[reclassify] Database: {db_path}")

    if args.purge_orphans:
        count = storage.purge_orphaned_incidents()
        print(f"[reclassify] Purged {count} orphaned snippet reference(s)")

    if args.batch_all:
        reclassify_all(storage, detection_cfg, audio_cfg,
                        verbose=args.verbose, update=args.update,
                        normalize=args.normalize, denoise=args.denoise,
                        target_peak_dbfs=target_peak_dbfs,
                        denoise_percentile=denoise_percentile,
                        denoise_alpha=denoise_alpha,
                        denoise_beta=denoise_beta)
    elif args.target and args.target.isdigit():
        incident_id = int(args.target)
        reclassify_incident(storage, incident_id, detection_cfg, audio_cfg,
                            verbose=args.verbose, update=args.update,
                            normalize=args.normalize, denoise=args.denoise,
                            target_peak_dbfs=target_peak_dbfs,
                            denoise_percentile=denoise_percentile,
                            denoise_alpha=denoise_alpha,
                            denoise_beta=denoise_beta)
    else:
        if not args.purge_orphans:
            print("[reclassify] ERROR: Specify an incident ID, a WAV file path, or --all.")
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
