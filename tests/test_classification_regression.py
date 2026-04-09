"""Classification regression tests — validate DSP changes against real recordings.

These tests replay curated WAV recordings through the current DSP pipeline and
verify the dominant classification matches a manually-confirmed expected value.
They serve as a safety net when tuning filter thresholds: if adjusting birdsong
parameters accidentally breaks mower detection, these tests catch it immediately.

The WAV files live in tests/classification_data/ and are version-controlled as
the empirical source of truth — completely decoupled from the incident database.
Recordings can survive hard-clears, re-recordings, or any database operation
without affecting the regression baseline. Beyond regression testing, these
recordings can seed a clean database for full reclassification after engine or
filter changes, without needing to manually re-record a dozen different sounds.

To add a new classification recording:
  1. Copy (or record) a clean WAV snippet into tests/classification_data/
  2. Name it descriptively: {source_type}_{distinguishing_detail}.wav
  3. Append an entry to REGRESSION_CLIPS below with the expected classification
  4. Run: pytest tests/test_classification_regression.py -v

Run just these tests:
    pytest tests/test_classification_regression.py -v
"""

from __future__ import annotations

import os

import pytest
import yaml

from noise_warden.reclassify import analyze_clip

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CLASSIFICATION_DIR = os.path.join(os.path.dirname(__file__), "classification_data")
LOCAL_CFG = os.path.join(
    os.path.dirname(__file__), "..", "config", "noise_warden_local.yaml"
)

_classification_data_available = (
    os.path.isdir(CLASSIFICATION_DIR) and os.path.exists(LOCAL_CFG)
)

pytestmark = pytest.mark.skipif(
    not _classification_data_available,
    reason="Regression tests require tests/classification_data/ and local config",
)


# ---------------------------------------------------------------------------
# Regression clips — the ground truth, keyed by filename.
#
# Each entry maps a WAV filename (in tests/classification_data/) to its expected
# dominant classification and a human note about what the recording contains.
#
# Status guide:
#   "locked"  — manually verified, classification must not drift
#   "pending" — we know the current classification is wrong; will fix soon
# ---------------------------------------------------------------------------

REGRESSION_CLIPS = {
    "birdsong-american_robin.wav": {
        "expected": "birdsong",
        "status": "locked",
        "note": "Clean robin chirps (44.1 kHz, no background noise). "
                "Bursty amplitude with quiet gaps (39-92 dBA). Tests Path C "
                "extreme spectral purity detection.",
    },
    "mower-eq.wav": {
        "expected": "mower",
        "status": "pending",
        "note": "YouTube mower recording, EQ'd + trimmed + pink noise mixed "
                "via scripts/eq_classification_data.py.  57/64 blocks classify "
                "as mower.  Pending replacement with real outdoor recording.",
    },
    # Future entries:
    # "diesel-eq.wav": {
    #     "expected": "diesel",
    #     "status": "pending",
    #     "note": "YouTube diesel — cannot match filter thresholds via EQ. "
    #             "Centroid/flatness requirements are irreconcilable with "
    #             "this source material. Needs real outdoor recording.",
    # },
}


# ---------------------------------------------------------------------------
# Fixture — load config once per session
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def local_cfg():
    """Load the local dev config YAML."""
    with open(LOCAL_CFG) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Parametrized regression test
# ---------------------------------------------------------------------------

def _clip_params():
    """Generate pytest param IDs for readable test names."""
    return [
        pytest.param(
            filename,
            info["expected"],
            id=f"{os.path.splitext(filename)[0]}_{info['expected'].replace(' ', '_')}",
        )
        for filename, info in sorted(REGRESSION_CLIPS.items())
    ]


@pytest.mark.parametrize("filename,expected_class", _clip_params())
def test_classification_matches_expected(filename, expected_class, local_cfg):
    """Reclassify a regression WAV and verify dominant classification."""
    wav_path = os.path.join(CLASSIFICATION_DIR, filename)
    if not os.path.exists(wav_path):
        pytest.skip(f"{filename}: WAV file not found in classification_data/")

    result = analyze_clip(
        wav_path,
        local_cfg["detection"],
        local_cfg["audio"],
    )

    actual = result["dominant"]
    info = REGRESSION_CLIPS[filename]

    assert actual == expected_class, (
        f"{filename} ({info['status']}): "
        f"expected '{expected_class}', got '{actual}'. "
        f"Note: {info['note']}"
    )


# ---------------------------------------------------------------------------
# Summary test — informational overview of all regression clips
# ---------------------------------------------------------------------------

def test_regression_summary(local_cfg):
    """Run all regression clips and report a summary table.

    This test always passes — it's informational. The parametrized tests
    above enforce correctness. This provides a convenient overview with -v -s.
    """
    results = []
    for filename, info in sorted(REGRESSION_CLIPS.items()):
        wav_path = os.path.join(CLASSIFICATION_DIR, filename)
        if not os.path.exists(wav_path):
            results.append((filename, info["expected"], "SKIP", info["status"]))
            continue

        result = analyze_clip(
            wav_path, local_cfg["detection"], local_cfg["audio"],
        )
        actual = result["dominant"]
        match = "PASS" if actual == info["expected"] else "FAIL"
        results.append((
            filename,
            info["expected"],
            actual if match == "FAIL" else match,
            info["status"],
        ))

    # Print summary table (visible with pytest -v -s)
    print("\n")
    print(f"{'File':<30}  {'Expected':<25}  {'Result':<25}  {'Status':<8}")
    print("-" * 95)
    for filename, expected, result, status in results:
        marker = "✓" if result == "PASS" else ("⊘" if result == "SKIP" else "✗")
        print(f"{filename:<30}  {expected:<25}  {marker} {result:<23}  {status}")

    failures = [r for r in results if r[2] not in ("PASS", "SKIP")]
    print(f"\n{len(results)} clips checked: "
          f"{sum(1 for r in results if r[2] == 'PASS')} passed, "
          f"{len(failures)} failed, "
          f"{sum(1 for r in results if r[2] == 'SKIP')} skipped")
