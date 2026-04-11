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
    beat_confidence,
    _inter_block_beat_confidence,
    dba_estimate,
    get_filter_detection_latency,
    identify_filter,
    intra_block_beat_confidence,
    is_impulse,
    looks_like_amplified_bass,
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
# _inter_block_beat_confidence
# ---------------------------------------------------------------------------

class TestInterBlockBeatConfidence:
    """Tests for the inter-block (macro-level) beat detection."""

    def test_short_history_returns_zero(self):
        assert _inter_block_beat_confidence([60, 62, 61]) == pytest.approx(0.0)

    def test_constant_returns_base_value(self):
        """Flat dB history → no periodicity → 0.0 (allclose guard trips)."""
        flat = [65.0] * 24
        result = _inter_block_beat_confidence(flat)
        assert result == pytest.approx(0.0)

    def test_periodic_pattern_higher_than_random(self):
        """An oscillating pattern should show higher beat confidence than random."""
        periodic = [60, 70, 60, 70] * 6  # 24 samples, period = 2
        rng = np.random.default_rng(123)
        random_db = list(rng.uniform(55, 75, 24))

        conf_periodic = _inter_block_beat_confidence(periodic)
        conf_random = _inter_block_beat_confidence(random_db)
        assert conf_periodic > conf_random

    def test_returns_between_zero_and_one(self):
        rng = np.random.default_rng(99)
        history = list(rng.uniform(50, 80, 24))
        result = _inter_block_beat_confidence(history)
        assert 0.0 <= result <= 1.0


class TestIntraBlockBeatConfidence:
    """Tests for intra-block beat detection (actual musical tempo within 1 block)."""

    SR = 22050  # Standard sample rate

    def _make_beat_block(self, bpm=120, sr=22050, amplitude=0.5, n_seconds=1):
        """Generate a synthetic block with amplitude pulses at the given BPM."""
        n_samples = sr * n_seconds
        t = np.arange(n_samples, dtype=np.float32) / sr
        # Amplitude envelope: periodic bumps at the beat frequency
        beat_freq = bpm / 60.0
        # Sharp pulses via half-rectified sine
        envelope = np.maximum(0, np.sin(2 * np.pi * beat_freq * t))
        # Modulate a carrier tone to create audible beats
        carrier = np.sin(2 * np.pi * 200 * t).astype(np.float32)
        return (carrier * envelope * amplitude).astype(np.float32)

    def test_rhythmic_block_high_confidence(self):
        """A block with clear 120 BPM beats should score high."""
        block = self._make_beat_block(bpm=120)
        result = intra_block_beat_confidence(block, self.SR)
        assert result > 0.90

    def test_silence_returns_zero(self):
        """A silent block should return 0.0."""
        block = np.zeros(self.SR, dtype=np.float32)
        result = intra_block_beat_confidence(block, self.SR)
        assert result == pytest.approx(0.0)

    def test_constant_tone_returns_zero(self):
        """A steady tone has negligible amplitude variation → 0.0.

        The CV guard catches this: a pure sine's frame-by-frame RMS
        varies only by float-precision phase alignment jitter."""
        t = np.arange(self.SR, dtype=np.float32) / self.SR
        block = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        result = intra_block_beat_confidence(block, self.SR)
        assert result == pytest.approx(0.0)

    def test_white_noise_low_confidence(self):
        """Random noise should not register as rhythmic."""
        rng = np.random.default_rng(42)
        block = rng.standard_normal(self.SR).astype(np.float32) * 0.3
        result = intra_block_beat_confidence(block, self.SR)
        assert result < 0.10

    def test_fast_tempo_detected(self):
        """180 BPM (upper range) should still be detectable."""
        block = self._make_beat_block(bpm=180)
        result = intra_block_beat_confidence(block, self.SR)
        assert result > 0.90

    def test_slow_tempo_detected(self):
        """100 BPM should produce non-trivial correlation, though only ~1.67
        beat cycles fit in a 1-second block, limiting autocorrelation strength."""
        block = self._make_beat_block(bpm=100)
        result = intra_block_beat_confidence(block, self.SR)
        assert result > 0.15

    def test_very_short_block_returns_zero(self):
        """A block shorter than 20 hop frames should return 0.0."""
        # At 10ms hop, need 20 frames = 200ms. 100ms block = too short.
        short_block = np.zeros(int(self.SR * 0.10), dtype=np.float32)
        result = intra_block_beat_confidence(short_block, self.SR)
        assert result == pytest.approx(0.0)

    def test_returns_between_zero_and_one(self):
        block = self._make_beat_block(bpm=120)
        result = intra_block_beat_confidence(block, self.SR)
        assert 0.0 <= result <= 1.0


class TestCombinedBeatConfidence:
    """Tests for beat_confidence() — intra-block only (inter-block removed).

    The inter-block component was dropped because it measures dB *stability*,
    not rhythm. Any steady source (mower, rain) produces high inter-block
    autocorrelation from consistent dB levels, inflating non-musical scores.
    """

    SR = 22050

    def test_equals_intra_block(self):
        """beat_confidence should return exactly the intra-block value."""
        t = np.arange(self.SR, dtype=np.float32) / self.SR
        beat_freq = 120 / 60.0
        envelope = np.maximum(0, np.sin(2 * np.pi * beat_freq * t))
        carrier = np.sin(2 * np.pi * 200 * t).astype(np.float32)
        block = (carrier * envelope * 0.5).astype(np.float32)
        db_history = [60, 70, 60, 70] * 6

        combined = beat_confidence(block, self.SR, db_history)
        intra = intra_block_beat_confidence(block, self.SR)

        assert combined == pytest.approx(intra)

    def test_ignores_inter_block(self):
        """Even with strong inter-block pattern, only intra matters."""
        block = np.zeros(self.SR, dtype=np.float32)
        db_history = [60, 70, 60, 70] * 6  # Strong inter-block pattern
        combined = beat_confidence(block, self.SR, db_history)
        # Silent block → intra is 0.0, and inter is ignored
        assert combined == pytest.approx(0.0)

    def test_accepts_db_history_param(self):
        """db_history param accepted for API compatibility even though unused."""
        t = np.arange(self.SR, dtype=np.float32) / self.SR
        beat_freq = 120 / 60.0
        envelope = np.maximum(0, np.sin(2 * np.pi * beat_freq * t))
        carrier = np.sin(2 * np.pi * 200 * t).astype(np.float32)
        block = (carrier * envelope * 0.5).astype(np.float32)

        # Should work with short, long, or empty history
        for history in [[], [65.0, 66.0], [70.0] * 100]:
            result = beat_confidence(block, self.SR, history)
            assert result > 0.0


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
# looks_like_amplified_bass
# ---------------------------------------------------------------------------

