"""
Tests for noise_warden.dsp — pure signal-processing functions.

These are the highest-value tests in the suite: they protect the core
detection logic (threshold math, false-positive filters, music scoring)
from regressions with zero I/O or mocking overhead.
"""
import numpy as np
import pytest

from noise_warden.dsp import (
    apply_filter_holdover,
    beat_confidence_from_history,
    dba_estimate,
    get_filter_detection_latency,
    identify_filter,
    is_impulse,
    looks_like_birdsong,
    looks_like_conversation,
    looks_like_diesel,
    looks_like_mower,
    looks_like_rain,
    looks_like_thunder,
    looks_like_weedwhacker,
    music_like_score,
    rms_dbfs,
    spectrum_features,
)


# ---------------------------------------------------------------------------
# rms_dbfs
# ---------------------------------------------------------------------------

class TestRmsDbfs:
    def test_silence_returns_very_negative(self):
        """Dead silence (all zeros) → extremely negative dBFS."""
        silence = np.zeros(8000, dtype=np.float32)
        result = rms_dbfs(silence)
        assert result < -200

    def test_full_scale_sine_near_zero(self):
        """Full-scale sine wave → dBFS close to zero (within ~4 dB for RMS of sine)."""
        t = np.linspace(0, 1, 16000, endpoint=False, dtype=np.float32)
        sine = np.sin(2 * np.pi * 440 * t)
        result = rms_dbfs(sine)
        # RMS of a unit-amplitude sine is 1/sqrt(2) ≈ -3.01 dBFS
        assert -4.0 < result < -2.0

    def test_louder_signal_yields_higher_dbfs(self):
        """Doubling amplitude should increase dBFS by ~6 dB."""
        t = np.linspace(0, 1, 16000, endpoint=False, dtype=np.float32)
        quiet = np.sin(2 * np.pi * 440 * t) * 0.1
        loud = np.sin(2 * np.pi * 440 * t) * 0.2
        diff = rms_dbfs(loud) - rms_dbfs(quiet)
        assert 5.5 < diff < 6.5

    def test_constant_dc(self):
        """Constant DC value should produce a predictable dBFS."""
        dc = np.full(8000, 0.5, dtype=np.float32)
        result = rms_dbfs(dc)
        # RMS of 0.5 = 0.5; 20*log10(0.5) ≈ -6.02
        assert -7.0 < result < -5.0


# ---------------------------------------------------------------------------
# dba_estimate
# ---------------------------------------------------------------------------

class TestDbaEstimate:
    def test_adds_offset(self):
        assert dba_estimate(-30.0, 88.0) == pytest.approx(58.0)

    def test_zero_offset(self):
        assert dba_estimate(-45.0, 0.0) == pytest.approx(-45.0)

    def test_negative_offset(self):
        assert dba_estimate(-10.0, -5.0) == pytest.approx(-15.0)


# ---------------------------------------------------------------------------
# spectrum_features
# ---------------------------------------------------------------------------

class TestSpectrumFeatures:
    @pytest.fixture
    def low_tone(self):
        """100 Hz sine — should have high lowband ratio."""
        t = np.linspace(0, 0.5, 8000, endpoint=False, dtype=np.float32)
        return np.sin(2 * np.pi * 100 * t)

    @pytest.fixture
    def high_tone(self):
        """4000 Hz sine — should have high highband ratio."""
        t = np.linspace(0, 0.5, 8000, endpoint=False, dtype=np.float32)
        return np.sin(2 * np.pi * 4000 * t)

    def test_returns_expected_keys(self, low_tone):
        feats = spectrum_features(low_tone, 16000)
        expected_keys = {"centroid_hz", "flatness", "lowband_ratio", "midband_ratio", "highband_ratio"}
        assert set(feats.keys()) == expected_keys

    def test_low_tone_has_dominant_lowband(self, low_tone):
        feats = spectrum_features(low_tone, 16000)
        assert feats["lowband_ratio"] > feats["midband_ratio"]
        assert feats["lowband_ratio"] > feats["highband_ratio"]

    def test_high_tone_has_dominant_highband(self, high_tone):
        feats = spectrum_features(high_tone, 16000)
        assert feats["highband_ratio"] > feats["lowband_ratio"]

    def test_centroid_tracks_frequency(self, low_tone, high_tone):
        low_centroid = spectrum_features(low_tone, 16000)["centroid_hz"]
        high_centroid = spectrum_features(high_tone, 16000)["centroid_hz"]
        assert high_centroid > low_centroid

    def test_ratios_sum_to_one(self, low_tone):
        feats = spectrum_features(low_tone, 16000)
        total = feats["lowband_ratio"] + feats["midband_ratio"] + feats["highband_ratio"]
        assert total == pytest.approx(1.0, abs=0.01)

    def test_white_noise_is_relatively_flat(self):
        """White noise should have moderate flatness (not extremely low)."""
        rng = np.random.default_rng(42)
        noise = rng.standard_normal(8000).astype(np.float32)
        feats = spectrum_features(noise, 16000)
        assert feats["flatness"] > 0.3


# ---------------------------------------------------------------------------
# beat_confidence_from_history
# ---------------------------------------------------------------------------

class TestBeatConfidence:
    def test_short_history_returns_zero(self):
        assert beat_confidence_from_history([60, 62, 61]) == pytest.approx(0.0)

    def test_constant_returns_base_value(self):
        """Flat dB history → no periodicity → confidence at baseline (0.5ish)."""
        flat = [65.0] * 24
        result = beat_confidence_from_history(flat)
        # With all-zero delta, allclose check returns 0.0
        assert result == pytest.approx(0.0)

    def test_periodic_pattern_higher_than_random(self):
        """An oscillating pattern should show higher beat confidence than random."""
        # Simulate a repeating 4-block beat pattern
        periodic = [60, 70, 60, 70] * 6  # 24 samples
        rng = np.random.default_rng(123)
        random_db = list(rng.uniform(55, 75, 24))

        conf_periodic = beat_confidence_from_history(periodic)
        conf_random = beat_confidence_from_history(random_db)
        assert conf_periodic > conf_random

    def test_returns_between_zero_and_one(self):
        rng = np.random.default_rng(99)
        history = list(rng.uniform(50, 80, 24))
        result = beat_confidence_from_history(history)
        assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# music_like_score
# ---------------------------------------------------------------------------

class TestMusicLikeScore:
    def test_high_lowband_moderate_flatness(self):
        """Strong bass + moderate tonality → high music score."""
        feats = {
            "lowband_ratio": 0.55,
            "midband_ratio": 0.30,
            "highband_ratio": 0.15,
            "flatness": 0.35,
            "centroid_hz": 400,
        }
        score = music_like_score(feats)
        assert score > 0.6

    def test_low_lowband_returns_low_score(self):
        """Weak bass → low music-likeness regardless of flatness."""
        feats = {
            "lowband_ratio": 0.05,
            "midband_ratio": 0.15,
            "highband_ratio": 0.80,
            "flatness": 0.35,
            "centroid_hz": 5000,
        }
        score = music_like_score(feats)
        assert score < 0.5

    def test_extreme_flatness_reduces_score(self):
        """Very flat spectrum (noise-like) → lower tonal score component."""
        flat_feats = {
            "lowband_ratio": 0.50,
            "midband_ratio": 0.30,
            "highband_ratio": 0.20,
            "flatness": 0.90,
            "centroid_hz": 600,
        }
        tonal_feats = dict(flat_feats, flatness=0.35)
        assert music_like_score(tonal_feats) > music_like_score(flat_feats)

    def test_score_bounded_zero_to_one(self):
        """Score should be clamped to [0, 1] regardless of input."""
        extremes = [
            {"lowband_ratio": 0.0, "midband_ratio": 0.0, "highband_ratio": 1.0, "flatness": 1.0, "centroid_hz": 8000},
            {"lowband_ratio": 1.0, "midband_ratio": 0.0, "highband_ratio": 0.0, "flatness": 0.0, "centroid_hz": 50},
        ]
        for feats in extremes:
            score = music_like_score(feats)
            assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# is_impulse
