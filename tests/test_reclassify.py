"""Tests for the reclassify module — DSP re-analysis and classification regeneration."""
import json
import os
import tempfile

import numpy as np
import pytest
import soundfile as sf

from noise_warden.reclassify import analyze_clip, _compute_dominant
from noise_warden.reclassify import denoise_snippet, normalize_snippet
from noise_warden.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tone(freq, duration_sec, sr=22050, amplitude=0.5):
    """Generate a pure sine wave at the given frequency."""
    t = np.linspace(0, duration_sec, int(sr * duration_sec), endpoint=False)
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_noise(duration_sec, sr=22050, amplitude=0.3):
    """Generate white noise."""
    n_samples = int(sr * duration_sec)
    rng = np.random.default_rng(42)
    return (amplitude * rng.standard_normal(n_samples)).astype(np.float32)


def _write_wav(path, data, sr=22050):
    """Write a mono WAV file."""
    sf.write(path, data, sr, subtype="PCM_16")


def _minimal_detection_cfg():
    """Minimal detection config with defaults that won't interfere with tests."""
    return {
        "calibration_offset_db": 100.0,
        "min_music_like_score": 0.62,
        "min_beat_confidence": 0.38,
        "impulse_peak_delta_db": 14.0,
        "noise_floor_db": 30.0,
        "thunder_peak_delta_db": 18.0,
        "rain_flatness_threshold": 0.72,
        "rain_low_variance_db": 2.5,
        "mower_flatness_threshold": 0.25,
        "mower_centroid_min_hz": 300,
        "mower_centroid_max_hz": 4000,
        "night_start_hour": 22,
        "night_end_hour": 7,
    }


def _minimal_audio_cfg():
    return {
        "sample_rate": 22050,
        "block_seconds": 1.0,
    }


# ---------------------------------------------------------------------------
# _compute_dominant
# ---------------------------------------------------------------------------

class TestComputeDominant:
    """Unit tests for the journal → dominant classification logic."""

    def test_single_source(self):
        """Single classification throughout → returns that classification."""
        journal = [(0, "mower")]
        assert _compute_dominant(journal, 60) == "mower"

    def test_multiple_sources_picks_longest(self):
        """One real source + unknown → 'class+' suffix."""
        # unknown for 5 seconds, mower for 55 seconds
        journal = [(0, "unknown"), (5, "mower")]
        result = _compute_dominant(journal, 60)
        assert result == "mower+"

    def test_multiple_with_transitions(self):
        """Multiple unknown/real transitions with one real source → 'class+'."""
        journal = [(0, "unknown"), (3, "mower"), (10, "unknown"), (12, "mower")]
        # unknown: 3s + 2s = 5s, mower: 7s + (60-12)=48s = 55s
        result = _compute_dominant(journal, 60)
        assert result == "mower+"

    def test_empty_journal(self):
        assert _compute_dominant([], 60) == "unknown"

    def test_unknown_excluded_from_dominant(self):
        """Unknown blocks should not win even when they have the most duration.
        A 100-second recording with 85 seconds of white noise and 15 seconds
        of thunder should report 'thunder (multiple)', not 'unknown (multiple)'."""
        journal = [(0, "unknown"), (85, "thunder"), (93, "impulse"), (97, "unknown")]
        result = _compute_dominant(journal, 100)
        assert result == "thunder (multiple)"

    def test_none_excluded_from_dominant(self):
        """'none' classifications (if they appear) should also be excluded."""
        journal = [(0, "none"), (50, "mower")]
        result = _compute_dominant(journal, 60)
        assert result == "mower+"

    def test_all_unknown_falls_back(self):
        """When every journal entry is unknown, fall back to unknown anyway."""
        journal = [(0, "unknown"), (30, "unknown")]
        result = _compute_dominant(journal, 60)
        # Single distinct class — returns it directly (not multiple)
        assert result == "unknown"

    def test_tie_goes_to_first_max(self):
        """When two classifications tie on duration, max() picks one deterministically."""
        journal = [(0, "mower"), (30, "birdsong")]
        result = _compute_dominant(journal, 60)
        # Both hold 30s — max() returns whichever comes first in dict insertion order
        assert "(multiple)" in result


# ---------------------------------------------------------------------------
# analyze_clip
# ---------------------------------------------------------------------------