class TestLooksLikeAmplifiedBass:
    """Tests for the amplified bass / neighborhood thump filter."""

    # Real-world-ish bass music profile (neighbor garage at fenceline)
    BASS_FEATS = {
        "flatness": 0.35, "centroid_hz": 2800, "lowband_ratio": 0.47,
        "midband_ratio": 0.33, "highband_ratio": 0.20,
    }
    STABLE_DB = [78.0] * 12

    def test_bass_music_detected(self):
        """Real bass-through-walls profile: high mscore, high lowband, mid centroid."""
        assert looks_like_amplified_bass(
            self.BASS_FEATS, self.STABLE_DB) is True

    def test_low_music_score_rejected(self):
        """Low-tonal, low-bass sound shouldn't trigger (diesel-like, mscore ~0.30)."""
        feats = {"flatness": 0.12, "centroid_hz": 1800, "lowband_ratio": 0.15,
                 "midband_ratio": 0.50, "highband_ratio": 0.35}
        assert looks_like_amplified_bass(feats, self.STABLE_DB) is False

    def test_low_lowband_rejected(self):
        """Sound with lowband below threshold (e.g. weedwhacker, highband source)."""
        feats = {**self.BASS_FEATS, "lowband_ratio": 0.10}
        assert looks_like_amplified_bass(feats, self.STABLE_DB) is False

    def test_high_centroid_rejected(self):
        """Centroid above 4000 Hz — birdsong or whine, not bass music."""
        feats = {**self.BASS_FEATS, "centroid_hz": 5000}
        assert looks_like_amplified_bass(feats, self.STABLE_DB) is False

    def test_high_env_std_rejected(self):
        """Erratic amplitude — conversation or traffic, not steady bass thump."""
        wild_db = [70.0, 85.0, 70.0, 85.0, 70.0, 85.0, 70.0, 85.0, 70.0, 85.0, 70.0, 85.0]
        assert looks_like_amplified_bass(
            self.BASS_FEATS, wild_db) is False

    def test_short_history_rejected(self):
        """Not enough history to establish a pattern."""
        assert looks_like_amplified_bass(
            self.BASS_FEATS, [78.0, 78.0]) is False

    def test_boundary_at_min_music_score(self):
        """Block exactly at the min_music_score boundary should pass."""
        # mscore = 0.6 * clamp(lowband * 1.6) + 0.4 * triangle(flatness).
        # lowband=0.26 → 0.6*clamp(0.416)=0.250, flatness=0.35 → 0.4*1.0=0.400
        # mscore = 0.650 → above 0.60, should pass
        feats = {"flatness": 0.35, "centroid_hz": 2500, "lowband_ratio": 0.26,
                 "midband_ratio": 0.44, "highband_ratio": 0.30}
        assert looks_like_amplified_bass(feats, self.STABLE_DB) is True

    def test_low_beat_confidence_rejected(self):
        """When beat_confidence is below an explicitly set threshold, reject.

        The default min_beat_confidence is 0.0 (disabled), but when set
        explicitly (e.g., via config), low bconf should still reject.
        """
        assert looks_like_amplified_bass(
            self.BASS_FEATS, self.STABLE_DB,
            beat_confidence=0.10, min_beat_confidence=0.20) is False

    def test_beat_confidence_disabled_by_default(self):
        """Default min_beat_confidence=0.0 means any bconf passes.

        Open-window recordings + truck overlays destroy rhythm detection,
        so bconf is disabled by default. Flatness guards diesel instead.
        """
        assert looks_like_amplified_bass(
            self.BASS_FEATS, self.STABLE_DB, beat_confidence=0.0) is True

    def test_low_flatness_rejected(self):
        """Diesel engines have flatness median 0.151 — the flatness floor blocks them.

        This is the primary diesel guard since bconf was disabled (diesel
        bconf median 0.80 would easily pass a bconf gate).
        """
        diesel_like = {**self.BASS_FEATS, "flatness": 0.15}
        assert looks_like_amplified_bass(diesel_like, self.STABLE_DB) is False

    def test_high_beat_confidence_accepted(self):
        """When beat_confidence meets threshold, accept (all other criteria met)."""
        assert looks_like_amplified_bass(
            self.BASS_FEATS, self.STABLE_DB, beat_confidence=0.54) is True

    def test_beat_confidence_none_skips_check(self):
        """When beat_confidence is None (default), the check is bypassed."""
        assert looks_like_amplified_bass(
            self.BASS_FEATS, self.STABLE_DB, beat_confidence=None) is True


# ---------------------------------------------------------------------------
# Music score guard on rain + mower
# ---------------------------------------------------------------------------

class TestRainMusicScoreGuard:
    """The rain filter should reject blocks with high music-like scores."""

    # Bass-heavy profile that would match rain's flatness + centroid + env_std
    BASS_RAIN_OVERLAP = {
        "flatness": 0.30, "centroid_hz": 3200, "lowband_ratio": 0.45,
        "midband_ratio": 0.35, "highband_ratio": 0.20,
    }
    STABLE_DB = [78.0] * 12

    def test_rain_rejected_by_high_music_score(self):
        """Bass music with rain-like flatness + stability should NOT match rain."""
        # mscore for this profile: lowband 0.45 → 0.6*clamp(0.72)=0.432,
        # flatness 0.30 → triangle = 0.857, 0.4*0.857=0.343 → mscore=0.775
        assert looks_like_rain(
            self.BASS_RAIN_OVERLAP, self.STABLE_DB, 0.27, 1.5) is False

    def test_rain_passes_with_real_rain_mscore(self):
        """Actual rain profile (low lowband) has mscore below guard threshold."""
        # Real rain: lowband 0.12 → mscore ~0.40 — well below 0.70
        rain_feats = {"flatness": 0.35, "centroid_hz": 3500,
                      "lowband_ratio": 0.12, "midband_ratio": 0.40,
                      "highband_ratio": 0.48}
        assert looks_like_rain(rain_feats, self.STABLE_DB, 0.27, 1.5) is True

    def test_rain_guard_threshold_configurable(self):
        """Custom max_music_score overrides the default 0.70."""
        # With guard at 0.90, the 0.775 mscore passes
        assert looks_like_rain(
            self.BASS_RAIN_OVERLAP, self.STABLE_DB, 0.27, 1.5,
            max_music_score=0.90) is True