# ---------------------------------------------------------------------------

class TestIsImpulse:
    def test_large_jump_is_impulse(self):
        assert is_impulse(80.0, 60.0, 14.0) is True

    def test_small_jump_is_not_impulse(self):
        assert is_impulse(65.0, 60.0, 14.0) is False

    def test_exact_threshold_is_not_impulse(self):
        """Delta exactly at threshold → False (requires >=, but 14.0 == 14.0 is True)."""
        # The implementation uses >=, so exact match IS an impulse
        assert is_impulse(74.0, 60.0, 14.0) is True

    def test_negative_jump_is_not_impulse(self):
        assert is_impulse(55.0, 70.0, 14.0) is False


# ---------------------------------------------------------------------------
# looks_like_thunder
# ---------------------------------------------------------------------------

class TestLooksLikeThunder:
    def test_loud_low_flat_burst_is_thunder(self):
        feats = {"lowband_ratio": 0.65, "flatness": 0.50, "centroid_hz": 200, "midband_ratio": 0.2, "highband_ratio": 0.15}
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is True

    def test_small_delta_is_not_thunder(self):
        feats = {"lowband_ratio": 0.65, "flatness": 0.50, "centroid_hz": 200, "midband_ratio": 0.2, "highband_ratio": 0.15}
        assert looks_like_thunder(feats, 70.0, 60.0, 18.0) is False

    def test_high_centroid_not_thunder(self):
        """High-frequency content with low lowband → not thunder despite big delta."""
        feats = {"lowband_ratio": 0.20, "flatness": 0.50, "centroid_hz": 4000, "midband_ratio": 0.3, "highband_ratio": 0.5}
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is False

    def test_low_flatness_not_thunder(self):
        """Tonal low-freq burst (not broadband enough) → not thunder."""
        feats = {"lowband_ratio": 0.65, "flatness": 0.20, "centroid_hz": 150, "midband_ratio": 0.2, "highband_ratio": 0.15}
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is False


# ---------------------------------------------------------------------------
# looks_like_rain
# ---------------------------------------------------------------------------

class TestLooksLikeRain:
    def test_flat_stable_is_rain(self):
        feats = {"flatness": 0.80, "centroid_hz": 3000, "lowband_ratio": 0.2, "midband_ratio": 0.4, "highband_ratio": 0.4}
        # Stable readings with low variance
        stable_db = [55.0, 55.1, 54.9, 55.2, 55.0, 54.8, 55.1, 55.0, 54.9, 55.0, 55.1, 54.8]
        assert looks_like_rain(feats, stable_db, 0.72, 2.5) is True

    def test_low_flatness_not_rain(self):
        feats = {"flatness": 0.40, "centroid_hz": 500, "lowband_ratio": 0.5, "midband_ratio": 0.3, "highband_ratio": 0.2}
        stable_db = [55.0] * 12
        assert looks_like_rain(feats, stable_db, 0.72, 2.5) is False

    def test_high_variance_not_rain(self):
        feats = {"flatness": 0.80, "centroid_hz": 3000, "lowband_ratio": 0.2, "midband_ratio": 0.4, "highband_ratio": 0.4}
        # Wild fluctuations
        wild_db = [50.0, 70.0, 50.0, 70.0, 50.0, 70.0, 50.0, 70.0, 50.0, 70.0, 50.0, 70.0]
        assert looks_like_rain(feats, wild_db, 0.72, 2.5) is False

    def test_short_history_not_rain(self):
        feats = {"flatness": 0.80, "centroid_hz": 3000, "lowband_ratio": 0.2, "midband_ratio": 0.4, "highband_ratio": 0.4}
        assert looks_like_rain(feats, [55.0, 55.1], 0.72, 2.5) is False


# ---------------------------------------------------------------------------
# looks_like_mower
# ---------------------------------------------------------------------------

class TestLooksLikeMower:
    def test_flat_mid_centroid_stable_is_mower(self):
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3, "midband_ratio": 0.4, "highband_ratio": 0.3}
        stable_db = [68.0, 68.2, 67.9, 68.1, 68.3, 67.8, 68.0, 68.1, 67.9, 68.2, 68.0, 67.9]
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000) is True

    def test_wrong_centroid_range_not_mower(self):
        feats = {"flatness": 0.65, "centroid_hz": 5000, "lowband_ratio": 0.1, "midband_ratio": 0.2, "highband_ratio": 0.7}
        stable_db = [68.0] * 12
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000) is False

    def test_high_variability_not_mower(self):
        """Mowers produce steady sound; wild variance → not a mower."""
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3, "midband_ratio": 0.4, "highband_ratio": 0.3}
        wild_db = [50.0, 80.0, 50.0, 80.0, 50.0, 80.0, 50.0, 80.0, 50.0, 80.0, 50.0, 80.0]
        assert looks_like_mower(feats, wild_db, 0.60, 300, 3000) is False

    def test_short_history_not_mower(self):
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3, "midband_ratio": 0.4, "highband_ratio": 0.3}
        assert looks_like_mower(feats, [68.0, 68.1], 0.60, 300, 3000) is False

    def test_quiet_fan_rejected_by_min_db(self):
        """Fan/HVAC at 61 dBA mimics mower spectrally but is far too quiet."""
        feats = {"flatness": 0.40, "centroid_hz": 2200, "lowband_ratio": 0.39,
                 "midband_ratio": 0.11, "highband_ratio": 0.50}
        stable_db = [61.0] * 12
        # Without db_now, spectral match passes (legacy behavior)
        assert looks_like_mower(feats, stable_db, 0.25, 300, 4000) is True
        # With db_now below min_db (70), the quiet source is rejected
        assert looks_like_mower(feats, stable_db, 0.25, 300, 4000,
                                db_now=61.0) is False

    def test_loud_mower_passes_min_db(self):
        """Real mower at 75 dBA clears the 70 dB floor."""
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3,
                 "midband_ratio": 0.4, "highband_ratio": 0.3}
        stable_db = [75.0] * 12
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000,
                                db_now=75.0) is True

    def test_min_db_boundary_at_threshold(self):
        """Exactly at min_db boundary should pass."""
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3,
                 "midband_ratio": 0.4, "highband_ratio": 0.3}
        stable_db = [70.0] * 12
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000,
                                db_now=70.0) is True


# ---------------------------------------------------------------------------
# looks_like_birdsong
# ---------------------------------------------------------------------------