class TestAnalyzeClip:
    """Integration tests for the full DSP re-analysis pipeline."""

    def test_returns_expected_structure(self, tmp_path):
        """analyze_clip should return all required keys."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(5))

        result = analyze_clip(wav_path, _minimal_detection_cfg(), _minimal_audio_cfg())

        assert "blocks" in result
        assert "journal" in result
        assert "dominant" in result
        assert "db_history" in result
        assert "peak_db" in result
        assert "avg_db" in result
        assert "filter_counts" in result
        assert "n_blocks" in result
        assert result["n_blocks"] == 5

    def test_block_count_matches_duration(self, tmp_path):
        """Number of blocks should match audio duration at 1 block/sec."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(10))

        result = analyze_clip(wav_path, _minimal_detection_cfg(), _minimal_audio_cfg())

        assert result["n_blocks"] == 10
        assert len(result["blocks"]) == 10
        assert len(result["db_history"]) == 10

    def test_journal_records_transitions_only(self, tmp_path):
        """Journal should only record classification changes, not every block."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(10))

        result = analyze_clip(wav_path, _minimal_detection_cfg(), _minimal_audio_cfg())

        # Journal should have fewer entries than blocks (only transitions, not per-block)
        assert len(result["journal"]) < result["n_blocks"]
        # First entry should start at block 0
        assert result["journal"][0][0] == 0
        # Each entry should be a (block_index, classification_string) tuple
        for sec, cls in result["journal"]:
            assert isinstance(sec, int)
            assert isinstance(cls, str)

    def test_block_fields_complete(self, tmp_path):
        """Each block dict should contain all expected fields."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(3))

        result = analyze_clip(wav_path, _minimal_detection_cfg(), _minimal_audio_cfg())
        block = result["blocks"][0]

        expected_keys = {
            "block", "dba", "centroid_hz", "envelope_cv", "flatness",
            "harmonic_ratio", "lowband", "midband", "highband", "mscore",
            "env_std", "filter", "classification",
        }
        assert set(block.keys()) == expected_keys

    def test_peak_db_is_maximum(self, tmp_path):
        """peak_db should be the max of all block dBA values."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(5))

        result = analyze_clip(wav_path, _minimal_detection_cfg(), _minimal_audio_cfg())

        assert result["peak_db"] == round(max(result["db_history"]), 1)


# ---------------------------------------------------------------------------
# DB integration (reclassify_incident)
# ---------------------------------------------------------------------------

class TestReclassifyIncident:
    """Test the DB-backed reclassify workflow."""

    def _make_db_with_incident(self, tmp_path, wav_path):
        """Create a fresh DB with one incident pointing to the given WAV."""
        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path)

        iid = storage.create_incident({
            "start_ts": "2026-04-08T12:00:00-06:00",
            "start_db": 70.0,
            "peak_db": 85.0,
            "avg_db": 78.0,
            "threshold_db": 65.0,
            "music_like_score": 0.3,
            "beat_confidence": 0.2,
            "classification": "unknown",
            "mode": "continuous",
            "responded": 0,
            "merge_count": 0,
            "snippet_path": wav_path,
            "notes": "",
        })
        # Finalize it so it has duration and end_ts
        storage.finalize_incident(
            iid, "2026-04-08T12:01:00-06:00", 60, 85.0, 78.0, wav_path,
            class_journal=json.dumps([(0, "unknown")]),
            classification="unknown",
        )
        return storage, iid

    def test_reclassify_reads_snippet_and_returns_result(self, tmp_path):
        """reclassify_incident should analyze the snippet and return a result."""
        from noise_warden.reclassify import reclassify_incident

        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(10))
        storage, iid = self._make_db_with_incident(tmp_path, wav_path)

        result = reclassify_incident(
            storage, iid, _minimal_detection_cfg(), _minimal_audio_cfg()
        )

        assert result is not None
        assert result["n_blocks"] == 10

    def test_reclassify_update_writes_to_db(self, tmp_path):
        """With update=True, the new classification should be written to DB."""
        from noise_warden.reclassify import reclassify_incident

        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(10))
        storage, iid = self._make_db_with_incident(tmp_path, wav_path)

        result = reclassify_incident(
            storage, iid, _minimal_detection_cfg(), _minimal_audio_cfg(),
            update=True,
        )

        # Verify DB was updated
        inc = storage.get_incident(iid)
        assert inc["classification"] == result["dominant"]
        assert inc["class_journal"] is not None
        journal = json.loads(inc["class_journal"])
        assert len(journal) >= 1

    def test_reclassify_missing_snippet_returns_none(self, tmp_path):
        """Incident with missing snippet file should return None gracefully."""
        from noise_warden.reclassify import reclassify_incident

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path)
        iid = storage.create_incident({
            "start_ts": "2026-04-08T12:00:00-06:00",
            "start_db": 70.0,
            "peak_db": 85.0,
            "avg_db": 78.0,
            "threshold_db": 65.0,
            "music_like_score": 0.3,
            "beat_confidence": 0.2,
            "classification": "unknown",
            "mode": "continuous",
            "snippet_path": "/nonexistent/file.wav",
        })

        result = reclassify_incident(
            storage, iid, _minimal_detection_cfg(), _minimal_audio_cfg()
        )
        assert result is None

    def test_reclassify_nonexistent_incident_returns_none(self, tmp_path):
        """Non-existent incident ID should return None."""
        from noise_warden.reclassify import reclassify_incident

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path)

        result = reclassify_incident(
            storage, 9999, _minimal_detection_cfg(), _minimal_audio_cfg()
        )
        assert result is None


# ---------------------------------------------------------------------------
# normalize_snippet
# ---------------------------------------------------------------------------

class TestNormalizeSnippet:
    """Tests for the standalone normalize_snippet function."""

    def test_boosts_quiet_recording(self, tmp_path):
        """A quiet recording (-40 dBFS peak) should be boosted to target."""
        wav_path = str(tmp_path / "quiet.wav")
        # amplitude 0.01 ≈ -40 dBFS peak
        _write_wav(wav_path, _make_tone(440, 2.0, amplitude=0.01))

        result = normalize_snippet(wav_path, target_peak_dbfs=-6.0)

        assert result is not None
        assert result["action"] == "normalized"
        assert result["gain_db"] > 0
        assert result["new_peak_dbfs"] == -6.0

        # Verify the file was actually modified — peak should be near target
        data, _ = sf.read(wav_path, dtype="float32")
        actual_peak_dbfs = 20.0 * np.log10(float(np.max(np.abs(data))))
        assert abs(actual_peak_dbfs - (-6.0)) < 0.5

    def test_skips_loud_recording(self, tmp_path):
        """A recording already louder than target should be left alone."""
        wav_path = str(tmp_path / "loud.wav")
        # amplitude 0.8 ≈ -1.9 dBFS peak — well above -6 target
        _write_wav(wav_path, _make_tone(440, 2.0, amplitude=0.8))

        result = normalize_snippet(wav_path, target_peak_dbfs=-6.0)

        assert result is None

    def test_skips_silent_recording(self, tmp_path):
        """An essentially silent file should be skipped (no extreme boost)."""
        wav_path = str(tmp_path / "silent.wav")
        data = np.zeros(22050, dtype=np.float32)
        _write_wav(wav_path, data)

        result = normalize_snippet(wav_path, target_peak_dbfs=-6.0)

        assert result is None

    def test_custom_target(self, tmp_path):
        """Should respect a custom target_peak_dbfs value."""
        wav_path = str(tmp_path / "custom.wav")
        _write_wav(wav_path, _make_tone(440, 2.0, amplitude=0.01))

        result = normalize_snippet(wav_path, target_peak_dbfs=-12.0)

        assert result is not None
        assert result["new_peak_dbfs"] == -12.0

    def test_nonexistent_file_returns_none(self, tmp_path):
        """Missing file should not raise — returns None gracefully."""
        result = normalize_snippet(str(tmp_path / "missing.wav"))

        assert result is None


class TestReclassifyIncidentNormalize:
    """Tests for normalize integration in reclassify_incident."""

    def _make_db_with_quiet_incident(self, tmp_path):
        """Create a DB with one incident whose snippet is very quiet."""
        wav_path = str(tmp_path / "quiet_snippet.wav")
        # Very quiet recording — ~-40 dBFS
        _write_wav(wav_path, _make_noise(10, amplitude=0.01))

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path)
        iid = storage.create_incident({
            "start_ts": "2026-04-18T12:00:00-06:00",
            "start_db": 70.0,
            "peak_db": 85.0,
            "avg_db": 78.0,
            "threshold_db": 65.0,
            "music_like_score": 0.3,
            "beat_confidence": 0.2,
            "classification": "unknown",
            "mode": "continuous",
            "responded": 0,
            "merge_count": 0,
            "snippet_path": wav_path,
            "notes": "",
        })
        storage.finalize_incident(
            iid, "2026-04-18T12:01:00-06:00", 60, 85.0, 78.0, wav_path,
            class_journal=json.dumps([(0, "unknown")]),
            classification="unknown",
        )
        return storage, iid, wav_path

    def test_normalize_flag_boosts_snippet(self, tmp_path):
        """With normalize=True, snippet should be boosted after analysis."""
        from noise_warden.reclassify import reclassify_incident

        storage, iid, wav_path = self._make_db_with_quiet_incident(tmp_path)

        # Read original peak for comparison
        orig_data, _ = sf.read(wav_path, dtype="float32")
        orig_peak = float(np.max(np.abs(orig_data)))

        result = reclassify_incident(
            storage, iid, _minimal_detection_cfg(), _minimal_audio_cfg(),
            normalize=True, target_peak_dbfs=-6.0,
        )

        assert result is not None
        assert result.get("normalized") is not None
        assert result["normalized"]["action"] == "normalized"

        # The file should have been modified
        new_data, _ = sf.read(wav_path, dtype="float32")
        new_peak = float(np.max(np.abs(new_data)))
        assert new_peak > orig_peak

    def test_no_normalize_flag_leaves_snippet(self, tmp_path):
        """Without normalize=True, snippet should be left untouched."""
        from noise_warden.reclassify import reclassify_incident

        storage, iid, wav_path = self._make_db_with_quiet_incident(tmp_path)

        orig_data, _ = sf.read(wav_path, dtype="float32")
        orig_peak = float(np.max(np.abs(orig_data)))

        result = reclassify_incident(
            storage, iid, _minimal_detection_cfg(), _minimal_audio_cfg(),
            normalize=False,
        )

        assert result is not None
        assert "normalized" not in result

        # File should be unchanged
        new_data, _ = sf.read(wav_path, dtype="float32")
        new_peak = float(np.max(np.abs(new_data)))
        assert abs(new_peak - orig_peak) < 1e-6


class TestDenoiseSnippet:
    """Tests for the standalone denoise_snippet function."""

    def test_reduces_noise_preserves_tone(self, tmp_path):
        """A TRANSIENT tone embedded in white noise should emerge cleaner after
        denoising — minimum statistics only protects content that is absent
        from enough frames to stay above the per-bin percentile, which is the
        algorithm's documented contract and matches real signals (music,
        speech, bangs). A constant tone is legitimately treated as part of the
        stationary floor and subtracted.

        History: this test originally used a constant 3-second tone and
        measured the FIRST second of output — which passed only because the
        pre-v21 OLA edge bug amplified the file head to full scale, inflating
        the tone bin (honest interior ratio was 0.64, i.e. the constant tone
        did NOT survive). Rewritten 2026-10-21 alongside the edge-artifact fix
        to assert the behavior the docstring actually promises."""
        wav_path = str(tmp_path / "noisy_tone.wav")
        sr = 22050
        duration = 3.0
        noise = _make_noise(duration, sr=sr, amplitude=0.15)
        # Tone bursts with 50% duty: on during odd half-seconds. Present in
        # only half the frames, the tone bin's 10th percentile stays at the
        # noise level, so subtraction removes hiss but not the bursts.
        tone = _make_tone(1000, duration, sr=sr, amplitude=0.5)
        gate = np.zeros(len(tone), dtype=np.float32)
        half = sr // 2
        for burst_start in range(half, len(tone), 2 * half):
            gate[burst_start:burst_start + half] = 1.0
        _write_wav(wav_path, tone * gate + noise, sr=sr)

        result = denoise_snippet(wav_path, percentile=10, alpha=1.0, beta=0.02)

        assert result is not None
        assert result["action"] == "denoised"
        assert "noise_floor_db" in result
        assert "snr_improvement_db" in result

        # Measure an interior burst (1.5-2.0 s): tone bin well above the
        # median, and clearly stronger than in an interior quiet gap.
        data, _ = sf.read(wav_path, dtype="float32")
        freqs = np.fft.rfftfreq(half, 1.0 / sr)
        tone_bin = int(np.argmin(np.abs(freqs - 1000)))
        burst_mag = np.abs(np.fft.rfft(data[int(1.5 * sr):2 * sr]))
        burst_ratio = float(burst_mag[tone_bin]) / float(np.median(burst_mag[50:]))
        assert burst_ratio > 2, (
            f"Transient tone should survive denoising (ratio {burst_ratio:.2f})"
        )

    def test_skips_too_short_file(self, tmp_path):
        """Files shorter than one FFT frame should be skipped gracefully."""
        wav_path = str(tmp_path / "tiny.wav")
        # 512 samples < 1024 default fft_size
        data = np.zeros(512, dtype=np.float32)
        _write_wav(wav_path, data)

        result = denoise_snippet(wav_path)

        assert result is None

    def test_handles_silent_file(self, tmp_path):
        """A silent file (all zeros) should be processed without error."""
        wav_path = str(tmp_path / "silent.wav")
        data = np.zeros(22050 * 2, dtype=np.float32)
        _write_wav(wav_path, data)

        result = denoise_snippet(wav_path)

        # Should still return a result (denoised silence is still silence)
        assert result is not None
        assert result["action"] == "denoised"

    def test_nonexistent_file_returns_none(self, tmp_path):
        """Missing file should not raise — returns None gracefully."""
        result = denoise_snippet(str(tmp_path / "missing.wav"))

        assert result is None

    def test_custom_parameters(self, tmp_path):
        """Custom percentile/alpha/beta should be accepted without error."""
        wav_path = str(tmp_path / "custom.wav")
        tone = _make_tone(440, 2.0, amplitude=0.3)
        noise = _make_noise(2.0, amplitude=0.1)
        _write_wav(wav_path, tone + noise)

        result = denoise_snippet(
            wav_path,
            percentile=20,
            alpha=1.5,
            beta=0.05,
            fft_size=2048,
            hop_size=512,
        )

        assert result is not None
        assert result["action"] == "denoised"


class TestReclassifyIncidentDenoise:
    """Tests for denoise integration in reclassify_incident."""

    def _make_db_with_noisy_incident(self, tmp_path):
        """Create a DB with one incident whose snippet has noise + tone."""
        wav_path = str(tmp_path / "noisy_snippet.wav")
        tone = _make_tone(800, 10, amplitude=0.3)
        noise = _make_noise(10, amplitude=0.1)
        _write_wav(wav_path, tone + noise)

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path)
        iid = storage.create_incident({
            "start_ts": "2026-04-18T12:00:00-06:00",
            "start_db": 70.0,
            "peak_db": 85.0,
            "avg_db": 78.0,
            "threshold_db": 65.0,
            "music_like_score": 0.3,
            "beat_confidence": 0.2,
            "classification": "unknown",
            "mode": "continuous",
            "responded": 0,
            "merge_count": 0,
            "snippet_path": wav_path,
            "notes": "",
        })
        storage.finalize_incident(
            iid, "2026-04-18T12:01:00-06:00", 60, 85.0, 78.0, wav_path,
            class_journal=json.dumps([(0, "unknown")]),
            classification="unknown",
        )
        return storage, iid, wav_path

    def test_denoise_flag_processes_snippet(self, tmp_path):
        """With denoise=True, snippet should be denoised after analysis."""
        from noise_warden.reclassify import reclassify_incident

        storage, iid, wav_path = self._make_db_with_noisy_incident(tmp_path)

        # Read original data for comparison
        orig_data, _ = sf.read(wav_path, dtype="float32")

        result = reclassify_incident(
            storage, iid, _minimal_detection_cfg(), _minimal_audio_cfg(),
            denoise=True,
        )

        assert result is not None
        assert result.get("denoised") is not None
        assert result["denoised"]["action"] == "denoised"

        # The file should have been modified
        new_data, _ = sf.read(wav_path, dtype="float32")
        # Not byte-identical — denoising changes the waveform
        assert not np.array_equal(orig_data, new_data)

    def test_no_denoise_flag_leaves_snippet(self, tmp_path):
        """Without denoise=True, snippet should be left untouched."""
        from noise_warden.reclassify import reclassify_incident

        storage, iid, wav_path = self._make_db_with_noisy_incident(tmp_path)

        orig_data, _ = sf.read(wav_path, dtype="float32")

        result = reclassify_incident(
            storage, iid, _minimal_detection_cfg(), _minimal_audio_cfg(),
            denoise=False,
        )

        assert result is not None
        assert "denoised" not in result

        # File should be unchanged
        new_data, _ = sf.read(wav_path, dtype="float32")
        assert np.array_equal(orig_data, new_data)


# ---------------------------------------------------------------------------
# Lead-in / lead-out parity with engine
# ---------------------------------------------------------------------------

class TestAnalyzeClipLeadInOut:
    """Verify that engine_captured=True marks preroll and post-trigger blocks."""

    def test_engine_captured_adds_lead_in(self, tmp_path):
        """With engine_captured=True and snippet_pre_seconds=2, the first 2
        blocks should be classified as 'lead-in', not run through the DSP."""
        wav_path = str(tmp_path / "test.wav")
        # 10 blocks total: 2 lead-in + 5 body + 3 lead-out
        _write_wav(wav_path, _make_noise(10))

        audio_cfg = {**_minimal_audio_cfg(), "snippet_pre_seconds": 2, "snippet_post_seconds": 3}
        result = analyze_clip(wav_path, _minimal_detection_cfg(), audio_cfg, engine_captured=True)

        # First two blocks should be lead-in
        assert result["blocks"][0]["classification"] == "lead-in"
        assert result["blocks"][1]["classification"] == "lead-in"
        # Body blocks should NOT be lead-in
        assert result["blocks"][2]["classification"] != "lead-in"

    def test_engine_captured_adds_lead_out(self, tmp_path):
        """With engine_captured=True and snippet_post_seconds=3, the last 3
        blocks should be classified as 'lead-out'."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(10))

        audio_cfg = {**_minimal_audio_cfg(), "snippet_pre_seconds": 2, "snippet_post_seconds": 3}
        result = analyze_clip(wav_path, _minimal_detection_cfg(), audio_cfg, engine_captured=True)

        # Last 3 blocks are lead-out
        assert result["blocks"][-1]["classification"] == "lead-out"
        assert result["blocks"][-2]["classification"] == "lead-out"
        assert result["blocks"][-3]["classification"] == "lead-out"
        # Body block before lead-out should NOT be lead-out
        assert result["blocks"][-4]["classification"] != "lead-out"

    def test_engine_captured_journal_has_negative_lead_in(self, tmp_path):
        """Lead-in journal entry should use a negative timestamp (like the
        engine's convention) so journals compare correctly."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(10))

        audio_cfg = {**_minimal_audio_cfg(), "snippet_pre_seconds": 2, "snippet_post_seconds": 3}
        result = analyze_clip(wav_path, _minimal_detection_cfg(), audio_cfg, engine_captured=True)

        # First journal entry should be lead-in with a negative timestamp
        assert result["journal"][0][1] == "lead-in"
        assert result["journal"][0][0] < 0

    def test_default_no_lead_in_lead_out(self, tmp_path):
        """Without engine_captured=True, no blocks should be lead-in/lead-out
        even when snippet_pre/post_seconds are configured."""
        wav_path = str(tmp_path / "test.wav")
        _write_wav(wav_path, _make_noise(10))

        audio_cfg = {**_minimal_audio_cfg(), "snippet_pre_seconds": 2, "snippet_post_seconds": 3}
        result = analyze_clip(wav_path, _minimal_detection_cfg(), audio_cfg)

        for block in result["blocks"]:
            assert block["classification"] not in ("lead-in", "lead-out")

    def test_lead_in_excluded_from_dominant(self):
        """lead-in and lead-out are structural bookends — they should be
        completely invisible to the dominant classification. A journal with
        only mower plus bookends should return plain 'mower', not 'mower+'."""
        journal = [(-5, "lead-in"), (0, "mower"), (50, "lead-out")]
        result = _compute_dominant(journal, 55)
        assert result == "mower"

    def test_lead_in_never_becomes_primary(self):
        """When all non-bookend classes are ignorable (unknown, engine_noise),
        the fallback should pick the longest ignorable class — NOT lead-in.
        Regression test: a 13-second all-unknown incident was classified as
        'lead-in+' because the 2s preroll outweighed other short blocks."""
        journal = [(-2, "lead-in"), (0, "unknown"), (1, "engine_noise")]
        result = _compute_dominant(journal, 13)
        assert "lead-in" not in result
        # engine_noise holds 12s vs unknown's 1s — should win
        assert result == "engine_noise+"

    def test_lead_in_single_source_fallback_never_returns_bookend(self):
        """Single-source path: when a short clip has only lead-in + unknown +
        lead-out (1 non-bookend class), the single-source fallback should
        return 'unknown', not 'lead-in'. Regression from incident 1287: a
        6-block recording with 2 lead-in, 1 unknown, 3 lead-out returned
        'lead-in' because journal[0][1] was the raw fallback."""
        journal = [(-2, "lead-in"), (0, "unknown"), (1, "lead-out")]
        result = _compute_dominant(journal, 6)
        assert result == "unknown"
        assert "lead-in" not in result

    def test_lead_in_lead_out_clamped_when_too_large(self, tmp_path):
        """If pre+post >= clip length, both revert to 0 (full DSP analysis)."""
        wav_path = str(tmp_path / "test.wav")
        # Only 3 blocks, but pre+post want 5
        _write_wav(wav_path, _make_noise(3))

        audio_cfg = {**_minimal_audio_cfg(), "snippet_pre_seconds": 2, "snippet_post_seconds": 3}
        result = analyze_clip(wav_path, _minimal_detection_cfg(), audio_cfg, engine_captured=True)

        # Should have no lead-in/lead-out since they'd consume the whole clip
        for block in result["blocks"]:
            assert block["classification"] not in ("lead-in", "lead-out")

    def test_unknown_still_produces_plus_suffix(self):
        """Unknown blocks (genuinely ambiguous audio) should still trigger the
        '+' suffix — only structural bookends are invisible."""
        journal = [(0, "unknown"), (5, "mower")]
        result = _compute_dominant(journal, 60)
        assert result == "mower+"


# ---------------------------------------------------------------------------
# Streaming per-clip envelope metrics (v21, DECISIONS D13)
# ---------------------------------------------------------------------------

class TestEnvelopeDepthMetricsAggregate:
    """envelope_depth_metrics streams a WAV in 8s windows and aggregates the
    per-window dsp metrics: p90 of window depths, median swings among deep
    (>=12 dB) windows. Streaming keeps memory flat per DECISIONS D11."""

    SR = 22050

    def _write(self, tmp_path, name, samples):
        import soundfile as sf
        path = str(tmp_path / name)
        sf.write(path, samples.astype("float32"), self.SR)
        return path

    def test_beat_clip_aggregates_deep(self, tmp_path):
        import numpy as np
        from noise_warden.reclassify import envelope_depth_metrics
        rng = np.random.default_rng(42)
        samples = rng.normal(0.0, 0.002, self.SR * 16)
        tone = np.sin(2 * np.pi * 60.0 * np.arange(int(self.SR * 0.15)) / self.SR) * 0.6
        for second in range(16):
            samples[second * self.SR:second * self.SR + len(tone)] += tone
        depth_db, swings = envelope_depth_metrics(self._write(tmp_path, "beats.wav", samples))
        assert depth_db is not None and depth_db >= 14.0
        assert swings >= 6

    def test_drone_clip_aggregates_shallow_with_zero_swings(self, tmp_path):
        import numpy as np
        from noise_warden.reclassify import envelope_depth_metrics
        samples = np.sin(2 * np.pi * 60.0 * np.arange(self.SR * 16) / self.SR) * 0.3
        depth_db, swings = envelope_depth_metrics(self._write(tmp_path, "drone.wav", samples))
        assert depth_db is not None and depth_db < 6.0
        assert swings == 0, "No deep windows → swing median must be 0"

    def test_clip_shorter_than_one_window_returns_none(self, tmp_path):
        import numpy as np
        from noise_warden.reclassify import envelope_depth_metrics
        samples = np.sin(2 * np.pi * 60.0 * np.arange(self.SR * 3) / self.SR) * 0.3
        depth_db, swings = envelope_depth_metrics(self._write(tmp_path, "short.wav", samples))
        assert depth_db is None and swings is None


# ---------------------------------------------------------------------------
# Denoiser OLA edge artifact (v21 bug fix)
# ---------------------------------------------------------------------------

class TestDenoiseEdgeArtifact:
    """The overlap-add reconstruction must not amplify the file's edges.

    Root cause of the "hard click at clip end" regression: in the final
    fft_size - hop samples only one analysis window overlaps, its squared
    Hann tail approaches zero, and dividing by that near-zero window_sum
    amplifies spectral-subtraction residue into a burst — audible as a click
    precisely because the tail was properly faded to silence beforehand
    (observed on live incidents 9293/9294: ~0.001 faded tail, final ~2 ms
    spiking to 0.13-0.18)."""

    SR = 48000

    def test_denoise_preserves_faded_quiet_tail(self, tmp_path):
        import numpy as np
        import soundfile as sf
        from noise_warden.reclassify import denoise_snippet

        rng = np.random.default_rng(3)
        # 3 s of audible noise, then a 50 ms cosine fade to silence — the
        # exact shape _trim_snippet_tail leaves behind.
        samples = rng.normal(0.0, 0.1, self.SR * 3).astype(np.float32)
        fade_len = int(self.SR * 0.05)
        samples[-fade_len:] *= np.cos(np.linspace(0, np.pi / 2, fade_len)).astype(np.float32)

        path = str(tmp_path / "faded.wav")
        sf.write(path, samples, self.SR)

        result = denoise_snippet(path)
        assert result is not None and result["action"] == "denoised"

        cleaned, _ = sf.read(path, dtype="float32")
        # The final 5 ms were essentially silent going in (< 0.02 peak);
        # they must stay silent coming out. The bug amplified them to > 0.1.
        tail_peak = float(np.max(np.abs(cleaned[-int(self.SR * 0.005):])))
        assert tail_peak < 0.02, (
            f"Denoiser amplified the faded tail to {tail_peak:.3f} — OLA edge artifact"
        )

    def test_denoise_preserves_quiet_head(self, tmp_path):
        import numpy as np
        import soundfile as sf
        from noise_warden.reclassify import denoise_snippet

        rng = np.random.default_rng(4)
        samples = rng.normal(0.0, 0.1, self.SR * 3).astype(np.float32)
        fade_len = int(self.SR * 0.05)
        samples[:fade_len] *= np.cos(np.linspace(np.pi / 2, 0, fade_len)).astype(np.float32)

        path = str(tmp_path / "faded_head.wav")
        sf.write(path, samples, self.SR)
        assert denoise_snippet(path)["action"] == "denoised"

        cleaned, _ = sf.read(path, dtype="float32")
        head_peak = float(np.max(np.abs(cleaned[:int(self.SR * 0.005)])))
        assert head_peak < 0.02, (
            f"Denoiser amplified the quiet head to {head_peak:.3f} — OLA edge artifact"
        )


# ---------------------------------------------------------------------------
# Sample-rate normalization in analyze_clip (v21 Group A, DECISIONS D15)
# ---------------------------------------------------------------------------

class TestAnalyzeClipRateNormalization:
    """analyze_clip analyzes at min(file rate, configured rate) — DECISIONS
    D15. Downsampling to the canonical rate is exact normalization; upsampling
    is deliberately never done, because fabricating an empty top band crushes
    spectral flatness and misfires the filter chain (measured: 44100 Hz
    regression clips upsampled to 48000 classified rain as diesel+)."""

    def _lowpassed_noise(self, sr, seconds=6, cutoff_hz=8000, seed=11):
        """Broadband noise with no content above cutoff_hz, so the same signal
        can be represented exactly at every rate involved."""
        rng = np.random.default_rng(seed)
        data = rng.normal(0.0, 0.2, sr * seconds)
        spec = np.fft.rfft(data)
        freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
        spec[freqs > cutoff_hz] = 0.0
        return np.fft.irfft(spec, n=len(data)).astype(np.float32)

    def test_higher_rate_file_downsampled_to_config_rate(self, tmp_path):
        """The same content in a 96000 container and a 48000 container must
        produce near-identical features under a 48000 config — the oversampled
        file is normalized down to canonical."""
        from noise_warden.dsp import resample_audio
        target_sr = 48000
        master = self._lowpassed_noise(target_sr)
        path_48k = str(tmp_path / "content_48k.wav")
        sf.write(path_48k, master, target_sr)
        path_96k = str(tmp_path / "content_96k.wav")
        sf.write(path_96k, resample_audio(master, target_sr, 96000), 96000)

        audio_cfg = {"sample_rate": target_sr, "block_seconds": 1.0}
        result_48k = analyze_clip(path_48k, _minimal_detection_cfg(), audio_cfg)
        result_96k = analyze_clip(path_96k, _minimal_detection_cfg(), audio_cfg)

        blocks_48k = result_48k["blocks"]
        blocks_96k = result_96k["blocks"]
        assert len(blocks_48k) == len(blocks_96k), (
            "Block counts must match — both files hold the same seconds of audio"
        )
        for key in ("flatness", "lowband", "midband", "highband"):
            med_48k = float(np.median([b[key] for b in blocks_48k]))
            med_96k = float(np.median([b[key] for b in blocks_96k]))
            assert med_96k == pytest.approx(med_48k, rel=0.05, abs=0.02), (
                f"{key}: 96000-file median {med_96k:.4f} vs 48000-file {med_48k:.4f} "
                f"— downsample normalization missing or broken"
            )

    def test_lower_rate_file_analyzed_at_native_rate(self, tmp_path):
        """A sub-canonical file is analyzed at its own rate: features must
        match a run where the config rate equals the file rate (i.e. no
        upsampling-induced distortion is introduced by a higher config rate)."""
        file_sr = 22050
        master = self._lowpassed_noise(file_sr)
        path_22k = str(tmp_path / "content_22k.wav")
        sf.write(path_22k, master, file_sr)

        result_under_48k_cfg = analyze_clip(
            path_22k, _minimal_detection_cfg(), {"sample_rate": 48000, "block_seconds": 1.0})
        result_under_native_cfg = analyze_clip(
            path_22k, _minimal_detection_cfg(), {"sample_rate": file_sr, "block_seconds": 1.0})

        for key in ("flatness", "lowband", "midband", "highband"):
            a = float(np.median([b[key] for b in result_under_48k_cfg["blocks"]]))
            b = float(np.median([b[key] for b in result_under_native_cfg["blocks"]]))
            assert a == pytest.approx(b, rel=1e-6), (
                f"{key}: sub-rate file was altered by a higher config rate "
                f"({a:.4f} vs native {b:.4f}) — upsampling must never happen"
            )


# ---------------------------------------------------------------------------
# reclassify_all resilience (v21 follow-up)
# ---------------------------------------------------------------------------

class TestReclassifyAllResilience:
    """A batch run must survive an unreadable WAV — in production the newest
    incident's WAV is still held open by the engine with an unfinalized header,
    and both 2026-10-21 migration dry runs crashed at 325/327 on exactly that
    file instead of reporting the 253 changes they had already computed."""

    def test_unreadable_wav_is_skipped_not_fatal(self, tmp_path):
        import numpy as np
        from noise_warden.reclassify import reclassify_all
        from noise_warden.storage import Storage

        storage = Storage(str(tmp_path / "test.db"))
        snippets = tmp_path / "snippets"
        snippets.mkdir()

        good = snippets / "incident_1_good.wav"
        sf.write(str(good), np.zeros(22050, dtype="float32"), 22050)
        bad = snippets / "incident_2_bad.wav"
        bad.write_bytes(b"not a wav at all")

        for path in (good, bad):
            storage.create_incident({
                "start_ts": "2026-04-01T12:00:00+00:00", "start_db": 70,
                "peak_db": 70, "avg_db": 70, "threshold_db": 65,
                "music_like_score": 0.5, "classification": "unknown",
                "mode": "respond", "snippet_path": str(path),
            })

        # Must not raise despite the corrupt file.
        reclassify_all(storage, _minimal_detection_cfg(), _minimal_audio_cfg())
