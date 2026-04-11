"""Seed the incident database from curated classification recordings.

Replays each WAV file in tests/classification_data/ through the current DSP
pipeline, creates a fully-finalized incident row, and copies the WAV into the
snippets directory.  This provides a known-state database for testing UI,
reclassification workflows, and engine changes — without needing to manually
re-record a dozen different sounds each time.

The classification_data/ WAV files are the empirical source of truth.  The
database rows created here are disposable derivatives — they can be hard-cleared
and re-seeded at any time.

Usage:
  python -m noise_warden.seed                          # seed from classification_data/
  python -m noise_warden.seed --verbose                # show per-clip analysis
  python -m noise_warden.seed --dry-run                # analyze only, no DB writes
  python -m noise_warden.seed -c path/to/config.yaml   # use specific config
"""
# eslint-disable -- node scripts use the console

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

import yaml

from noise_warden.ordinance import applicable_threshold
from noise_warden.reclassify import analyze_clip, print_block_table, print_summary


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Location of curated classification WAV files (relative to project root)
DEFAULT_CLASSIFICATION_DIR = os.path.join(
    os.path.dirname(__file__), "..", "tests", "classification_data"
)


# ---------------------------------------------------------------------------
# Core seeding logic
# ---------------------------------------------------------------------------

def discover_clips(classification_dir):
    """Find all WAV files in the classification data directory.

    Returns a sorted list of (filename, full_path) tuples.  Only .wav files
    are included — other files (READMEs, etc.) are silently skipped.
    """
    if not os.path.isdir(classification_dir):
        return []

    clips = []
    for entry in sorted(os.listdir(classification_dir)):
        if entry.lower().endswith(".wav"):
            clips.append((entry, os.path.join(classification_dir, entry)))

    return clips


def seed_clip(storage, wav_path, filename, detection_cfg, audio_cfg, full_cfg,
              snippets_dir, verbose=False, end_time=None):
    """Analyze a single WAV, create a finalized incident, and copy the snippet.

    Args:
        end_time: explicit end timestamp for this incident. When seed_all()
            staggers clips at 15-minute intervals, each clip gets its own
            time slot so they don't stack in the timeline view. Falls back
            to now() if not provided (single-clip seeding).

    Returns a dict summarizing what was created, or None on failure.
    """
    result = analyze_clip(wav_path, detection_cfg, audio_cfg)

    if verbose:
        print(f"\n=== {filename} ===")
        print_block_table(result["blocks"])
        print_summary(result)

    if end_time is None:
        end_time = datetime.now().astimezone().replace(microsecond=0)

    duration_sec = result["n_blocks"]  # 1 block ≈ 1 second
    start_time = end_time - timedelta(seconds=duration_sec)

    # Determine the ordinance threshold that would apply at this time
    _rule_name, threshold_db = applicable_threshold(full_cfg, end_time)

    # Build the incident row — mirrors engine._begin_incident() fields.
    # Use the LAST block's mscore/bconf — block 0 has almost no history,
    # and beat_confidence_from_history() needs 8+ readings to be non-zero.
    last_block = result["blocks"][-1] if result["blocks"] else {}
    row = {
        "start_ts": start_time.isoformat(),
        "start_db": round(result["db_history"][0], 1) if result["db_history"] else 0.0,
        "peak_db": result["peak_db"],
        "avg_db": result["avg_db"],
        "threshold_db": threshold_db,
        "music_like_score": last_block.get("mscore", 0.0),
        "beat_confidence": last_block.get("bconf", 0.0),
        "classification": result["dominant"],
        "mode": "day",
        "responded": 0,
        "merge_count": 0,
        "snippet_path": None,  # Set after copy
        "notes": f"Seeded from {filename}",
        "excluded": 0,
    }

    incident_id = storage.create_incident(row)

    # Copy the WAV into the snippets directory with the incident ID
    os.makedirs(snippets_dir, exist_ok=True)
    # Strip .wav suffix from filename for a readable snippet name
    base = os.path.splitext(filename)[0]
    dest_name = f"incident_{incident_id}_{base}.wav"
    dest_path = os.path.join(snippets_dir, dest_name)
    shutil.copy2(wav_path, dest_path)

    # Finalize with full metrics, snippet path, and journal
    journal_json = json.dumps(result["journal"])
    storage.finalize_incident(
        incident_id,
        end_ts=end_time.isoformat(),
        duration_sec=duration_sec,
        peak_db=result["peak_db"],
        avg_db=result["avg_db"],
        snippet_path=dest_path,
        class_journal=journal_json,
        classification=result["dominant"],
    )

    return {
        "id": incident_id,
        "filename": filename,
        "classification": result["dominant"],
        "n_blocks": result["n_blocks"],
        "peak_db": result["peak_db"],
        "snippet_path": dest_path,
    }