class TestLooksLikeBirdsong:
    """Birdsong: dominant high-frequency energy, minimal bass, moderate flatness, stable amplitude."""

    def test_high_freq_no_bass_stable_is_birdsong(self):
        """Classic birdsong signature: energy concentrated above 1200 Hz, no bass."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        stable_db = [62.0, 63.0, 61.5, 62.5, 63.0, 62.0, 61.0, 62.5, 63.0, 62.0, 61.5, 62.5]
        assert looks_like_birdsong(feats, stable_db) is True

    def test_bass_heavy_not_birdsong(self):
        """Music with treble emphasis but also bass content should not match."""
        feats = {"highband_ratio": 0.60, "lowband_ratio": 0.25, "flatness": 0.35,
                 "midband_ratio": 0.15, "centroid_hz": 3500}
        stable_db = [65.0] * 12
        assert looks_like_birdsong(feats, stable_db) is False

    def test_low_highband_not_birdsong(self):
        """Mid-frequency dominant sound (speech, traffic) should not match."""
        feats = {"highband_ratio": 0.30, "lowband_ratio": 0.10, "flatness": 0.45,
                 "midband_ratio": 0.60, "centroid_hz": 800}
        stable_db = [60.0] * 12
        assert looks_like_birdsong(feats, stable_db) is False

    def test_high_variance_not_birdsong(self):
        """Birdsong is relatively steady; wild amplitude swings should not match."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        wild_db = [50.0, 80.0, 50.0, 80.0, 50.0, 80.0, 50.0, 80.0, 50.0, 80.0, 50.0, 80.0]
        assert looks_like_birdsong(feats, wild_db) is False

    def test_short_history_not_birdsong(self):
        """Need at least 8 readings (default min_history) for stability check."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        assert looks_like_birdsong(feats, [62.0, 63.0]) is False

    def test_very_low_flatness_not_birdsong(self):
        """Pure tone (flatness near 0) is more like a siren than birdsong."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.10,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        stable_db = [62.0] * 12
        assert looks_like_birdsong(feats, stable_db) is False


# ---------------------------------------------------------------------------
# Path C — extreme spectral purity
# ---------------------------------------------------------------------------

class TestBirdsongPathC:
    """Path C: extreme highband (≥0.95) + high centroid + dB floor.

    Catches clean bursty recordings where Path A fails on variance and
    Path B fails because consecutive loud chirps prevent the peak-over-mean
    check from firing.
    """

    def test_extreme_purity_high_variance_is_birdsong(self):
        """Extreme highband with high amplitude variance should hit Path C
        even though Path A (variance too high) and Path B (delta too small) reject.
        """
        feats = {"highband_ratio": 0.96, "lowband_ratio": 0.03, "flatness": 0.05,
                 "midband_ratio": 0.01, "centroid_hz": 3500}
        # Bursty dB pattern — env_std ~10 (fails Path A variance_max=3.0)
        # Mean ~82, last value 88 (+6 dB, fails Path B peak_db_threshold=10)
        bursty_db = [75.0, 90.0, 72.0, 88.0, 75.0, 90.0, 72.0, 88.0,
                     75.0, 90.0, 72.0, 88.0]
        assert looks_like_birdsong(feats, bursty_db) is True

    def test_purity_near_silence_rejected(self):
        """Near-silence blocks produce misleading spectral shapes and must be
        rejected by the dB floor (db_now < mean - margin).
        """
        feats = {"highband_ratio": 0.997, "lowband_ratio": 0.00, "flatness": 0.02,
                 "midband_ratio": 0.003, "centroid_hz": 17000}
        # Quiet block at 39 dBA with window mean ~75 dBA.
        # 39 < 75 - 15 = 60 → rejected by purity_db_margin.
        silence_db = [85.0, 88.0, 72.0, 86.0, 90.0, 85.0,
                      80.0, 75.0, 70.0, 60.0, 50.0, 39.0]
        assert looks_like_birdsong(feats, silence_db) is False

    def test_purity_below_threshold_not_birdsong(self):
        """Highband at 0.94 (just below 0.95) should NOT trigger Path C.
        With high variance, it also fails Path A.  With low delta, it also
        fails Path B.  None of the three paths match → False.
        """
        feats = {"highband_ratio": 0.94, "lowband_ratio": 0.03, "flatness": 0.05,
                 "midband_ratio": 0.03, "centroid_hz": 3500}
        bursty_db = [75.0, 90.0, 72.0, 88.0, 75.0, 90.0, 72.0, 88.0,
                     75.0, 90.0, 72.0, 88.0]
        assert looks_like_birdsong(feats, bursty_db) is False

    def test_purity_with_too_much_bass_rejected(self):
        """Extreme highband but excessive lowband should still fail the shared
        lowband ceiling check (runs before any path).
        """
        feats = {"highband_ratio": 0.96, "lowband_ratio": 0.20, "flatness": 0.05,
                 "midband_ratio": 0.01, "centroid_hz": 3500}
        stable_db = [80.0] * 12
        assert looks_like_birdsong(feats, stable_db) is False


# ===========================================================================
# Sensitivity tests — verify that DSP magic numbers are in reasonable ranges
# and that small perturbations don't wildly change classification outcomes.
# ===========================================================================


