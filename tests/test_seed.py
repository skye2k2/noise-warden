"""Tests for noise_warden.seed — classification data seeding into the database."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
import soundfile as sf

from noise_warden.seed import discover_clips, seed_all, seed_clip
from noise_warden.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_wav(path, duration_sec=3, sample_rate=22050):
    """Create a minimal WAV file with a 440 Hz sine tone for testing."""
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    audio = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(path), audio, sample_rate)


# ---------------------------------------------------------------------------
# discover_clips
# ---------------------------------------------------------------------------

class TestDiscoverClips:
    def test_finds_wav_files(self, tmp_path):
        """Should find .wav files and ignore non-wav files."""
        _make_test_wav(tmp_path / "birdsong.wav")
        _make_test_wav(tmp_path / "mower.wav")
        (tmp_path / "README.md").write_text("notes")
        (tmp_path / "data.json").write_text("{}")

        clips = discover_clips(str(tmp_path))
        filenames = [c[0] for c in clips]
        assert filenames == ["birdsong.wav", "mower.wav"]

    def test_returns_sorted(self, tmp_path):
        """Clips should be alphabetically sorted for deterministic seeding."""
        _make_test_wav(tmp_path / "zebra.wav")
        _make_test_wav(tmp_path / "alpha.wav")

        clips = discover_clips(str(tmp_path))
        filenames = [c[0] for c in clips]
        assert filenames == ["alpha.wav", "zebra.wav"]

    def test_empty_directory(self, tmp_path):
        """Empty directory should return empty list, not error."""
        clips = discover_clips(str(tmp_path))
        assert clips == []

    def test_nonexistent_directory(self):
        """Nonexistent directory should return empty list, not error."""
        clips = discover_clips("/nonexistent/path")
        assert clips == []


# ---------------------------------------------------------------------------
# seed_clip
# ---------------------------------------------------------------------------

class TestSeedClip:
    def test_creates_incident_and_copies_wav(self, tmp_path, base_cfg):
        """Seeding a clip should create a DB row and copy the WAV."""
        wav_path = tmp_path / "test_clip.wav"
        _make_test_wav(wav_path)

        snippets_dir = str(tmp_path / "snippets")
        db_path = str(tmp_path / "seed_test.db")
        storage = Storage(db_path)

        result = seed_clip(
            storage, str(wav_path), "test_clip.wav",
            base_cfg["detection"], base_cfg["audio"], base_cfg,
            snippets_dir,
        )

        assert result is not None
        assert result["id"] == 1
        assert result["filename"] == "test_clip.wav"
        assert result["classification"] is not None

        # Verify snippet was copied
        assert os.path.exists(result["snippet_path"])
        assert "incident_1_test_clip.wav" in result["snippet_path"]

    def test_incident_row_is_complete(self, tmp_path, base_cfg):
        """The incident row should have all required fields populated."""
        wav_path = tmp_path / "complete_test.wav"
        _make_test_wav(wav_path)

        snippets_dir = str(tmp_path / "snippets")
        db_path = str(tmp_path / "seed_complete.db")
        storage = Storage(db_path)

        result = seed_clip(
            storage, str(wav_path), "complete_test.wav",
            base_cfg["detection"], base_cfg["audio"], base_cfg,
            snippets_dir,
        )

        # Read back the row and check all fields
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM incidents WHERE id=?", (result["id"],)).fetchone()
        conn.close()

        assert row["start_ts"] is not None
        assert row["end_ts"] is not None
        assert row["duration_sec"] > 0
        assert row["peak_db"] > 0
        assert row["avg_db"] > 0
        assert row["classification"] is not None
        assert row["snippet_path"] is not None
        assert row["class_journal"] is not None
        assert "Seeded from" in row["notes"]

        # Journal should be valid JSON
        journal = json.loads(row["class_journal"])
        assert isinstance(journal, list)


# ---------------------------------------------------------------------------
# seed_all
# ---------------------------------------------------------------------------

class TestSeedAll:
    def test_seeds_multiple_clips(self, tmp_path, base_cfg):
        """seed_all should create one incident per WAV file."""
        data_dir = tmp_path / "classification_data"
        data_dir.mkdir()
        _make_test_wav(data_dir / "alpha.wav")
        _make_test_wav(data_dir / "beta.wav")
        _make_test_wav(data_dir / "gamma.wav")

        snippets_dir = str(tmp_path / "snippets")
        db_path = str(tmp_path / "seed_multi.db")
        storage = Storage(db_path)

        results = seed_all(
            storage, str(data_dir),
            base_cfg["detection"], base_cfg["audio"], base_cfg,
            snippets_dir,
        )

        assert len(results) == 3
        # Incident IDs should be sequential
        ids = [r["id"] for r in results]
        assert ids == [1, 2, 3]

    def test_dry_run_creates_no_rows(self, tmp_path, base_cfg):
        """Dry run should analyze but not touch the database."""
        data_dir = tmp_path / "classification_data"
        data_dir.mkdir()
        _make_test_wav(data_dir / "test.wav")

        results = seed_all(
            storage=None,
            classification_dir=str(data_dir),
            detection_cfg=base_cfg["detection"],
            audio_cfg=base_cfg["audio"],
            full_cfg=base_cfg,
            snippets_dir=None,
            dry_run=True,
        )

        assert len(results) == 1
        # dry_run results should NOT have an "id" key
        assert "id" not in results[0]

    def test_empty_directory_returns_empty(self, tmp_path, base_cfg):
        """No clips means no incidents — should not error."""
        data_dir = tmp_path / "empty_data"
        data_dir.mkdir()

        db_path = str(tmp_path / "seed_empty.db")
        storage = Storage(db_path)

        results = seed_all(
            storage, str(data_dir),
            base_cfg["detection"], base_cfg["audio"], base_cfg,
            str(tmp_path / "snippets"),
        )

        assert results == []