class TestMowerMusicScoreGuard:
    """The mower filter should reject blocks with high music-like scores."""

    # Bass-heavy profile that would match mower's flatness + centroid range
    BASS_MOWER_OVERLAP = {
        "flatness": 0.35, "centroid_hz": 2500, "lowband_ratio": 0.45,
        "midband_ratio": 0.35, "highband_ratio": 0.20,
    }
    STABLE_DB = [78.0] * 12

    def test_mower_rejected_by_high_music_score(self):
        """Bass music with mower-like profile should NOT match mower."""
        # mscore = 0.775 (same arithmetic as rain guard test)
        assert looks_like_mower(
            self.BASS_MOWER_OVERLAP, self.STABLE_DB, 0.28, 300, 4000,
            db_now=78.0) is False

    def test_mower_passes_with_real_mower_mscore(self):
        """Real mower profile (low lowband) has mscore well below guard threshold."""
        # Real gas mower: lowband 0.04 → mscore ~0.27
        mower_feats = {"flatness": 0.40, "centroid_hz": 1800,
                       "lowband_ratio": 0.04, "midband_ratio": 0.36,
                       "highband_ratio": 0.60}
        assert looks_like_mower(
            mower_feats, self.STABLE_DB, 0.28, 300, 4000, db_now=78.0) is True

    def test_mower_guard_threshold_configurable(self):
        """Custom max_music_score overrides the default 0.70."""
        assert looks_like_mower(
            self.BASS_MOWER_OVERLAP, self.STABLE_DB, 0.28, 300, 4000,
            db_now=78.0, max_music_score=0.90) is True


# ---------------------------------------------------------------------------
# looks_like_rain
# ---------------------------------------------------------------------------

class TestLooksLikeRain:
    def test_flat_stable_is_rain(self):
        # Real rain profile: flatness 0.27–0.38, lowband 0.08–0.14, centroid 3130–4023
        feats = {"flatness": 0.35, "centroid_hz": 3500, "lowband_ratio": 0.12, "midband_ratio": 0.4, "highband_ratio": 0.48}
        # Stable readings with low variance (real rain env_std < 0.50)
        stable_db = [55.0, 55.1, 54.9, 55.2, 55.0, 54.8, 55.1, 55.0, 54.9, 55.0, 55.1, 54.8]
        assert looks_like_rain(feats, stable_db, 0.27, 1.5) is True

    def test_low_flatness_not_rain(self):
        # Flatness 0.15 is well below the 0.27 threshold (tonal, not broadband)
        feats = {"flatness": 0.15, "centroid_hz": 500, "lowband_ratio": 0.5, "midband_ratio": 0.3, "highband_ratio": 0.2}
        stable_db = [55.0] * 12
        assert looks_like_rain(feats, stable_db, 0.27, 1.5) is False

    def test_high_variance_not_rain(self):
        feats = {"flatness": 0.35, "centroid_hz": 3500, "lowband_ratio": 0.12, "midband_ratio": 0.4, "highband_ratio": 0.48}
        # Wild fluctuations
        wild_db = [50.0, 70.0, 50.0, 70.0, 50.0, 70.0, 50.0, 70.0, 50.0, 70.0, 50.0, 70.0]
        assert looks_like_rain(feats, wild_db, 0.27, 1.5) is False

    def test_short_history_not_rain(self):
        feats = {"flatness": 0.35, "centroid_hz": 3500, "lowband_ratio": 0.12, "midband_ratio": 0.4, "highband_ratio": 0.48}
        assert looks_like_rain(feats, [55.0, 55.1], 0.27, 1.5) is False


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
        feats = {"flatness": 0.40, "centroid_hz": 2200, "lowband_ratio": 0.10,
                 "midband_ratio": 0.40, "highband_ratio": 0.50}
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

    def test_high_highband_rejected(self):
        """Birdsong choruses can mimic mower centroid + flatness but have
        highband > 0.75. The highband ceiling rejects them."""
        feats = {"flatness": 0.30, "centroid_hz": 3800, "lowband_ratio": 0.04,
                 "midband_ratio": 0.13, "highband_ratio": 0.83}
        stable_db = [80.0] * 12
        assert looks_like_mower(feats, stable_db, 0.28, 300, 4000,
                                db_now=80.0, highband_max=0.75) is False

    def test_highband_at_ceiling_passes(self):
        """Highband exactly at the ceiling should still pass."""
        feats = {"flatness": 0.65, "centroid_hz": 800, "lowband_ratio": 0.3,
                 "midband_ratio": 0.4, "highband_ratio": 0.75}
        stable_db = [75.0] * 12
        assert looks_like_mower(feats, stable_db, 0.60, 300, 3000,
                                db_now=75.0, highband_max=0.75) is True


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
        # Bursty dB pattern — env_std ~10 (fails Path A variance_max=1.0)
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


# ---------------------------------------------------------------------------
# Path D — multi-species chorus (temporal highband variance)
# ---------------------------------------------------------------------------