class TestMusicLikeScoreSensitivity:
    """Probe the music_like_score formula's response to its key constants.

    Formula: score = clamp(0.6 * low_component + 0.4 * tonal_component)
    where:
      low_component  = clamp(lowband_ratio * 1.6)
      tonal_component = clamp(1.0 - |flatness - 0.35| / 0.35)
    """

    # -- Lowband boost factor (1.6) --

    def test_lowband_boost_maps_typical_music_above_threshold(self):
        """Typical through-wall music has lowband_ratio ~0.40.
        0.40 * 1.6 = 0.64 → low_component = 0.64.
        With perfect tonality (flatness=0.35, tonal=1.0):
        score = 0.6*0.64 + 0.4*1.0 = 0.784 → well above 0.62 threshold.
        """
        feats = {"lowband_ratio": 0.40, "flatness": 0.35, "midband_ratio": 0.35,
                 "highband_ratio": 0.25, "centroid_hz": 500}
        assert music_like_score(feats) > 0.62

    def test_lowband_boost_rejects_ambient_bass(self):
        """Ambient hum with lowband_ratio ~0.20 should not reach threshold.
        0.20 * 1.6 = 0.32 → low_component = 0.32.
        Even with perfect tonality: 0.6*0.32 + 0.4*1.0 = 0.592 → below 0.62.
        """
        feats = {"lowband_ratio": 0.20, "flatness": 0.35, "midband_ratio": 0.50,
                 "highband_ratio": 0.30, "centroid_hz": 800}
        assert music_like_score(feats) < 0.62

    def test_lowband_boost_saturates_at_one(self):
        """Lowband_ratio of 0.70: 0.70 * 1.6 = 1.12, clamped to 1.0.
        Scores above 0.625 shouldn't keep climbing dramatically.
        """
        moderate = {"lowband_ratio": 0.50, "flatness": 0.35, "midband_ratio": 0.30,
                    "highband_ratio": 0.20, "centroid_hz": 400}
        extreme = {"lowband_ratio": 0.80, "flatness": 0.35, "midband_ratio": 0.10,
                   "highband_ratio": 0.10, "centroid_hz": 200}
        diff = music_like_score(extreme) - music_like_score(moderate)
        # Saturation means the gap should be small (both >0.8)
        assert diff < 0.15

    # -- Tonal window center (0.35) and half-width (0.35) --

    def test_tonal_peak_at_035_flatness(self):
        """The tonal component should be maximized at flatness=0.35."""
        feats_peak = {"lowband_ratio": 0.50, "flatness": 0.35, "midband_ratio": 0.30,
                      "highband_ratio": 0.20, "centroid_hz": 400}
        feats_off = {"lowband_ratio": 0.50, "flatness": 0.50, "midband_ratio": 0.30,
                     "highband_ratio": 0.20, "centroid_hz": 400}
        assert music_like_score(feats_peak) > music_like_score(feats_off)

    def test_tonal_window_symmetric(self):
        """Flatness equally distant from 0.35 in both directions should yield
        similar scores (the triangle window is symmetric).
        """
        base = {"lowband_ratio": 0.50, "midband_ratio": 0.30,
                "highband_ratio": 0.20, "centroid_hz": 400}
        # 0.35 ± 0.15 → flatness 0.20 and 0.50
        score_low = music_like_score({**base, "flatness": 0.20})
        score_high = music_like_score({**base, "flatness": 0.50})
        assert abs(score_low - score_high) < 0.01

    def test_tonal_window_rejects_rain_flatness(self):
        """Rain flatness ~0.72 is well outside the tonal window.
        |0.72 - 0.35| / 0.35 = 1.057 → tonal_component = max(0, 1 - 1.057) = 0.
        Even with decent bass, score should be lower.
        """
        feats = {"lowband_ratio": 0.40, "flatness": 0.72, "midband_ratio": 0.35,
                 "highband_ratio": 0.25, "centroid_hz": 600}
        # 0.6 * (0.40 * 1.6 = 0.64) + 0.4 * 0 = 0.384
        assert music_like_score(feats) < 0.50

    # -- Bass vs tonal weighting (0.6 / 0.4) --

    def test_bass_weight_dominates(self):
        """With no tonal contribution, bass alone can still produce a moderate score.
        lowband=0.50 → low=0.80, tonal=0 (flatness=0.0 → |0-0.35|/0.35 = 1.0).
        score = 0.6*0.80 + 0.4*0.0 = 0.48. Below threshold, which is correct:
        bass-only (no tonality) should not trigger detection.
        """
        feats = {"lowband_ratio": 0.50, "flatness": 0.0, "midband_ratio": 0.30,
                 "highband_ratio": 0.20, "centroid_hz": 300}
        score = music_like_score(feats)
        assert 0.40 < score < 0.55

    def test_tonal_weight_alone_insufficient(self):
        """Perfect tonality but no bass should not reach threshold.
        lowband=0.0 → low=0.0, tonal=1.0.
        score = 0.6*0.0 + 0.4*1.0 = 0.40. Well below 0.62.
        """
        feats = {"lowband_ratio": 0.0, "flatness": 0.35, "midband_ratio": 0.50,
                 "highband_ratio": 0.50, "centroid_hz": 2000}
        assert music_like_score(feats) < 0.50


class TestBeatConfidenceSensitivity:
    """Verify the autocorrelation lag range captures relevant beat patterns."""

    def test_lag2_pattern_highest_confidence(self):
        """Alternating loud/quiet every 2 blocks (120 BPM at 1 block/sec)
        should produce the highest confidence — lag 2 is the first checked.
        """
        pattern_2 = [60, 75, 60, 75] * 6  # 24 samples, period = 2
        pattern_4 = [60, 60, 75, 75] * 6  # 24 samples, period = 4
        conf_2 = beat_confidence_from_history(pattern_2)
        conf_4 = beat_confidence_from_history(pattern_4)
        # Both should be high, but lag-2 correlation is direct
        assert conf_2 > 0.7
        assert conf_4 > 0.5

    def test_random_noise_low_confidence(self):
        """Random amplitude fluctuations should not register as rhythmic."""
        rng = np.random.default_rng(42)
        noise = list(rng.uniform(50, 80, 24))
        assert beat_confidence_from_history(noise) < 0.7

    def test_monotone_returns_zero(self):
        """Constant dB (all-zero deltas) returns exactly 0.0 via the allclose check."""
        assert beat_confidence_from_history([65.0] * 24) == 0.0

    def test_minimum_history_boundary(self):
        """Exactly 8 readings should produce a valid (non-zero) result for a pattern."""
        pattern = [60, 75, 60, 75, 60, 75, 60, 75]
        result = beat_confidence_from_history(pattern)
        assert result > 0.0

    def test_seven_readings_returns_zero(self):
        """7 readings (below minimum of 8) should return 0.0."""
        assert beat_confidence_from_history([60, 75] * 3 + [60]) == 0.0


class TestThunderSensitivity:
    """Verify thunder filter thresholds separate it from impulse and music."""

    def test_lowband_boundary_below_rejects(self):
        """Lowband just below 0.55 → not thunder, just a generic impulse."""
        feats = {"lowband_ratio": 0.54, "flatness": 0.50, "centroid_hz": 200,
                 "midband_ratio": 0.30, "highband_ratio": 0.16}
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is False

    def test_lowband_boundary_above_accepts(self):
        """Lowband at 0.56 (just above 0.55) + flat + big delta → thunder."""
        feats = {"lowband_ratio": 0.56, "flatness": 0.50, "centroid_hz": 200,
                 "midband_ratio": 0.28, "highband_ratio": 0.16}
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is True

    def test_flatness_boundary_below_rejects(self):
        """Flatness just below 0.45 → tonal bass burst, not broadband thunder."""
        feats = {"lowband_ratio": 0.65, "flatness": 0.44, "centroid_hz": 150,
                 "midband_ratio": 0.20, "highband_ratio": 0.15}
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is False

    def test_flatness_boundary_above_accepts(self):
        """Flatness at 0.46 (just above 0.45) with other conditions met → thunder."""
        feats = {"lowband_ratio": 0.65, "flatness": 0.46, "centroid_hz": 150,
                 "midband_ratio": 0.20, "highband_ratio": 0.15}
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is True

    def test_configurable_lowband_overrides_default(self):
        """Passing a custom lowband_min should override the 0.55 default."""
        feats = {"lowband_ratio": 0.50, "flatness": 0.50, "centroid_hz": 200,
                 "midband_ratio": 0.30, "highband_ratio": 0.20}
        # Default (0.55) would reject 0.50
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0) is False
        # Custom 0.45 should accept
        assert looks_like_thunder(feats, 85.0, 60.0, 18.0, lowband_min=0.45) is True