def seed_all(storage, classification_dir, detection_cfg, audio_cfg, full_cfg,
             snippets_dir, verbose=False, dry_run=False):
    """Discover and seed all classification clips into the database.

    Returns a list of summary dicts (one per seeded clip).
    """
    clips = discover_clips(classification_dir)
    if not clips:
        print(f"[seed] No WAV files found in {classification_dir}")
        return []

    print(f"[seed] Found {len(clips)} classification clip(s) in {classification_dir}")

    # Stagger incident timestamps at 15-minute intervals counting backward
    # from now, so seeded incidents spread out in the timeline view instead
    # of stacking on the same timestamp.
    interval = timedelta(minutes=15)
    base_time = datetime.now().astimezone().replace(microsecond=0)

    results = []
    for i, (filename, wav_path) in enumerate(clips):
        clip_end_time = base_time - (i * interval)

        if dry_run:
            result = analyze_clip(wav_path, detection_cfg, audio_cfg)
            print(f"  {i + 1}. {filename} → {result['dominant']} "
                  f"({result['n_blocks']} blocks, peak {result['peak_db']} dBA)")
            if verbose:
                print_block_table(result["blocks"])
                print_summary(result)
            results.append({
                "filename": filename,
                "classification": result["dominant"],
                "n_blocks": result["n_blocks"],
                "peak_db": result["peak_db"],
            })
        else:
            summary = seed_clip(
                storage, wav_path, filename, detection_cfg, audio_cfg,
                full_cfg, snippets_dir, verbose=verbose,
                end_time=clip_end_time,
            )
            if summary:
                results.append(summary)
                print(f"  {i + 1}. {filename} → incident {summary['id']} "
                      f"({summary['classification']}, {summary['n_blocks']} blocks)")

    # Summary
    print(f"\n[seed] {'Analyzed' if dry_run else 'Seeded'} {len(results)} clip(s)")
    if not dry_run and results:
        ids = [str(r["id"]) for r in results]
        print(f"[seed] Incident IDs: {', '.join(ids)}")

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Seed the incident database from curated classification recordings.",
        epilog="Examples:\n"
               "  python -m noise_warden.seed\n"
               "  python -m noise_warden.seed --verbose\n"
               "  python -m noise_warden.seed --dry-run\n"
               "  python -m noise_warden.seed -c config/noise_warden.yaml\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--data-dir", default=None,
        help="Path to classification data directory. "
             "Defaults to tests/classification_data/.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print full block-by-block analysis table for each clip.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Analyze clips and print results without writing to the database.",
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
        print("[seed] ERROR: No config file found. Use -c to specify one.")
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    print(f"[seed] Config: {config_path}")

    classification_dir = args.data_dir or DEFAULT_CLASSIFICATION_DIR
    classification_dir = os.path.abspath(classification_dir)

    if not os.path.isdir(classification_dir):
        print(f"[seed] ERROR: Classification data directory not found: {classification_dir}")
        sys.exit(1)

    detection_cfg = cfg["detection"]
    audio_cfg = cfg["audio"]

    if args.dry_run:
        seed_all(
            storage=None,
            classification_dir=classification_dir,
            detection_cfg=detection_cfg,
            audio_cfg=audio_cfg,
            full_cfg=cfg,
            snippets_dir=None,
            verbose=args.verbose,
            dry_run=True,
        )
        return

    # DB-backed seeding
    from noise_warden.storage import Storage

    shared_dir = cfg["app"].get("shared_dir", "./local_data")
    db_path = args.db or os.path.join(shared_dir, "noise_warden.db")
    snippets_dir = os.path.join(shared_dir, "snippets")

    if not os.path.exists(db_path):
        print(f"[seed] ERROR: Database not found: {db_path}")
        print(f"[seed] Hint: Start the engine once to create the database, "
              f"or use --dry-run to analyze without a database.")
        sys.exit(1)

    storage = Storage(db_path)
    print(f"[seed] Database: {db_path}")
    print(f"[seed] Snippets: {snippets_dir}")

    seed_all(
        storage=storage,
        classification_dir=classification_dir,
        detection_cfg=detection_cfg,
        audio_cfg=audio_cfg,
        full_cfg=cfg,
        snippets_dir=snippets_dir,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