class TestBirdsongPathD:
    """Path D: multi-species chorus detection via temporal highband variance.

    Uses feature_history (list of spectrum_features dicts) to compute the
    standard deviation of highband_ratio over a sliding window. Multi-species
    choruses produce significant block-to-block variation in highband as
    different species alternate calls. Mechanical sources (mowers, fans)
    produce stable highband values.

    Two safety margins prevent false positives:
      - Window-wide lowband ceiling: every block in the window must have
        lowband ≤ chorus_lowband_max (0.12). This rejects thunder (0.55+
        on crack blocks), mower (windows always contain some 0.12+ blocks),
        rain (0.16+), and diesel (0.19+).
      - Minimum highband std: rejects monotone steady-state sources.
    """

    # Simulated chorus: alternating high/moderate highband, consistently low lowband
    CHORUS_FEATS = [
        {"highband_ratio": hb, "lowband_ratio": 0.07, "flatness": 0.30,
         "midband_ratio": 0.20, "centroid_hz": 2000}
        for hb in [0.85, 0.55, 0.80, 0.60, 0.90, 0.50, 0.75, 0.65,
                    0.88, 0.52, 0.78, 0.62]
    ]  # std ≈ 0.137, well above 0.10 threshold

    def test_chorus_pattern_accepted(self):
        """Classic multi-species chorus: oscillating highband, all-low lowband."""
        # Features fail Path A (highband 0.65 < 0.70), fail B/C (not extreme enough)
        feats = {"highband_ratio": 0.65, "lowband_ratio": 0.08, "flatness": 0.25,
                 "midband_ratio": 0.20, "centroid_hz": 2000}
        moderate_db = [70.0, 78.0, 72.0, 76.0, 71.0, 77.0,
                       73.0, 75.0, 70.0, 78.0, 72.0, 76.0]
        result = looks_like_birdsong(
            feats, moderate_db, variance_max=1.0,
            feature_history=self.CHORUS_FEATS,
            chorus_highband_std_min=0.10, chorus_lowband_max=0.12,
            chorus_min_history=12,
        )
        assert result is True

    def test_no_feature_history_falls_through(self):
        """Without feature_history, Path D cannot fire — only A/B/C apply."""
        # Features designed to fail paths A (env_std too high), B (not extreme enough),
        # C (not pure enough) — but would pass Path D if feature_history were present.
        feats = {"highband_ratio": 0.65, "lowband_ratio": 0.07, "flatness": 0.25,
                 "midband_ratio": 0.20, "centroid_hz": 2000}
        moderate_db = [70.0, 78.0, 72.0, 76.0, 71.0, 77.0,
                       73.0, 75.0, 70.0, 78.0, 72.0, 76.0]
        assert looks_like_birdsong(feats, moderate_db, variance_max=1.0) is False

    def test_short_feature_history_rejected(self):
        """Feature history shorter than chorus_min_history should not trigger."""
        # Features fail Path A (highband 0.65 < 0.70), fail B/C (not extreme enough)
        feats = {"highband_ratio": 0.65, "lowband_ratio": 0.08, "flatness": 0.25,
                 "midband_ratio": 0.20, "centroid_hz": 2000}
        moderate_db = [70.0, 78.0, 72.0, 76.0, 71.0, 77.0,
                       73.0, 75.0, 70.0, 78.0, 72.0, 76.0]
        short_history = self.CHORUS_FEATS[:11]  # 11 < 12 required
        result = looks_like_birdsong(
            feats, moderate_db, variance_max=1.0,
            feature_history=short_history,
            chorus_highband_std_min=0.10, chorus_lowband_max=0.12,
            chorus_min_history=12,
        )
        assert result is False

    def test_high_lowband_block_in_window_rejected(self):
        """One block with high lowband in the window should kill Path D.

        This is the primary mower/thunder discriminator — mower windows always
        contain some blocks at 0.12+, and thunder blocks have lowband 0.55+.
        """
        # Poison one block with high lowband (simulating a mower block in window)
        poisoned = [dict(f) for f in self.CHORUS_FEATS]
        poisoned[6]["lowband_ratio"] = 0.13  # Above 0.12 ceiling
        # Features fail Path A (highband 0.65 < 0.70), fail B/C (not extreme enough)
        feats = {"highband_ratio": 0.65, "lowband_ratio": 0.07, "flatness": 0.25,
                 "midband_ratio": 0.20, "centroid_hz": 2000}
        moderate_db = [70.0, 78.0, 72.0, 76.0, 71.0, 77.0,
                       73.0, 75.0, 70.0, 78.0, 72.0, 76.0]
        result = looks_like_birdsong(
            feats, moderate_db, variance_max=1.0,
            feature_history=poisoned,
            chorus_highband_std_min=0.10, chorus_lowband_max=0.12,
            chorus_min_history=12,
        )
        assert result is False

    def test_low_highband_variance_rejected(self):
        """Steady-state highband (like a mower or fan) should not trigger Path D."""
        # All blocks at similar highband — std ≈ 0.01, well below 0.10
        steady_feats = [
            {"highband_ratio": 0.60 + (i * 0.003), "lowband_ratio": 0.05,
             "flatness": 0.35, "midband_ratio": 0.25, "centroid_hz": 2500}
            for i in range(12)
        ]
        feats = {"highband_ratio": 0.63, "lowband_ratio": 0.05, "flatness": 0.35,
                 "midband_ratio": 0.25, "centroid_hz": 2500}
        stable_db = [73.0] * 12
        result = looks_like_birdsong(
            feats, stable_db, variance_max=1.0,
            feature_history=steady_feats,
            chorus_highband_std_min=0.10, chorus_lowband_max=0.12,
            chorus_min_history=12,
        )
        assert result is False

    def test_path_a_still_fires_when_path_d_available(self):
        """Path A should still fire for sustained birdsong even when
        feature_history is present — Path D is additive, not exclusive."""
        feats = {"highband_ratio": 0.70, "lowband_ratio": 0.05, "flatness": 0.55,
                 "midband_ratio": 0.25, "centroid_hz": 4000}
        stable_db = [62.0] * 12
        result = looks_like_birdsong(
            feats, stable_db, variance_max=1.0,
            feature_history=self.CHORUS_FEATS,
            chorus_highband_std_min=0.10, chorus_lowband_max=0.12,
            chorus_min_history=12,
        )
        assert result is True


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
        """Rain flatness ~0.35 (real-world calibration 0.27–0.38).
        |0.35 - 0.35| / 0.35 = 0.0 → tonal_component = 1.0, but rain
        has negligible bass so the score stays low via low_component.
        Use 0.72 (broadband noise) to verify tonal rejection path.
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
        conf_2 = _inter_block_beat_confidence(pattern_2)
        conf_4 = _inter_block_beat_confidence(pattern_4)
        # Both should be high, but lag-2 correlation is direct
        assert conf_2 > 0.7
        assert conf_4 > 0.5

    def test_random_noise_low_confidence(self):
        """Random amplitude fluctuations should not register as rhythmic."""
        rng = np.random.default_rng(42)
        noise = list(rng.uniform(50, 80, 24))
        assert _inter_block_beat_confidence(noise) < 0.7

    def test_monotone_returns_zero(self):
        """Constant dB (all-zero deltas) returns exactly 0.0 via the allclose check."""
        assert _inter_block_beat_confidence([65.0] * 24) == 0.0

    def test_minimum_history_boundary(self):
        """Exactly 8 readings should produce a valid (non-zero) result for a pattern."""
        pattern = [60, 75, 60, 75, 60, 75, 60, 75]
        result = _inter_block_beat_confidence(pattern)
        assert result > 0.0

    def test_seven_readings_returns_zero(self):
        """7 readings (below minimum of 8) should return 0.0."""
        assert _inter_block_beat_confidence([60, 75] * 3 + [60]) == 0.0


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

    # -- Path B: sustained rumble --

    def _rumble_feats(self, centroid=1000, flatness=0.10, midband=0.55,
                      lowband=0.25):
        """Helper: typical sustained thunder rumble features."""
        return {"centroid_hz": centroid, "flatness": flatness,
                "midband_ratio": midband, "lowband_ratio": lowband,
                "highband_ratio": 1.0 - lowband - midband}

    def test_path_b_accepts_typical_rumble(self):
        """Low centroid, very low flatness, dominant midband, loud → thunder."""
        feats = self._rumble_feats()
        history = [90.0] * 10
        assert looks_like_thunder(feats, 100.0, 98.0, 18.0,
                                  recent_db=history) is True

    def test_path_b_rejects_without_history(self):
        """Path B requires min_history blocks; insufficient history → no match."""
        feats = self._rumble_feats()
        history = [90.0] * 3  # Less than default 6
        assert looks_like_thunder(feats, 100.0, 98.0, 18.0,
                                  recent_db=history) is False

    def test_path_b_rejects_quiet_rumble(self):
        """Below the dB floor (95) — ambient droning, not thunder."""
        feats = self._rumble_feats()
        history = [80.0] * 10
        assert looks_like_thunder(feats, 90.0, 88.0, 18.0,
                                  recent_db=history) is False

    def test_path_b_rejects_high_flatness(self):
        """Flatness above 0.15 → too broadband for concentrated thunder rumble.
        This separates thunder from mower (mower flatness ≥ 0.28)."""
        feats = self._rumble_feats(flatness=0.20)
        history = [90.0] * 10
        assert looks_like_thunder(feats, 100.0, 98.0, 18.0,
                                  recent_db=history) is False

    def test_path_b_rejects_high_centroid(self):
        """Centroid above 1300 Hz — too high for thunder rumble."""
        feats = self._rumble_feats(centroid=1400)
        history = [90.0] * 10
        assert looks_like_thunder(feats, 100.0, 98.0, 18.0,
                                  recent_db=history) is False

    def test_path_b_rejects_low_midband(self):
        """Midband below 0.40 — thunder rumble body should dominate midband."""
        feats = self._rumble_feats(midband=0.35)
        history = [90.0] * 10
        assert looks_like_thunder(feats, 100.0, 98.0, 18.0,
                                  recent_db=history) is False

    def test_path_b_configurable_thresholds(self):
        """All Path B thresholds should be overridable via kwargs."""
        feats = self._rumble_feats(centroid=1500, flatness=0.20, midband=0.35)
        history = [90.0] * 10
        # Defaults would reject (centroid > 1300, flatness > 0.15, midband < 0.40)
        assert looks_like_thunder(feats, 92.0, 90.0, 18.0,
                                  recent_db=history) is False
        # With relaxed thresholds
        assert looks_like_thunder(feats, 92.0, 90.0, 18.0,
                                  recent_db=history,
                                  rumble_centroid_max=1600,
                                  rumble_flatness_max=0.25,
                                  rumble_midband_min=0.30,
                                  rumble_min_db=90.0) is True


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


class TestAmplifiedBassSensitivity:
    """Boundary and sensitivity tests for the amplified bass filter."""

    BASS_FEATS = {
        "flatness": 0.35, "centroid_hz": 2800, "lowband_ratio": 0.47,
        "midband_ratio": 0.33, "highband_ratio": 0.20,
    }
    STABLE_DB = [78.0] * 12

    def test_min_history_boundary_accepts(self):
        """Exactly 6 readings (default min_history) should be evaluated."""
        assert looks_like_amplified_bass(
            self.BASS_FEATS, [78.0] * 6) is True

    def test_min_history_boundary_rejects(self):
        """5 readings (below default 6) should be rejected."""
        assert looks_like_amplified_bass(
            self.BASS_FEATS, [78.0] * 5) is False

    def test_lowband_at_boundary(self):
        """Lowband at 0.16 should pass (exactly at floor; min_music_score lowered
        to isolate the lowband variable — lowband 0.16 produces mscore ~0.554)."""
        feats = {**self.BASS_FEATS, "lowband_ratio": 0.16}
        assert looks_like_amplified_bass(
            feats, self.STABLE_DB, min_music_score=0.45) is True

    def test_lowband_just_below_boundary(self):
        """Lowband at 0.15 should fail (below 0.16 floor)."""
        feats = {**self.BASS_FEATS, "lowband_ratio": 0.15}
        assert looks_like_amplified_bass(
            feats, self.STABLE_DB, min_music_score=0.50) is False

    def test_centroid_at_boundary(self):
        """Centroid exactly at 4000 should pass."""
        feats = {**self.BASS_FEATS, "centroid_hz": 4000}
        assert looks_like_amplified_bass(feats, self.STABLE_DB) is True

    def test_centroid_just_above_boundary(self):
        """Centroid at 4001 should fail."""
        feats = {**self.BASS_FEATS, "centroid_hz": 4001}
        assert looks_like_amplified_bass(feats, self.STABLE_DB) is False

    def test_env_std_boundary(self):
        """env_std just above 3.0 should reject."""
        # std dev of alternating 72.0, 80.0 repeating ≈ 4.0, well above 3.0
        wild_db = [72.0, 80.0, 72.0, 80.0, 72.0, 80.0,
                   72.0, 80.0, 72.0, 80.0, 72.0, 80.0]
        assert looks_like_amplified_bass(
            self.BASS_FEATS, wild_db) is False

    def test_diesel_does_not_match(self):
        """Diesel car profile (mscore ~0.46) should not trigger amplified bass."""
        diesel_feats = {"flatness": 0.14, "centroid_hz": 1800,
                        "lowband_ratio": 0.20, "midband_ratio": 0.30,
                        "highband_ratio": 0.50}
        assert looks_like_amplified_bass(
            diesel_feats, [71.0] * 12) is False

    def test_rain_does_not_match(self):
        """Rain profile (mscore ~0.40) should not trigger amplified bass."""
        rain_feats = {"flatness": 0.35, "centroid_hz": 3500,
                      "lowband_ratio": 0.12, "midband_ratio": 0.40,
                      "highband_ratio": 0.48}
        assert looks_like_amplified_bass(
            rain_feats, [55.0] * 12) is False


class TestRainSensitivity:
    """Verify rain filter min_history, window, lowband_min, and centroid_max."""

    # Realistic rain feature profile (calibrated from real outdoor rain)
    RAIN_FEATS = {"flatness": 0.35, "centroid_hz": 3500, "lowband_ratio": 0.12,
                  "midband_ratio": 0.4, "highband_ratio": 0.48}

    def test_min_history_boundary_accepts(self):
        """Exactly 6 readings (default min_history) should be evaluated."""
        assert looks_like_rain(self.RAIN_FEATS, [55.0] * 6, 0.27, 1.5) is True

    def test_min_history_boundary_rejects(self):
        """5 readings (below default 6) should be rejected."""
        assert looks_like_rain(self.RAIN_FEATS, [55.0] * 5, 0.27, 1.5) is False

    def test_custom_min_history(self):
        """Custom min_history should override default."""
        # 8 readings, but min_history=10 → rejected
        assert looks_like_rain(self.RAIN_FEATS, [55.0] * 8, 0.27, 1.5, min_history=10) is False

    def test_custom_window(self):
        """Custom window should change which readings are evaluated.
        If most recent 6 are stable but the full 12 include wild early data,
        using window=6 should accept while window=12 might reject.
        """
        # 6 wild + 6 stable = 12 total
        mixed_db = [40.0, 70.0, 40.0, 70.0, 40.0, 70.0,
                    55.0, 55.0, 55.0, 55.0, 55.0, 55.0]
        # Default window=12 → std includes the wild data → rejects
        assert looks_like_rain(self.RAIN_FEATS, mixed_db, 0.27, 1.5) is False
        # Narrow window=6 → std of last 6 (all 55.0) → accepts
        assert looks_like_rain(self.RAIN_FEATS, mixed_db, 0.27, 1.5, window=6) is True

    def test_lowband_below_min_rejected(self):
        """Mower-like lowband (0.04) below rain_lowband_min (0.07) → not rain.
        This is the key separator: rain excites bass more than mechanical sources."""
        mower_like = {**self.RAIN_FEATS, "lowband_ratio": 0.04}
        assert looks_like_rain(mower_like, [55.0] * 12, 0.27, 1.5) is False

    def test_lowband_at_min_accepted(self):
        """Lowband exactly at the minimum (0.07) should still pass."""
        borderline = {**self.RAIN_FEATS, "lowband_ratio": 0.07}
        assert looks_like_rain(borderline, [55.0] * 12, 0.27, 1.5) is True

    def test_custom_lowband_min_overrides_default(self):
        """Custom lowband_min should override the 0.07 default."""
        low_bass = {**self.RAIN_FEATS, "lowband_ratio": 0.05}
        # Default 0.07 rejects 0.05
        assert looks_like_rain(low_bass, [55.0] * 12, 0.27, 1.5) is False
        # Custom 0.04 accepts 0.05
        assert looks_like_rain(low_bass, [55.0] * 12, 0.27, 1.5, lowband_min=0.04) is True

    def test_centroid_above_max_rejected(self):
        """High centroid (birdsong territory, 5800+ Hz) → not rain.
        Rain centroid maxes at ~4023 Hz in real recordings."""
        birdsong_like = {**self.RAIN_FEATS, "centroid_hz": 5800}
        assert looks_like_rain(birdsong_like, [55.0] * 12, 0.27, 1.5) is False

    def test_centroid_at_max_accepted(self):
        """Centroid exactly at the ceiling (5000) should still pass."""
        borderline = {**self.RAIN_FEATS, "centroid_hz": 5000}
        assert looks_like_rain(borderline, [55.0] * 12, 0.27, 1.5) is True

    def test_custom_centroid_max_overrides_default(self):
        """Custom centroid_max should override the 5000 default."""
        high_centroid = {**self.RAIN_FEATS, "centroid_hz": 5500}
        # Default 5000 rejects 5500
        assert looks_like_rain(high_centroid, [55.0] * 12, 0.27, 1.5) is False
        # Custom 6000 accepts 5500
        assert looks_like_rain(high_centroid, [55.0] * 12, 0.27, 1.5, centroid_max=6000) is True


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


class TestBirdsongChorusSensitivity:
    """Verify Path D chorus detection boundary conditions and configurable thresholds."""

    # Shared chorus feature history with clean separation
    CHORUS_FEATS = [
        {"highband_ratio": hb, "lowband_ratio": 0.07, "flatness": 0.30,
         "midband_ratio": 0.20, "centroid_hz": 2000}
        for hb in [0.85, 0.55, 0.80, 0.60, 0.90, 0.50, 0.75, 0.65,
                    0.88, 0.52, 0.78, 0.62]
    ]  # hb_std ≈ 0.137

    BASE_FEATS = {"highband_ratio": 0.65, "lowband_ratio": 0.07, "flatness": 0.25,
                  "midband_ratio": 0.20, "centroid_hz": 2000}
    STABLE_DB = [73.0] * 12
    KWARGS = {"variance_max": 1.0, "chorus_highband_std_min": 0.10,
              "chorus_lowband_max": 0.12, "chorus_min_history": 12}

    def test_highband_std_boundary(self):
        """hb_std at exactly 0.10 should accept; just below should reject."""
        # Build history with std exactly at 0.10 (12 values alternating between
        # 0.60 and 0.80 with small noise gives std ~0.10)
        borderline = [
            {"highband_ratio": hb, "lowband_ratio": 0.06, "flatness": 0.30,
             "midband_ratio": 0.20, "centroid_hz": 2000}
            for hb in [0.60, 0.80, 0.60, 0.80, 0.60, 0.80,
                        0.60, 0.80, 0.60, 0.80, 0.60, 0.80]
        ]  # std = 0.10
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=borderline, **self.KWARGS,
        ) is True

        # Slightly lower variance (all at 0.70 ± tiny noise)
        steady = [
            {"highband_ratio": 0.70 + (i * 0.001), "lowband_ratio": 0.06,
             "flatness": 0.30, "midband_ratio": 0.20, "centroid_hz": 2000}
            for i in range(12)
        ]  # std ≈ 0.003
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=steady, **self.KWARGS,
        ) is False

    def test_chorus_lowband_max_boundary(self):
        """Window with max lowband at exactly 0.12 should accept; 0.13 should reject."""
        at_boundary = [dict(f) for f in self.CHORUS_FEATS]
        at_boundary[3]["lowband_ratio"] = 0.12  # Exactly at ceiling
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=at_boundary, **self.KWARGS,
        ) is True

        over_boundary = [dict(f) for f in self.CHORUS_FEATS]
        over_boundary[3]["lowband_ratio"] = 0.13  # Just above ceiling
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=over_boundary, **self.KWARGS,
        ) is False

    def test_chorus_min_history_boundary(self):
        """Exactly 12 blocks should accept; 11 should reject."""
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=self.CHORUS_FEATS[:12], **self.KWARGS,
        ) is True
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=self.CHORUS_FEATS[:11], **self.KWARGS,
        ) is False

    def test_custom_chorus_min_history(self):
        """Raising chorus_min_history to 15 should reject 12 entries."""
        kwargs = {**self.KWARGS, "chorus_min_history": 15}
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=self.CHORUS_FEATS[:12], **kwargs,
        ) is False

    def test_custom_highband_std_min(self):
        """Custom chorus_highband_std_min should override the 0.10 default."""
        # Build a history with moderate variance (std ≈ 0.05)
        moderate = [
            {"highband_ratio": 0.65 + (0.05 if i % 2 else -0.05),
             "lowband_ratio": 0.06, "flatness": 0.30,
             "midband_ratio": 0.20, "centroid_hz": 2000}
            for i in range(12)
        ]  # std ≈ 0.05
        # Default 0.10 rejects
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=moderate, **self.KWARGS,
        ) is False
        # Custom 0.04 accepts
        kwargs = {**self.KWARGS, "chorus_highband_std_min": 0.04}
        assert looks_like_birdsong(
            self.BASE_FEATS, self.STABLE_DB,
            feature_history=moderate, **kwargs,
        ) is True


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
    """Diesel engine: low flatness (tonal harmonics), mid centroid, some bass, steady."""

    def test_classic_diesel_car_signature(self):
        """Real diesel car profile: low flatness, mid centroid, moderate lowband → diesel."""
        # Calibrated from diesel-car.wav steady-state blocks
        feats = {"centroid_hz": 1800, "flatness": 0.15, "lowband_ratio": 0.22,
                 "midband_ratio": 0.33, "highband_ratio": 0.45}
        steady_db = [71.0, 71.5, 70.8, 71.2, 71.1, 70.9, 71.3, 71.0,
                     70.8, 71.1, 71.2, 70.9]
        assert looks_like_diesel(feats, steady_db) is True

    def test_high_centroid_not_diesel(self):
        """Centroid above 3600 Hz → mower/weedwhacker territory, not diesel."""
        feats = {"centroid_hz": 4000, "flatness": 0.15, "lowband_ratio": 0.22,
                 "midband_ratio": 0.33, "highband_ratio": 0.45}
        steady_db = [71.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_low_centroid_not_diesel(self):
        """Centroid below 1200 Hz → very low-frequency rumble, not diesel."""
        feats = {"centroid_hz": 1100, "flatness": 0.15, "lowband_ratio": 0.22,
                 "midband_ratio": 0.33, "highband_ratio": 0.45}
        steady_db = [71.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_high_flatness_not_diesel(self):
        """Flatness above 0.20 → too broadband for tonal engine harmonics."""
        feats = {"centroid_hz": 1800, "flatness": 0.25, "lowband_ratio": 0.22,
                 "midband_ratio": 0.33, "highband_ratio": 0.45}
        steady_db = [71.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_low_lowband_not_diesel(self):
        """Lowband below 0.10 → birdsong territory, not diesel."""
        feats = {"centroid_hz": 1800, "flatness": 0.15, "lowband_ratio": 0.05,
                 "midband_ratio": 0.33, "highband_ratio": 0.62}
        steady_db = [71.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_low_midband_not_diesel(self):
        """Midband below 0.20 → engine energy should sit in mid frequencies."""
        feats = {"centroid_hz": 1800, "flatness": 0.15, "lowband_ratio": 0.22,
                 "midband_ratio": 0.15, "highband_ratio": 0.63}
        steady_db = [71.0] * 12
        assert looks_like_diesel(feats, steady_db) is False

    def test_variable_amplitude_not_diesel(self):
        """High amplitude variance → not a steady engine."""
        feats = {"centroid_hz": 1800, "flatness": 0.15, "lowband_ratio": 0.22,
                 "midband_ratio": 0.33, "highband_ratio": 0.45}
        wild_db = [55.0, 80.0, 55.0, 80.0, 55.0, 80.0, 55.0, 80.0,
                   55.0, 80.0, 55.0, 80.0]
        assert looks_like_diesel(feats, wild_db) is False

    def test_short_history_not_diesel(self):
        """Need at least 8 readings (default min_history) for sustained engine."""
        feats = {"centroid_hz": 1800, "flatness": 0.15, "lowband_ratio": 0.22,
                 "midband_ratio": 0.33, "highband_ratio": 0.45}
        assert looks_like_diesel(feats, [71.0] * 5) is False


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
    """Boundary tests for diesel filter parameters — calibrated from real diesel car."""

    # Realistic diesel feature profile (calibrated from diesel-car.wav steady-state)
    DIESEL_FEATS = {"centroid_hz": 1800, "flatness": 0.15, "lowband_ratio": 0.22,
                    "midband_ratio": 0.33, "highband_ratio": 0.45}

    def test_centroid_boundary(self):
        """Centroid at 3600 Hz should accept; 3601 should reject.
        Centroid at 1200 Hz should accept; 1199 should reject."""
        steady_db = [71.0] * 12
        assert looks_like_diesel({**self.DIESEL_FEATS, "centroid_hz": 3600}, steady_db) is True
        assert looks_like_diesel({**self.DIESEL_FEATS, "centroid_hz": 3601}, steady_db) is False
        assert looks_like_diesel({**self.DIESEL_FEATS, "centroid_hz": 1200}, steady_db) is True
        assert looks_like_diesel({**self.DIESEL_FEATS, "centroid_hz": 1199}, steady_db) is False

    def test_flatness_boundary(self):
        """Flatness at 0.20 should accept; 0.21 should reject.
        This is the key separator from mower (≥ 0.28) and rain (≥ 0.27)."""
        steady_db = [71.0] * 12
        assert looks_like_diesel({**self.DIESEL_FEATS, "flatness": 0.20}, steady_db) is True
        assert looks_like_diesel({**self.DIESEL_FEATS, "flatness": 0.21}, steady_db) is False

    def test_lowband_boundary(self):
        """Lowband at 0.10 should accept; 0.09 should reject.
        Separates diesel (some bass) from birdsong (≤ 0.09)."""
        steady_db = [71.0] * 12
        assert looks_like_diesel({**self.DIESEL_FEATS, "lowband_ratio": 0.10}, steady_db) is True
        assert looks_like_diesel({**self.DIESEL_FEATS, "lowband_ratio": 0.09}, steady_db) is False

    def test_midband_boundary(self):
        """Midband at 0.20 should accept; 0.19 should reject."""
        steady_db = [71.0] * 12
        assert looks_like_diesel({**self.DIESEL_FEATS, "midband_ratio": 0.20}, steady_db) is True
        assert looks_like_diesel({**self.DIESEL_FEATS, "midband_ratio": 0.19}, steady_db) is False

    def test_min_history_boundary(self):
        """Exactly 8 readings should evaluate; 7 should reject."""
        assert looks_like_diesel(self.DIESEL_FEATS, [71.0] * 8) is True
        assert looks_like_diesel(self.DIESEL_FEATS, [71.0] * 7) is False

    def test_custom_env_std_overrides_default(self):
        """Custom env_std_max should override the 3.0 default."""
        # env_std of these ≈ 3.5 (above 3.0 but below custom 4.0)
        semi_stable = [68.0, 75.0, 68.0, 75.0, 68.0, 75.0,
                       68.0, 75.0, 68.0, 75.0, 68.0, 75.0]
        assert looks_like_diesel(self.DIESEL_FEATS, semi_stable) is False
        assert looks_like_diesel(self.DIESEL_FEATS, semi_stable, env_std_max=4.0) is True


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

    def test_music_score_guard_rejects_vocal_music(self):
        """Blocks with high music_like_score should be rejected — prevents
        music-with-vocals from being misclassified as conversation.
        The filter chain runs before classify_sound(), so without this guard
        vocal music would be labeled 'conversation' instead of 'music'."""
        # Features typical of bass-heavy music through wall: lowband 0.40,
        # flatness near tonal center → music_like_score ≈ 0.78
        music_feats = {"centroid_hz": 1200, "flatness": 0.35, "lowband_ratio": 0.40,
                       "midband_ratio": 0.35, "highband_ratio": 0.25}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        assert looks_like_conversation(music_feats, speech_db) is False

    def test_music_score_guard_allows_speech(self):
        """Blocks with low music_like_score (light bass, moderate tonal)
        should pass through — real conversation scores ~0.43–0.53."""
        speech_feats = {"centroid_hz": 1200, "flatness": 0.45, "lowband_ratio": 0.12,
                        "midband_ratio": 0.55, "highband_ratio": 0.33}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        assert looks_like_conversation(speech_feats, speech_db) is True

    def test_music_score_guard_configurable(self):
        """Custom max_music_score overrides the 0.55 default."""
        # Score ≈ 0.535 (the classic test feats) — below 0.55 default, above 0.50
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        assert looks_like_conversation(feats, speech_db, max_music_score=0.55) is True
        assert looks_like_conversation(feats, speech_db, max_music_score=0.50) is False

    def test_music_score_guard_disabled_at_zero(self):
        """Setting max_music_score=0 should disable the guard entirely."""
        # Features that produce a high music score (≈0.67) but pass all other
        # conversation checks: moderate bass, near-peak tonal flatness.
        # With default max_music_score=0.55, this would be rejected.
        borderline_feats = {"centroid_hz": 1200, "flatness": 0.33, "lowband_ratio": 0.30,
                            "midband_ratio": 0.45, "highband_ratio": 0.25}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        # Default threshold rejects (score ≈ 0.67 > 0.55)
        assert looks_like_conversation(borderline_feats, speech_db) is False
        # Disabled guard allows it through
        assert looks_like_conversation(borderline_feats, speech_db, max_music_score=0) is True

    def test_min_db_rejects_quiet_speech(self):
        """Quiet ambient conversation below nuisance level should be rejected."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        # 55 dBA is below a 60 dBA floor
        assert looks_like_conversation(feats, speech_db, min_db=60.0, db_now=55.0) is False
        # 65 dBA is above a 60 dBA floor
        assert looks_like_conversation(feats, speech_db, min_db=60.0, db_now=65.0) is True

    def test_min_db_disabled_by_default(self):
        """Default min_db=0 means no dB floor — all loudness levels pass."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        # db_now=0 with min_db=0 → guard disabled
        assert looks_like_conversation(feats, speech_db, db_now=0.0) is True

    def test_midband_rejects_low_midband(self):
        """Blocks without sufficient midband energy are not conversation —
        speech concentrates in the formant region (250–4000 Hz)."""
        # Low midband: not speech, more like broadband environmental noise
        low_mid_feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                         "midband_ratio": 0.15, "highband_ratio": 0.65}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        assert looks_like_conversation(low_mid_feats, speech_db) is False

    def test_midband_configurable(self):
        """Custom midband_min should override the 0.25 default."""
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.22, "highband_ratio": 0.58}
        speech_db = [60.0, 70.0, 62.0, 71.0, 61.0, 69.0,
                     63.0, 70.0, 60.0, 68.0, 62.0, 70.0]
        # Below default 0.25
        assert looks_like_conversation(feats, speech_db) is False
        # Above custom 0.20
        assert looks_like_conversation(feats, speech_db, midband_min=0.20) is True


# ===========================================================================
# identify_filter — orchestration layer
# ===========================================================================

class TestIdentifyFilter:
    """Tests for the centralized filter chain entry point."""

    # Minimal detection config with defaults for all required keys
    DEFAULT_DET = {
        "impulse_peak_delta_db": "14.0",
        "thunder_peak_delta_db": "18.0",
        "rain_flatness_threshold": "0.27",
        "rain_low_variance_db": "1.5",
        "rain_lowband_min": "0.07",
        "rain_centroid_max_hz": "5000",
        "mower_flatness_threshold": "0.60",
        "mower_centroid_min_hz": "300",
        "mower_centroid_max_hz": "3000",
    }

    def test_no_filter_returns_none(self):
        """Normal sound that passes all filters should return None."""
        # Low flatness (0.15) ensures this doesn't trip the rain filter (threshold 0.27)
        feats = {"flatness": 0.15, "centroid_hz": 500, "lowband_ratio": 0.4,
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
        # Low flatness (0.15) avoids tripping the rain filter when impulse is bypassed
        feats = {"lowband_ratio": 0.20, "flatness": 0.15, "centroid_hz": 500,
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

    def test_higher_priority_breaker_breaks_holdover(self):
        """A filter listed in holdover_priority_breakers with higher priority
        breaks through an active lower-priority holdover. Thunder (priority 0)
        breaking mower (priority 5) holdover is the canonical case: thunder
        Path B's min_history guarantees the signal genuinely changed."""
        effective, pf, pr, g = apply_filter_holdover(
            "thunder", "mower", 10, 3, self.DET,
        )
        assert effective == "thunder"
        assert pf == "thunder"
        assert pr == 1   # new filter starts fresh
        assert g == 0

    def test_instant_higher_priority_stays_suppressed(self):
        """An instant detector NOT in holdover_priority_breakers should NOT
        break holdover even if it's higher priority. Impulse (priority 1)
        during mower holdover is a transient blip."""
        effective, pf, pr, g = apply_filter_holdover(
            "impulse", "mower", 10, 3, self.DET,
        )
        assert effective == "mower"
        assert pf == "mower"
        assert pr == 10  # unchanged
        assert g == 4    # incremented (transient treated as gap)

    def test_lower_priority_does_not_break_holdover(self):
        """A lower-priority filter should never break holdover, even if it's
        in the breakers list. Conversation (priority 7) during mower
        (priority 5) holdover should be suppressed."""
        cfg = {**self.DET, "holdover_priority_breakers": "thunder,conversation"}
        effective, pf, pr, g = apply_filter_holdover(
            "conversation", "mower", 10, 3, cfg,
        )
        assert effective == "mower"
        assert pf == "mower"
        assert pr == 10
        assert g == 4

    def test_non_breaker_higher_priority_stays_suppressed(self):
        """A higher-priority filter NOT in holdover_priority_breakers stays
        suppressed. Weedwhacker (priority 4) during mower (priority 5: lower)
        is suppressed because weedwhacker isn't a default breaker."""
        effective, pf, pr, g = apply_filter_holdover(
            "weedwhacker", "mower", 10, 3, self.DET,
        )
        assert effective == "mower"
        assert pf == "mower"
        assert pr == 10
        assert g == 4

    def test_breakers_configurable(self):
        """Custom holdover_priority_breakers should be respected. Adding
        birdsong (priority 2) to the breakers list lets it break mower
        (priority 5) holdover."""
        cfg = {**self.DET, "holdover_priority_breakers": "thunder,birdsong"}
        effective, pf, pr, g = apply_filter_holdover(
            "birdsong", "mower", 10, 3, cfg,
        )
        assert effective == "birdsong"
        assert pf == "birdsong"
        assert pr == 1
        assert g == 0


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

    def test_thunder_has_latency(self):
        """Thunder Path B is a sustained detector with min_history=6."""
        assert get_filter_detection_latency("thunder", self.DET) == 6

    def test_thunder_config_override(self):
        """Config key overrides the hardcoded thunder latency."""
        custom = {"thunder_rumble_min_history": 10}
        assert get_filter_detection_latency("thunder", custom) == 10

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