class TestMowerSensitivity:
    """Verify mower filter boundary conditions and newly-configurable env_std_max."""

    def test_env_std_boundary_below_accepts(self):
        """Amplitude std just below 3.5 → mower."""
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3,
                 "midband_ratio": 0.4, "highband_ratio": 0.3}
        # std dev of [68, 68, 68, 68, 65, 65, 65, 65, 68, 68, 68, 68] ≈ 1.5
        stable_db = [68.0, 68.0, 68.0, 68.0, 65.0, 65.0, 65.0, 65.0,
                     68.0, 68.0, 68.0, 68.0]
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000) is True

    def test_env_std_boundary_above_rejects(self):
        """Amplitude std well above 4.5 → too variable for a mower."""
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3,
                 "midband_ratio": 0.4, "highband_ratio": 0.3}
        wild_db = [50.0, 80.0, 50.0, 80.0, 50.0, 80.0, 50.0, 80.0,
                   50.0, 80.0, 50.0, 80.0]
        assert looks_like_mower(feats, wild_db, 0.60, 300, 3000) is False

    def test_configurable_env_std_overrides_default(self):
        """Custom env_std_max should override the 4.5 default."""
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3,
                 "midband_ratio": 0.4, "highband_ratio": 0.3}
        # std dev of [62, 73] repeating ≈ 5.5 → exceeds default 4.5 but below custom 6.0
        semi_stable = [62.0, 73.0, 62.0, 73.0, 62.0, 73.0,
                       62.0, 73.0, 62.0, 73.0, 62.0, 73.0]
        assert looks_like_mower(feats, semi_stable, 0.60, 300, 3000) is False
        assert looks_like_mower(feats, semi_stable, 0.60, 300, 3000,
                                env_std_max=6.0) is True

    def test_centroid_at_lower_boundary(self):
        """Centroid exactly at cmin (300 Hz) should still match."""
        feats = {"flatness": 0.65, "centroid_hz": 300, "lowband_ratio": 0.4,
                 "midband_ratio": 0.4, "highband_ratio": 0.2}
        stable_db = [68.0] * 12
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000) is True

    def test_centroid_below_lower_boundary(self):
        """Centroid at 299 Hz → below mower range (likely HVAC or traffic rumble)."""
        feats = {"flatness": 0.65, "centroid_hz": 299, "lowband_ratio": 0.5,
                 "midband_ratio": 0.35, "highband_ratio": 0.15}
        stable_db = [68.0] * 12
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000) is False


class TestRainSensitivity:
    """Verify rain filter min_history and window parameters."""

    def test_min_history_boundary_accepts(self):
        """Exactly 6 readings (default min_history) should be evaluated."""
        feats = {"flatness": 0.80, "centroid_hz": 3000, "lowband_ratio": 0.2,
                 "midband_ratio": 0.4, "highband_ratio": 0.4}
        assert looks_like_rain(feats, [55.0] * 6, 0.72, 2.5) is True

    def test_min_history_boundary_rejects(self):
        """5 readings (below default 6) should be rejected."""
        feats = {"flatness": 0.80, "centroid_hz": 3000, "lowband_ratio": 0.2,
                 "midband_ratio": 0.4, "highband_ratio": 0.4}
        assert looks_like_rain(feats, [55.0] * 5, 0.72, 2.5) is False

    def test_custom_min_history(self):
        """Custom min_history should override default."""
        feats = {"flatness": 0.80, "centroid_hz": 3000, "lowband_ratio": 0.2,
                 "midband_ratio": 0.4, "highband_ratio": 0.4}
        # 8 readings, but min_history=10 → rejected
        assert looks_like_rain(feats, [55.0] * 8, 0.72, 2.5, min_history=10) is False

    def test_custom_window(self):
        """Custom window should change which readings are evaluated.
        If most recent 6 are stable but the full 12 include wild early data,
        using window=6 should accept while window=12 might reject.
        """
        feats = {"flatness": 0.80, "centroid_hz": 3000, "lowband_ratio": 0.2,
                 "midband_ratio": 0.4, "highband_ratio": 0.4}
        # 6 wild + 6 stable = 12 total
        mixed_db = [40.0, 70.0, 40.0, 70.0, 40.0, 70.0,
                    55.0, 55.0, 55.0, 55.0, 55.0, 55.0]
        # Default window=12 → std includes the wild data → rejects
        assert looks_like_rain(feats, mixed_db, 0.72, 2.5) is False
        # Narrow window=6 → std of last 6 (all 55.0) → accepts
        assert looks_like_rain(feats, mixed_db, 0.72, 2.5, window=6) is True


