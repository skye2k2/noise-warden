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
    rms_dbfs,
    spectrum_features,
)

# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def analyze_clip(wav_path, detection_cfg, audio_cfg):
    """Run the full DSP pipeline block-by-block on a WAV file.

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

    block_seconds = float(audio_cfg.get("block_seconds", 1.0))
    block_size = int(sr * block_seconds)
    n_blocks = len(data) // block_size

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

        prev = db_history[-2] if len(db_history) > 1 else db_now
        raw_filt = identify_filter(feats, db_history, db_now, prev, detection_cfg,
                                   feature_history=feature_history,
                                   beat_confidence=bconf)

        # Apply holdover (same as engine._identify_filter)
        filt, prev_filt, prev_filt_run, holdover_gap = apply_filter_holdover(
            raw_filt, prev_filt, prev_filt_run, holdover_gap, detection_cfg,
        )

        # Replicate engine._classify_sound logic
        if mscore >= min_music and bconf >= min_beat:
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
            "flatness": round(feats["flatness"], 3),
            "lowband": round(feats["lowband_ratio"], 3),
            "midband": round(feats["midband_ratio"], 3),
            "highband": round(feats["highband_ratio"], 3),
            "mscore": round(mscore, 2),
            "bconf": round(bconf, 2),
            "env_std": round(env_std, 2),
            "filter": filt,
            "classification": final,
        })

    # Compute dominant classification (replicates engine._finalize_incident journal logic)
    duration = n_blocks  # Each block is ~1 second
    dominant = _compute_dominant(journal, duration)

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
_IGNORABLE_CLASSES = {"unknown", "none"}


def _compute_dominant(journal, duration):
    """Derive the dominant classification from a journal, matching engine logic.

    For single-source journals, returns that classification directly.
    For multi-source journals:
      - If only one meaningful class (plus ignorable ones like unknown/none),
        returns "class+" — the "+" indicates some unclassified blocks were
        present but only one real source was identified.
      - If 2+ meaningful classes, returns the longest-running one with
        " (multiple)" appended.
    Background noise ("unknown", "none") is excluded from the duration
    contest and the suffix decision. Falls back to "unknown" only when
    every journal entry is ignorable.
    """
    if not journal:
        return "unknown"

    unique_classes = set(entry[1] for entry in journal)

    if len(unique_classes) > 1 and len(journal) > 1:
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
        # ignorable blocks; "(multiple)" when 2+ distinct real sources.
        if len(meaningful) <= 1:
            return f"{dominant}+"
        return f"{dominant} (multiple)"

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
                        verbose=False, update=False):
    """Re-analyze a single incident from the database.

    Looks up the incident, reads its snippet WAV, runs the DSP pipeline,
    prints results, and optionally updates the DB with the new classification.

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

    result = analyze_clip(wav_path, detection_cfg, audio_cfg)

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

    return result


def reclassify_all(storage, detection_cfg, audio_cfg, verbose=False, update=False):
    """Batch-reclassify all incidents that have snippet files.

    Prints a summary table of changed classifications at the end.
    """
    with storage.conn() as c:
        rows = c.execute(
            "SELECT id, classification, snippet_path FROM incidents "
            "WHERE deleted=0 AND snippet_path IS NOT NULL "
            "ORDER BY id"
        ).fetchall()

    total = len(rows)
    changed = []
    skipped = 0
    processed = 0

    print(f"[reclassify] Batch processing {total} incidents with snippets...")
    print()

    for row in rows:
        iid = row["id"]
        old_class = row["classification"]
        wav_path = row["snippet_path"]

        if not wav_path or not os.path.exists(wav_path):
            skipped += 1
            continue

        result = analyze_clip(wav_path, detection_cfg, audio_cfg)
        processed += 1

        if result["dominant"] != old_class:
            changed.append((iid, old_class, result["dominant"]))

            if verbose:
                print(f"=== Incident {iid} ===")
                print_block_table(result["blocks"])
                print_summary(result, old_class)
                print()

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
                     iid)
                )

        # Progress logging every 25 incidents
        if processed % 25 == 0:
            print(f"  Processed {processed}/{total} ({len(changed)} changed so far)...")

    # Summary table
    print()
    print(f"--- Batch Summary ---")
    print(f"  Processed: {processed}")
    print(f"  Skipped (missing file): {skipped}")
    print(f"  Changed: {len(changed)}")

    if changed:
        print()
        print(f"  {'ID':>5}  {'Old':>25}  →  New")
        print(f"  {'—'*5}  {'—'*25}     {'—'*25}")
        for iid, old, new in changed:
            marker = "★" if update else " "
            print(f"  {iid:5d}  {(old or '?'):>25}  →  {new} {marker}")

    if update:
        print(f"\n  ★ = updated in DB")
    else:
        print(f"\n  (dry run — use --update to write changes)")


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

    if args.batch_all:
        reclassify_all(storage, detection_cfg, audio_cfg,
                        verbose=args.verbose, update=args.update)
    elif args.target and args.target.isdigit():
        incident_id = int(args.target)
        reclassify_incident(storage, incident_id, detection_cfg, audio_cfg,
                            verbose=args.verbose, update=args.update)
    else:
        print("[reclassify] ERROR: Specify an incident ID, a WAV file path, or --all.")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
