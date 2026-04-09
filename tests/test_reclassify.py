"""Tests for the reclassify module — DSP re-analysis and classification regeneration."""
import json
import os
import tempfile

import numpy as np
import pytest
import soundfile as sf

from noise_warden.reclassify import analyze_clip, _compute_dominant
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
        """Multiple sources — the one holding the most seconds wins."""
        # unknown for 5 seconds, mower for 55 seconds
        journal = [(0, "unknown"), (5, "mower")]
        result = _compute_dominant(journal, 60)
        assert result == "mower (multiple)"

    def test_multiple_with_transitions(self):
        """Complex journal with several transitions."""
        journal = [(0, "unknown"), (3, "mower"), (10, "unknown"), (12, "mower")]
        # unknown: 3s + 2s = 5s, mower: 7s + (60-12)=48s = 55s
        result = _compute_dominant(journal, 60)
        assert result == "mower (multiple)"

    def test_empty_journal(self):
        assert _compute_dominant([], 60) == "unknown"

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
            "block", "dba", "centroid_hz", "flatness", "lowband", "midband",
            "highband", "mscore", "bconf", "env_std", "filter", "classification",
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