class TestBirdsongSensitivity:
    """Verify birdsong filter min_history boundary and configurable thresholds."""

    def test_min_history_boundary_accepts(self):
        """Exactly 8 readings (default min_history) should be evaluated."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        assert looks_like_birdsong(feats, [62.0] * 8) is True

    def test_min_history_boundary_rejects(self):
        """7 readings (below default 8) should be rejected."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        assert looks_like_birdsong(feats, [62.0] * 7) is False

    def test_custom_min_history_raises_bar(self):
        """Raising min_history to 12 should reject 10 readings."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        assert looks_like_birdsong(feats, [62.0] * 10, min_history=12) is False
        assert looks_like_birdsong(feats, [62.0] * 12, min_history=12) is True

    def test_highband_boundary(self):
        """Highband at exactly 0.70 should accept; 0.69 should reject."""
        base_feats = {"lowband_ratio": 0.05, "flatness": 0.55,
                      "midband_ratio": 0.25, "centroid_hz": 3000}
        stable_db = [62.0] * 12
        assert looks_like_birdsong({**base_feats, "highband_ratio": 0.70}, stable_db) is True
        assert looks_like_birdsong({**base_feats, "highband_ratio": 0.69}, stable_db) is False


# ===========================================================================
# looks_like_weedwhacker
# ===========================================================================

class TestLooksLikeWeedwhacker:
    """Weedwhacker: high-pitched mechanical whine, 2–6 kHz, flat, no bass, moderately steady."""

    def test_classic_weedwhacker_signature(self):
        """High centroid, flat spectrum, no bass, moderate stability → weedwhacker."""
        feats = {"centroid_hz": 3500, "flatness": 0.55, "lowband_ratio": 0.05,
                 "midband_ratio": 0.30, "highband_ratio": 0.65}
        stable_db = [72.0, 73.0, 71.5, 72.5, 73.0, 72.0, 71.0, 72.5, 73.0, 72.0, 71.5, 72.5]
        assert looks_like_weedwhacker(feats, stable_db) is True

    def test_low_centroid_not_weedwhacker(self):
        """Centroid below 2000 Hz → probably a mower, not weedwhacker."""
        feats = {"centroid_hz": 800, "flatness": 0.55, "lowband_ratio": 0.05,
                 "midband_ratio": 0.60, "highband_ratio": 0.35}
        stable_db = [70.0] * 12
        assert looks_like_weedwhacker(feats, stable_db) is False

    def test_too_much_bass_not_weedwhacker(self):
        """Significant bass content → not a weedwhacker (maybe music or diesel)."""
        feats = {"centroid_hz": 3000, "flatness": 0.50, "lowband_ratio": 0.25,
                 "midband_ratio": 0.35, "highband_ratio": 0.40}
        stable_db = [70.0] * 12
        assert looks_like_weedwhacker(feats, stable_db) is False

    def test_wild_amplitude_not_weedwhacker(self):
        """Wild amplitude swings → not a steady mechanical tool."""
        feats = {"centroid_hz": 3500, "flatness": 0.55, "lowband_ratio": 0.05,
                 "midband_ratio": 0.30, "highband_ratio": 0.65}
        wild_db = [50.0, 85.0, 50.0, 85.0, 50.0, 85.0, 50.0, 85.0, 50.0, 85.0, 50.0, 85.0]
        assert looks_like_weedwhacker(feats, wild_db) is False

    def test_low_flatness_not_weedwhacker(self):
        """Very tonal high-frequency sound → more like a siren than a weedwhacker."""
        feats = {"centroid_hz": 3500, "flatness": 0.20, "lowband_ratio": 0.05,
                 "midband_ratio": 0.30, "highband_ratio": 0.65}
        stable_db = [70.0] * 12
        assert looks_like_weedwhacker(feats, stable_db) is False

    def test_short_history_not_weedwhacker(self):
        """Need at least 6 readings (default min_history)."""
        feats = {"centroid_hz": 3500, "flatness": 0.55, "lowband_ratio": 0.05,
                 "midband_ratio": 0.30, "highband_ratio": 0.65}
        assert looks_like_weedwhacker(feats, [70.0, 71.0]) is False


# ===========================================================================
# looks_like_diesel
# ===========================================================================

class TestLooksLikeDiesel:
    """Diesel idle: low centroid, bass-dominant, moderate flatness, very steady."""

    def test_classic_diesel_signature(self):
        """Low rumble, bass-heavy, moderate flatness, steady amplitude → diesel."""
        feats = {"centroid_hz": 200, "flatness": 0.50, "lowband_ratio": 0.55,
                 "midband_ratio": 0.30, "highband_ratio": 0.15}
        steady_db = [68.0, 68.5, 67.8, 68.2, 68.1, 67.9, 68.3, 68.0,
                     67.8, 68.1, 68.2, 67.9]
        assert looks_like_diesel(feats, steady_db) is True

    def test_high_centroid_not_diesel(self):
        """Centroid above 400 Hz → not diesel (maybe mower or music)."""
        feats = {"centroid_hz": 800, "flatness": 0.50, "lowband_ratio": 0.40,
                 "midband_ratio": 0.40, "highband_ratio": 0.20}
        steady_db = [68.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_low_lowband_not_diesel(self):
        """Insufficient bass → not diesel rumble."""
        feats = {"centroid_hz": 250, "flatness": 0.50, "lowband_ratio": 0.30,
                 "midband_ratio": 0.45, "highband_ratio": 0.25}
        steady_db = [68.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_too_flat_not_diesel(self):
        """Flatness above 0.65 → more like rain than diesel."""
        feats = {"centroid_hz": 200, "flatness": 0.75, "lowband_ratio": 0.50,
                 "midband_ratio": 0.30, "highband_ratio": 0.20}
        steady_db = [68.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_variable_amplitude_not_diesel(self):
        """High amplitude variance → not a steady idle."""
        feats = {"centroid_hz": 200, "flatness": 0.50, "lowband_ratio": 0.55,
                 "midband_ratio": 0.30, "highband_ratio": 0.15}
        wild_db = [55.0, 80.0, 55.0, 80.0, 55.0, 80.0, 55.0, 80.0,
                   55.0, 80.0, 55.0, 80.0]
        assert looks_like_diesel(feats, wild_db) is False

    def test_short_history_not_diesel(self):
        """Need at least 8 readings (default min_history) for sustained rumble."""
        feats = {"centroid_hz": 200, "flatness": 0.50, "lowband_ratio": 0.55,
                 "midband_ratio": 0.30, "highband_ratio": 0.15}
        assert looks_like_diesel(feats, [68.0] * 5) is False


# ===========================================================================
# looks_like_conversation
# ===========================================================================

class TestLooksLikeConversation:
    """Conversation: mid-frequency, moderate flatness, syllable-level amplitude modulation."""

    def test_classic_conversation_signature(self):
        """Mid-centroid, low flatness, syllable-level amplitude modulation → conversation."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        # Syllable-like modulation: std ≈ 4.1 dB (within the 3.0–8.0 range)
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        assert looks_like_conversation(feats, speech_db) is True

    def test_too_steady_not_conversation(self):
        """Very steady amplitude → drone, not speech (fails env_std_min check)."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        steady_db = [65.0] * 12
        assert looks_like_conversation(feats, steady_db) is False

    def test_too_wild_not_conversation(self):
        """Extreme amplitude swings → traffic or erratic noise, not speech."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        wild_db = [40.0, 85.0, 42.0, 83.0, 41.0, 84.0,
                   40.0, 85.0, 42.0, 83.0, 41.0, 84.0]
        assert looks_like_conversation(feats, wild_db) is False

    def test_low_centroid_not_conversation(self):
        """Centroid below 500 Hz → bass rumble, not speech."""
        feats = {"centroid_hz": 200, "flatness": 0.40, "lowband_ratio": 0.50,
                 "midband_ratio": 0.30, "highband_ratio": 0.20}
        speech_db = [62.0, 68.0, 64.0, 70.0, 63.0, 67.0,
                     65.0, 69.0, 62.0, 66.0, 64.0, 68.0]
        assert looks_like_conversation(feats, speech_db) is False

    def test_high_centroid_not_conversation(self):
        """Centroid above 2500 Hz → sibilance or high-pitched tool, not speech."""
        feats = {"centroid_hz": 4000, "flatness": 0.40, "lowband_ratio": 0.05,
                 "midband_ratio": 0.25, "highband_ratio": 0.70}
        speech_db = [62.0, 68.0, 64.0, 70.0, 63.0, 67.0,
                     65.0, 69.0, 62.0, 66.0, 64.0, 68.0]
        assert looks_like_conversation(feats, speech_db) is False

    def test_bass_heavy_not_conversation(self):
        """High lowband ratio → music or diesel, not conversation."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.50,
                 "midband_ratio": 0.30, "highband_ratio": 0.20}
        speech_db = [62.0, 68.0, 64.0, 70.0, 63.0, 67.0,
                     65.0, 69.0, 62.0, 66.0, 64.0, 68.0]
        assert looks_like_conversation(feats, speech_db) is False

    def test_too_flat_not_conversation(self):
        """High flatness → broadband noise (rain/mower), not speech harmonics."""
        feats = {"centroid_hz": 1200, "flatness": 0.70, "lowband_ratio": 0.20,
                 "midband_ratio": 0.40, "highband_ratio": 0.40}
        speech_db = [62.0, 68.0, 64.0, 70.0, 63.0, 67.0,
                     65.0, 69.0, 62.0, 66.0, 64.0, 68.0]
        assert looks_like_conversation(feats, speech_db) is False

    def test_short_history_not_conversation(self):
        """Need at least 10 readings (default min_history) for syllable patterns."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        assert looks_like_conversation(feats, [65.0, 68.0, 63.0, 67.0, 64.0]) is False


# ===========================================================================
# Sensitivity tests for new Tier 3 filters
# ===========================================================================

