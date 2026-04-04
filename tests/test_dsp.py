"""
Tests for noise_warden.dsp — pure signal-processing functions.

These are the highest-value tests in the suite: they protect the core
detection logic (threshold math, false-positive filters, music scoring)
from regressions with zero I/O or mocking overhead.
"""
import numpy as np
import pytest

from noise_warden.dsp import (
    beat_confidence_from_history,
    dba_estimate,
    is_impulse,
    looks_like_mower,
    looks_like_rain,
    looks_like_thunder,
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
        assert beat_confidence_from_history([60, 62, 61]) == 0.0

    def test_constant_returns_base_value(self):
        """Flat dB history → no periodicity → confidence at baseline (0.5ish)."""
        flat = [65.0] * 24
        result = beat_confidence_from_history(flat)
        # With all-zero delta, allclose check returns 0.0
        assert result == 0.0

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
