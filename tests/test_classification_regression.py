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
    "birdsong-chorus.wav": {
        "expected": "birdsong+",
        "status": "locked",
        "note": "Real outdoor multi-species chorus (wrens, mourning doves, "
                "robins). Path D (temporal highband variance) catches 77/165 "
                "blocks; Path A catches 1. Previously only 1/165 — chorus "
                "env_std median 4.9 far exceeds Path A's 1.0 ceiling. "
                "Key discriminators: window-wide lowband ≤ 0.12 (rejects "
                "mower/thunder), highband std ≥ 0.10 (rejects steady-state). "
                "Key calibration clip for the chorus detection boundary.",
    },
    "birdsong-morning.wav": {
        "expected": "birdsong",
        "status": "locked",
        "note": "Real outdoor multi-bird morning chorus including robins. "
                "130/141 blocks classify as birdsong via Path A (sustained). "
                "env_std 0.13–0.77, highband 0.59–0.87, midband 0.06–0.22. "
                "Key calibration clip for birdsong_amplitude_std_max (1.0).",
    },
    "mower-electric.wav": {
        "expected": "weedwhacker+",
        "status": "pending",
        "note": "Real outdoor electric mower recording. Classifies as "
                "weedwhacker due to similar high-frequency whine profile — "
                "a reasonable misclassification that may warrant a broader "
                "'lawn equipment' category. 2/68 blocks weedwhacker, rest "
                "unknown. Centroid 3800–7200 Hz, very low lowband (0.01–0.05), "
                "high env_std during passes.",
    },
    "mower-gas.wav": {
        "expected": "mower+",
        "status": "pending",
        "note": "Real outdoor gas mower recording. Centroid averages "
                "5000–7000 Hz — far above mower_centroid_max of 4000 — so "
                "only 1/51 blocks hits mower (centroid 3920 at block 34). "
                "Was previously misclassified as birdsong (multiple) before "
                "tightening birdsong_amplitude_std_max from 3.0 to 1.0. "
                "CURRENTLY BROKEN (pre-v14): block 34's dBA is 65.2, below "
                "mower_min_db of 70.0, so zero blocks match any filter. "
                "Fix options: (a) lower mower_min_db to ~60 (risk: quiet "
                "HVAC/fan false positives), (b) raise mower_centroid_max "
                "above 4000 to catch more blocks that DO exceed 70 dBA, "
                "(c) both. Option (b) is preferred — 35 out of 51 blocks "
                "have centroid ≤ 7000 and dBA ≥ 70 but centroid > 4000.",
    },
    "rain.wav": {
        "expected": "rain (multiple)",
        "status": "locked",
        "note": "Real outdoor heavy rain recording. 15/29 blocks classify as "
                "rain once amplitude stabilizes (env_std < 0.50). 2 early blocks "
                "hit mower during startup ramp (high env_std). Flatness 0.27–0.38, "
                "lowband 0.08–0.14, centroid 3130–4023. Key calibration clip for "
                "rain filter recalibration from uncalibrated 0.72 flatness threshold "
                "to real-world 0.27. Lowband minimum (0.07) is the primary separator "
                "from mower (lowband 0.02–0.06).",
    },
    "thunder-and-light-rain.wav": {
        "expected": "thunder (multiple)",
        "status": "locked",
        "note": "Spliced Sennheiser MKH 8020SP thunderstorm — mellow rumble "
                "with light rain.  Tests Path B sustained rumble detection and "
                "priority holdover breaking mower holdover.  55/170 blocks "
                "classify as thunder, 15 as mower.",
    },
    "thunder-cracks.wav": {
        "expected": "thunder (multiple)",
        "status": "locked",
        "note": "High-fidelity isolated thunder cracks with quiet gaps. "
                "Tests Path B crack detection. 8 thunder, 4 impulse blocks "
                "amid 85 quiet blocks — dominant is thunder because unknown/"
                "none blocks are excluded from the duration contest.",
    },
    # Diesel — real recording, filter recalibrated from actual spectral data
    "diesel-car.wav": {
        "expected": "diesel (multiple)",
        "status": "locked",
        "note": "Real diesel car at ~71 dBA. Calibration anchor for the "
                "recalibrated diesel filter (tonal harmonics, mid centroid). "
                "Steady-state: flatness 0.12–0.16, centroid 1441–2023. "
                "Most blocks hit diesel once min_history (8) is satisfied.",
    },
    # Amplified bass — real recording, neighbor playing boosted bass music
    "idiot-neighbor-mild-85db.wav": {
        "expected": "amplified_bass (multiple)",
        "status": "locked",
        "note": "Real neighbor playing boosted bass music through garage walls, "
                "measured at ~90 dB at the fenceline. 59 blocks. Lowband median "
                "0.538, mscore median 0.816, beat_confidence 0.728, centroid "
                "1200–1900. Previously 16 blocks stolen by rain, 7 by mower. "
                "The amplified_bass filter + music score guard on rain/mower "
                "corrects this. Key calibration clip for bass-through-walls.",
    },
    # Amplified bass — open windows/doors, broader spectrum than through walls
    "idiot-neighbor-medium-90db.wav": {
        "expected": "amplified_bass (multiple)",
        "status": "locked",
        "note": "Same neighbor, recorded with windows/doors open (~90 dB). "
                "More spectrum passes through — lowband median 0.19, mscore "
                "median 0.50, centroid median 2977. Requires the lowered "
                "thresholds (mscore 0.45, lowband 0.16) plus flatness floor "
                "(0.20) to classify correctly. 37/49 blocks amplified_bass.",
    },
    # Amplified bass + diesel truck overlay
    "idiot-neighbor-medium-with-truck.wav": {
        "expected": "amplified_bass (multiple)",
        "status": "locked",
        "note": "Same neighbor with open windows, plus diesel truck fired up "
                "midway. Truck broadband noise destroys beat confidence (median "
                "0.00) and suppresses mscore. 28/33 blocks classify as "
                "amplified_bass with the flatness-based diesel guard. Key test "
                "for multi-source scenarios where bconf is unreliable.",
    },
    # Wind — real outdoor recording with roof-edge whistle
    "wind-and-faint-windchimes.wav": {
        "expected": "wind (multiple)",
        "status": "locked",
        "note": "Real moderate wind with roof-edge whistle and faint windchimes. "
                "11 blocks. Broadband aero noise: centroid 3313–5858 Hz, "
                "flatness 0.32–0.55, lowband 0.15–0.26 (separates from rain). "
                "Low env_std (0–2.55). Early blocks show filter exploration "
                "(music_like, flyover, amplified_bass) before wind establishes "
                "from block 6. Key calibration clip for wind lowband_max (0.26).",
    },
    # Plane flyover — propeller-driven aircraft at recording distance
    "plane-flyover.wav": {
        "expected": "flyover (multiple)",
        "status": "locked",
        "note": "Real propeller plane flyover. 51 blocks showing Doppler approach/"
                "departure arc. Centroid 2508–10631, flatness 0.22–0.47, lowband "
                "0.16–0.47 (engine rumble). Some blocks stolen by wind (spectral "
                "overlap) and amplified_bass (mscore 0.46–0.79), but flyover "
                "dominates. Key test for beat confidence guard (bconf median 0.21 "
                "vs music's 0.38+) and highband ceiling (escaping mower blocks).",
    },
    # Engine speed launch — vehicle acceleration
    "engine-speed-launch.wav": {
        "expected": "flyover+",
        "status": "locked",
        "note": "Real vehicle acceleration/launch. 8 blocks. Very tonal mid-frequency "
                "engine (flatness 0.11–0.47, midband 0.33–0.73). Caught by flyover "
                "filter after blocks 2+ arrive. Low bconf (median 0.00 — RPM changes "
                "destroy periodicity). Midband penalty in music_like_score reduces "
                "false music classification. Originally tested as a diesel candidate "
                "but lowband too low (0.04–0.06) for diesel's lowband_min (0.10).",
    },
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
    """Generate pytest param IDs for readable test names.

    Clips with status "pending" are marked as expected failures (xfail) so
    they don't block the suite — we know the current classification is wrong
    and have documented the path forward in the clip's note.
    """
    params = []
    for filename, info in sorted(REGRESSION_CLIPS.items()):
        marks = []
        if info.get("status") == "pending":
            marks.append(pytest.mark.xfail(reason=f"pending: {filename}", strict=False))
        params.append(
            pytest.param(
                filename,
                info["expected"],
                id=f"{os.path.splitext(filename)[0]}_{info['expected'].replace(' ', '_')}",
                marks=marks,
            )
        )
    return params


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