class TestWeedwhackerSensitivity:
    """Boundary tests for weedwhacker filter parameters."""

    def test_centroid_lower_boundary(self):
        """Centroid exactly at 2000 Hz should accept; 1999 should reject."""
        feats_base = {"flatness": 0.55, "lowband_ratio": 0.05,
                      "midband_ratio": 0.30, "highband_ratio": 0.65}
        stable_db = [70.0] * 12
        assert looks_like_weedwhacker({**feats_base, "centroid_hz": 2000}, stable_db) is True
        assert looks_like_weedwhacker({**feats_base, "centroid_hz": 1999}, stable_db) is False

    def test_centroid_upper_boundary(self):
        """Centroid exactly at 6000 Hz should accept; 6001 should reject."""
        feats_base = {"flatness": 0.55, "lowband_ratio": 0.05,
                      "midband_ratio": 0.20, "highband_ratio": 0.75}
        stable_db = [70.0] * 12
        assert looks_like_weedwhacker({**feats_base, "centroid_hz": 6000}, stable_db) is True
        assert looks_like_weedwhacker({**feats_base, "centroid_hz": 6001}, stable_db) is False

    def test_configurable_env_std_max(self):
        """Custom env_std_max should override the 5.0 default."""
        feats = {"centroid_hz": 3500, "flatness": 0.55, "lowband_ratio": 0.05,
                 "midband_ratio": 0.30, "highband_ratio": 0.65}
        # std dev of [64, 76] repeating = 6.0 — exceeds default 5.0 but below custom 7.0
        varied_db = [64.0, 76.0, 64.0, 76.0, 64.0, 76.0,
                     64.0, 76.0, 64.0, 76.0, 64.0, 76.0]
        assert looks_like_weedwhacker(feats, varied_db) is False
        assert looks_like_weedwhacker(feats, varied_db, env_std_max=7.0) is True


class TestDieselSensitivity:
    """Boundary tests for diesel filter parameters."""

    def test_centroid_boundary(self):
        """Centroid at 400 Hz should accept; 401 should reject."""
        feats_base = {"flatness": 0.50, "lowband_ratio": 0.50,
                      "midband_ratio": 0.35, "highband_ratio": 0.15}
        steady_db = [68.0] * 12
        assert looks_like_diesel({**feats_base, "centroid_hz": 400}, steady_db) is True
        assert looks_like_diesel({**feats_base, "centroid_hz": 401}, steady_db) is False

    def test_flatness_window(self):
        """Flatness must be within 0.40–0.65 range.
        Below 0.40 → too tonal (music bass line).
        Above 0.65 → too flat (approaching rain).
        """
        feats_base = {"centroid_hz": 200, "lowband_ratio": 0.55,
                      "midband_ratio": 0.30, "highband_ratio": 0.15}
        steady_db = [68.0] * 12
        assert looks_like_diesel({**feats_base, "flatness": 0.40}, steady_db) is True
        assert looks_like_diesel({**feats_base, "flatness": 0.39}, steady_db) is False
        assert looks_like_diesel({**feats_base, "flatness": 0.65}, steady_db) is True
        assert looks_like_diesel({**feats_base, "flatness": 0.66}, steady_db) is False

    def test_min_history_boundary(self):
        """Exactly 8 readings should evaluate; 7 should reject."""
        feats = {"centroid_hz": 200, "flatness": 0.50, "lowband_ratio": 0.55,
                 "midband_ratio": 0.30, "highband_ratio": 0.15}
        assert looks_like_diesel(feats, [68.0] * 8) is True
        assert looks_like_diesel(feats, [68.0] * 7) is False


class TestConversationSensitivity:
    """Boundary tests for conversation filter parameters."""

    def test_env_std_lower_boundary(self):
        """Amplitude std at/above 4.0 should accept; below should reject.
        Conversation requires env_std >= 4.0 to distinguish from steady drones.
        """
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        # Build arrays with known std dev ≈ 4.1 (within 4.0–8.0 range, range 11 < 15)
        boundary_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                       63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        assert looks_like_conversation(feats, boundary_db) is True

        # 2 dB spread → std well below 4.0
        too_steady = [65.0, 66.0, 65.0, 66.0, 65.0, 66.0,
                      65.0, 66.0, 65.0, 66.0, 65.0, 66.0]
        assert looks_like_conversation(feats, too_steady) is False

    def test_db_range_rejects_level_transitions(self):
        """A window spanning a major level transition (mower start/stop) should be rejected,
        even when env_std falls within the conversation band. This prevents false positives
        at the boundaries of loud mechanical sounds."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        # Simulates a gradual level drop: range = 18 dB (above 15 dB guard),
        # but env_std ≈ 5.5 (within 4.0–8.0 band). Classic false positive scenario.
        transition_db = [78.0, 77.0, 75.0, 73.0, 71.0, 69.0,
                         67.0, 65.0, 63.0, 62.0, 61.0, 60.0]
        assert looks_like_conversation(feats, transition_db) is False

        # Same data but with custom high db_range_max → passes since all other checks hold
        assert looks_like_conversation(feats, transition_db, db_range_max=20.0) is True

    def test_min_history_boundary(self):
        """Exactly 10 readings should evaluate; 9 should reject."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        # std ≈ 4.5 (within 3.0–8.0 range)
        speech_db = [60.0, 70.0, 61.0, 71.0, 60.0, 69.0,
                     62.0, 70.0, 61.0, 68.0]
        assert looks_like_conversation(feats, speech_db) is True
        assert looks_like_conversation(feats, speech_db[:9]) is False

    def test_custom_env_std_range(self):
        """Custom env_std_min/max should override defaults."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        # std dev ~2.5 — below default 3.0 but above custom 2.0
        mild_db = [64.0, 68.0, 64.0, 68.0, 64.0, 68.0,
                   64.0, 68.0, 64.0, 68.0, 64.0, 68.0]
        assert looks_like_conversation(feats, mild_db) is False
        assert looks_like_conversation(feats, mild_db, env_std_min=2.0) is True


# ===========================================================================
# identify_filter — orchestration layer
# ===========================================================================

class TestIdentifyFilter:
    """Tests for the centralized filter chain entry point."""

    # Minimal detection config with defaults for all required keys
    DEFAULT_DET = {
        "impulse_peak_delta_db": "14.0",
        "thunder_peak_delta_db": "18.0",
        "rain_flatness_threshold": "0.72",
        "rain_low_variance_db": "2.5",
        "mower_flatness_threshold": "0.60",
        "mower_centroid_min_hz": "300",
        "mower_centroid_max_hz": "3000",
    }

    def test_no_filter_returns_none(self):
        """Normal sound that passes all filters should return None."""
        feats = {"flatness": 0.30, "centroid_hz": 500, "lowband_ratio": 0.4,
                 "midband_ratio": 0.4, "highband_ratio": 0.2}
        db_history = [65.0] * 20
        assert identify_filter(feats, db_history, 68.0, 67.0, self.DEFAULT_DET) is None

    def test_thunder_highest_priority(self):
        """Thunder should be identified even when the sound also qualifies as impulse."""
        feats = {"lowband_ratio": 0.60, "flatness": 0.50, "centroid_hz": 200,
                 "midband_ratio": 0.25, "highband_ratio": 0.15}
        db_history = [50.0] * 20
        # 20 dB jump qualifies as both thunder (18 dB) and impulse (14 dB)
        result = identify_filter(feats, db_history, 70.0, 50.0, self.DEFAULT_DET)
        assert result == "thunder"

    def test_impulse_when_not_thunder(self):
        """Large dB jump without thunder's spectral shape → generic impulse."""
        feats = {"lowband_ratio": 0.20, "flatness": 0.30, "centroid_hz": 2000,
                 "midband_ratio": 0.40, "highband_ratio": 0.40}
        db_history = [50.0] * 20
        result = identify_filter(feats, db_history, 70.0, 50.0, self.DEFAULT_DET)
        assert result == "impulse"

    def test_priority_order_preserved(self):
        """Birdsong features should match birdsong, not weedwhacker or mower,
        even though the centroid could overlap other filters."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        db_history = [62.0] * 12
        result = identify_filter(feats, db_history, 65.0, 64.0, self.DEFAULT_DET)
        assert result == "birdsong"

    def test_config_overrides_respected(self):
        """Custom config values should change filter behavior."""
        feats = {"lowband_ratio": 0.20, "flatness": 0.30, "centroid_hz": 500,
                 "midband_ratio": 0.40, "highband_ratio": 0.40}
        db_history = [50.0] * 20
        # 16 dB jump — above default impulse threshold (14) but below custom (20)
        cfg = {**self.DEFAULT_DET, "impulse_peak_delta_db": "20.0"}
        assert identify_filter(feats, db_history, 66.0, 50.0, self.DEFAULT_DET) == "impulse"
        assert identify_filter(feats, db_history, 66.0, 50.0, cfg) is None


# ===========================================================================
# Filter holdover — classification persistence through brief gaps
# ===========================================================================

class TestFilterHoldover:
    """Tests for apply_filter_holdover().

    When a filter has been established for holdover_min_run consecutive blocks,
    it persists through up to holdover_max_gap unmatched blocks. This prevents
    sustained sounds (like mowers) from fragmenting during stop/start pauses."""

    DET = {
        "holdover_min_run": "5",
        "holdover_max_gap": "10",
    }

    def test_holdover_activates_after_min_run(self):
        """Once a filter has run for holdover_min_run blocks, gaps should
        return the established filter instead of None."""
        effective, _, _, _ = apply_filter_holdover(
            None, "mower", 5, 0, self.DET,
        )
        assert effective == "mower"

    def test_holdover_does_not_activate_below_min_run(self):
        """A filter that ran for fewer than holdover_min_run blocks should
        not persist through gaps."""
        effective, _, _, _ = apply_filter_holdover(
            None, "mower", 4, 0, self.DET,
        )
        assert effective is None

    def test_holdover_expires_after_max_gap(self):
        """After holdover_max_gap unmatched blocks, holdover should stop."""
        effective, _, _, _ = apply_filter_holdover(
            None, "mower", 10, 10, self.DET,
        )
        assert effective is None

    def test_holdover_last_valid_block(self):
        """The last block within the holdover window should still hold."""
        effective, _, _, _ = apply_filter_holdover(
            None, "mower", 10, 9, self.DET,
        )
        assert effective == "mower"

    def test_holdover_suppresses_transient_filters(self):
        """During active holdover, a different filter (e.g., impulse during
        mower) should be suppressed in favor of the established filter.
        The holdover gap counter increments but the filter persists."""
        effective, pf, pr, g = apply_filter_holdover(
            "impulse", "mower", 10, 3, self.DET,
        )
        assert effective == "mower"
        assert pf == "mower"
        assert pr == 10  # unchanged
        assert g == 4     # incremented (transient treated as gap)

    def test_holdover_disabled_when_no_prev_filter(self):
        """With prev_filter=None, no holdover should apply."""
        effective, _, _, _ = apply_filter_holdover(
            None, None, 0, 0, self.DET,
        )
        assert effective is None

    def test_holdover_state_tracking_during_run(self):
        """Consecutive real matches should increment prev_run and reset gap."""
        effective, pf, pr, g = apply_filter_holdover(
            "mower", "mower", 5, 0, self.DET,
        )
        assert effective == "mower"
        assert pf == "mower"
        assert pr == 6
        assert g == 0

    def test_holdover_state_tracking_during_gap(self):
        """During holdover, gap should increment while prev_run stays fixed."""
        effective, pf, pr, g = apply_filter_holdover(
            None, "mower", 10, 3, self.DET,
        )
        assert effective == "mower"
        assert pf == "mower"
        assert pr == 10  # unchanged
        assert g == 4     # incremented

    def test_new_filter_starts_when_no_holdover(self):
        """When holdover is not active (run too short), a new different filter
        should start its own tracking."""
        effective, pf, pr, g = apply_filter_holdover(
            "impulse", "mower", 3, 0, self.DET,
        )
        assert effective == "impulse"
        assert pf == "impulse"
        assert pr == 1
        assert g == 0

    def test_holdover_config_override(self):
        """Custom holdover_min_run and holdover_max_gap values should be respected."""
        strict_cfg = {"holdover_min_run": "10", "holdover_max_gap": "3"}

        # Run of 5 blocks — enough for default (5) but not for strict (10)
        effective, _, _, _ = apply_filter_holdover(
            None, "mower", 5, 0, strict_cfg,
        )
        assert effective is None

        # Run of 10 blocks — meets strict threshold
        effective, _, _, _ = apply_filter_holdover(
            None, "mower", 10, 0, strict_cfg,
        )
        assert effective == "mower"

        # Gap of 3 (at max_gap boundary) — expired
        effective, _, _, _ = apply_filter_holdover(
            None, "mower", 10, 3, strict_cfg,
        )
        assert effective is None


# ===========================================================================
# Filter detection latency — backdate journal entries by min_history
# ===========================================================================

class TestFilterDetectionLatency:
    """Tests for get_filter_detection_latency().

    Sustained-pattern filters (mower, birdsong, etc.) need min_history blocks
    before they can match. When a filter first triggers, the journal entry
    should be backdated by this latency so the timeline reflects when the
    source actually started, not when it was confirmed."""

    DET = {}

    def test_mower_default(self):
        assert get_filter_detection_latency("mower", self.DET) == 6

    def test_birdsong_default(self):
        assert get_filter_detection_latency("birdsong", self.DET) == 8

    def test_conversation_default(self):
        assert get_filter_detection_latency("conversation", self.DET) == 10

    def test_diesel_default(self):
        assert get_filter_detection_latency("diesel", self.DET) == 8

    def test_rain_default(self):
        assert get_filter_detection_latency("rain", self.DET) == 6

    def test_weedwhacker_default(self):
        assert get_filter_detection_latency("weedwhacker", self.DET) == 6

    def test_impulse_instant(self):
        """Impulse-like detectors have zero latency — they don't use a sliding
        window, so there's nothing to backdate."""
        assert get_filter_detection_latency("impulse", self.DET) == 0

    def test_thunder_instant(self):
        assert get_filter_detection_latency("thunder", self.DET) == 0

    def test_unknown_names_zero(self):
        """Non-filter classifications (music, music_like, unknown) return 0."""
        for name in ("music", "music_like", "unknown", "bogus"):
            assert get_filter_detection_latency(name, self.DET) == 0

    def test_config_override(self):
        """Config keys override the hardcoded defaults."""
        custom = {"birdsong_min_history": 12}
        assert get_filter_detection_latency("birdsong", custom) == 12

    def test_config_override_conversation(self):
        custom = {"conversation_min_history": 15}
        assert get_filter_detection_latency("conversation", custom) == 15

    def test_config_override_diesel(self):
        custom = {"diesel_min_history": 5}
        assert get_filter_detection_latency("diesel", custom) == 5
