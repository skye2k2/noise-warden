"""
Tests for noise_warden.storage — SQLite-backed incident CRUD.

Uses pytest's tmp_path fixture to create throwaway databases, so each
test is fully isolated with no cleanup needed.
"""
import os

import pytest

from noise_warden.storage import Storage


# ---------------------------------------------------------------------------
# Incident CRUD
# ---------------------------------------------------------------------------

class TestIncidentCrud:

    def test_create_returns_int_id(self, tmp_storage, sample_incident):
        iid = tmp_storage.create_incident(sample_incident)
        assert isinstance(iid, int)
        assert iid >= 1

    def test_get_incident_returns_created_row(self, tmp_storage, sample_incident):
        iid = tmp_storage.create_incident(sample_incident)
        row = tmp_storage.get_incident(iid)
        assert row is not None
        assert row["id"] == iid
        assert row["start_db"] == sample_incident["start_db"]
        assert row["classification"] == "music_like"

    def test_get_incident_nonexistent_returns_none(self, tmp_storage):
        assert tmp_storage.get_incident(9999) is None

    def test_finalize_updates_fields(self, tmp_storage, sample_incident):
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.finalize_incident(iid, "2026-04-01T12:05:00+00:00", 300.0, 78.0, 74.0, "/tmp/snip.wav")
        row = tmp_storage.get_incident(iid)
        assert row["end_ts"] == "2026-04-01T12:05:00+00:00"
        assert row["duration_sec"] == pytest.approx(300.0)
        assert row["peak_db"] == pytest.approx(78.0)
        assert row["avg_db"] == pytest.approx(74.0)
        assert row["snippet_path"] == "/tmp/snip.wav"

    def test_update_notes(self, tmp_storage, sample_incident):
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.update_incident_notes(iid, "Neighbor's bass at 2 AM again")
        row = tmp_storage.get_incident(iid)
        assert row["notes"] == "Neighbor's bass at 2 AM again"


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------

class TestSoftDelete:

    def test_soft_delete_hides_from_get(self, tmp_storage, sample_incident):
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.soft_delete_incident(iid)
        assert tmp_storage.get_incident(iid) is None

    def test_soft_delete_hides_from_list(self, tmp_storage, sample_incident):
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.soft_delete_incident(iid)
        rows = tmp_storage.list_incidents()
        assert all(r["id"] != iid for r in rows)

    def test_soft_delete_all(self, tmp_storage, sample_incident):
        for _ in range(5):
            tmp_storage.create_incident(sample_incident)
        assert tmp_storage.count_incidents() == 5
        tmp_storage.soft_delete_all_incidents()
        assert tmp_storage.count_incidents() == 0

    def test_soft_delete_all_is_idempotent(self, tmp_storage):
        """Calling soft_delete_all on an empty table doesn't error."""
        tmp_storage.soft_delete_all_incidents()
        assert tmp_storage.count_incidents() == 0


# ---------------------------------------------------------------------------
# Listing & counting
# ---------------------------------------------------------------------------

class TestListAndCount:

    def _create_n(self, storage, sample, n):
        """Helper: create n incidents with sequential timestamps."""
        for i in range(n):
            row = dict(sample)
            row["start_ts"] = f"2026-04-01T{12 + i:02d}:00:00+00:00"
            storage.create_incident(row)

    def test_list_respects_limit(self, tmp_storage, sample_incident):
        self._create_n(tmp_storage, sample_incident, 10)
        rows = tmp_storage.list_incidents(limit=3)
        assert len(rows) == 3

    def test_list_respects_offset(self, tmp_storage, sample_incident):
        self._create_n(tmp_storage, sample_incident, 5)
        all_rows = tmp_storage.list_incidents(limit=100)
        offset_rows = tmp_storage.list_incidents(limit=100, offset=2)
        assert len(offset_rows) == len(all_rows) - 2

    def test_list_ordered_newest_first(self, tmp_storage, sample_incident):
        self._create_n(tmp_storage, sample_incident, 3)
        rows = tmp_storage.list_incidents()
        timestamps = [r["start_ts"] for r in rows]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_list_since_filters(self, tmp_storage, sample_incident):
        self._create_n(tmp_storage, sample_incident, 5)
        # Only incidents at 14:00+ should match
        rows = tmp_storage.list_incidents(since="2026-04-01T14:00:00+00:00")
        for r in rows:
            assert r["start_ts"] >= "2026-04-01T14:00:00+00:00"

    def test_count_matches_list_length(self, tmp_storage, sample_incident):
        self._create_n(tmp_storage, sample_incident, 7)
        assert tmp_storage.count_incidents() == 7
        assert tmp_storage.count_incidents() == len(tmp_storage.list_incidents(limit=100))


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

class TestCsvExport:

    def test_empty_export_returns_empty_string(self, tmp_storage):
        assert tmp_storage.export_csv() == ""

    def test_export_contains_headers_and_rows(self, tmp_storage, sample_incident):
        tmp_storage.create_incident(sample_incident)
        csv_text = tmp_storage.export_csv()
        lines = csv_text.strip().split("\n")
        assert len(lines) == 2  # header + 1 row
        assert "start_ts" in lines[0]
        assert "music_like" in lines[1]


# ---------------------------------------------------------------------------
# Build meta
# ---------------------------------------------------------------------------

class TestBuildMeta:

    def test_default_build_meta_is_empty(self, tmp_storage):
        meta = tmp_storage.get_build_meta()
        assert meta["notes"] == ""
        assert meta["ordinance_excerpt"] == ""

    def test_save_and_retrieve_build_meta(self, tmp_storage):
        tmp_storage.save_build_meta("Pi 4B in waterproof box", "Section 5-2B-1")
        meta = tmp_storage.get_build_meta()
        assert meta["notes"] == "Pi 4B in waterproof box"
        assert meta["ordinance_excerpt"] == "Section 5-2B-1"


# ---------------------------------------------------------------------------
# Calibration profiles
# ---------------------------------------------------------------------------

class TestCalibrationProfiles:

    def test_empty_list(self, tmp_storage):
        assert tmp_storage.list_calibration_profiles() == []

    def test_add_and_list(self, tmp_storage):
        tmp_storage.add_calibration_profile("indoor test", 88.5, "2026-04-01T10:00:00")
        profiles = tmp_storage.list_calibration_profiles()
        assert len(profiles) == 1
        assert profiles[0]["name"] == "indoor test"
        assert profiles[0]["offset_db"] == pytest.approx(88.5)

    def test_newest_first_ordering(self, tmp_storage):
        tmp_storage.add_calibration_profile("first", 80.0, "2026-04-01T10:00:00")
        tmp_storage.add_calibration_profile("second", 85.0, "2026-04-01T11:00:00")
        profiles = tmp_storage.list_calibration_profiles()
        assert profiles[0]["name"] == "second"
        assert profiles[1]["name"] == "first"


# ---------------------------------------------------------------------------
# Snippet cleanup
# ---------------------------------------------------------------------------

class TestSnippetCleanup:

    def test_removes_old_snippets(self, tmp_storage, tmp_path, sample_incident):
        snippets_dir = str(tmp_path / "snippets")
        os.makedirs(snippets_dir)

        # Create a snippet file
        snippet_file = os.path.join(snippets_dir, "old_snippet.wav")
        with open(snippet_file, "w") as f:
            f.write("fake wav data")

        # Create an incident with an old timestamp and the snippet path
        old_incident = dict(sample_incident)
        old_incident["start_ts"] = "2020-01-01T00:00:00+00:00"
        old_incident["snippet_path"] = snippet_file
        tmp_storage.create_incident(old_incident)

        removed = tmp_storage.cleanup_old_snippets(retention_days=30)
        assert removed == 1
        assert not os.path.exists(snippet_file)

    def test_keeps_recent_snippets(self, tmp_storage, tmp_path, sample_incident):
        snippets_dir = str(tmp_path / "snippets")
        os.makedirs(snippets_dir)

        snippet_file = os.path.join(snippets_dir, "recent.wav")
        with open(snippet_file, "w") as f:
            f.write("fake wav data")

        # Recent timestamp — should NOT be cleaned up
        recent_incident = dict(sample_incident)
        recent_incident["snippet_path"] = snippet_file
        tmp_storage.create_incident(recent_incident)

        removed = tmp_storage.cleanup_old_snippets(retention_days=30)
        assert removed == 0
        assert os.path.exists(snippet_file)


# ---------------------------------------------------------------------------
# Stale incident repair
# ---------------------------------------------------------------------------

class TestStaleIncidentRepair:

    def test_repairs_incident_without_end_ts(self, tmp_storage, sample_incident):
        """Incidents missing end_ts should be marked as crash-repaired."""
        iid = tmp_storage.create_incident(sample_incident)
        # create_incident leaves end_ts NULL — simulates a crash mid-incident
        repaired = tmp_storage.repair_stale_incidents()
        assert repaired == 1

        row = tmp_storage.get_incident(iid)
        assert row["end_ts"] is not None
        assert "crash-repaired" in row["notes"]

    def test_skips_already_finalized_incidents(self, tmp_storage, sample_incident):
        """Finalized incidents (with end_ts) should not be touched."""
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.finalize_incident(iid, "2026-04-01T13:00:00+00:00", 3600.0, 75.0, 70.0, None)

        repaired = tmp_storage.repair_stale_incidents()
        assert repaired == 0

    def test_skips_soft_deleted_incidents(self, tmp_storage, sample_incident):
        """Soft-deleted incidents should not be repaired."""
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.soft_delete_incident(iid)

        repaired = tmp_storage.repair_stale_incidents()
        assert repaired == 0

    def test_appends_to_existing_notes(self, tmp_storage, sample_incident):
        """If the incident already has notes, crash-repair note should be appended."""
        sample_incident["notes"] = "Loud bass at midnight"
        iid = tmp_storage.create_incident(sample_incident)

        tmp_storage.repair_stale_incidents()
        row = tmp_storage.get_incident(iid)
        assert "Loud bass at midnight" in row["notes"]
        assert "crash-repaired" in row["notes"]


# ---------------------------------------------------------------------------
# Vacuum
# ---------------------------------------------------------------------------

class TestVacuum:

    def test_vacuum_does_not_error(self, tmp_storage):
        """VACUUM should complete without raising."""
        tmp_storage.vacuum()


# ---------------------------------------------------------------------------
# WAL mode + schema versioning
# ---------------------------------------------------------------------------

class TestWalAndSchema:

    def test_wal_mode_enabled(self, tmp_path):
        """Storage should initialize with WAL journal mode for safe concurrent access."""
        db_path = str(tmp_path / "wal_test.db")
        s = Storage(db_path)
        import sqlite3
        c = sqlite3.connect(db_path)
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        c.close()
        assert mode == "wal"

    def test_schema_version_set(self, tmp_path):
        """Storage should stamp user_version on fresh databases."""
        db_path = str(tmp_path / "version_test.db")
        s = Storage(db_path)
        import sqlite3
        c = sqlite3.connect(db_path)
        version = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        assert version >= 1

    def test_start_ts_index_exists(self, tmp_path):
        """The start_ts index should exist for query performance."""
        db_path = str(tmp_path / "index_test.db")
        s = Storage(db_path)
        import sqlite3
        c = sqlite3.connect(db_path)
        indexes = [r[1] for r in c.execute("PRAGMA index_list(incidents)").fetchall()]
        c.close()
        assert "idx_incidents_start_ts" in indexes


# ---------------------------------------------------------------------------
# Autodismissed cleanup
# ---------------------------------------------------------------------------

class TestAutodismissedCleanup:

    def test_cleanup_removes_old_quarantined_files(self, tmp_path):
        """Files in autodismissed/ older than retention should be removed."""
        snippets_dir = str(tmp_path / "snippets")
        quarantine = os.path.join(snippets_dir, "autodismissed")
        os.makedirs(quarantine)

        # Create a "stale" WAV — set modification time to 60 days ago
        old_file = os.path.join(quarantine, "old_incident.wav")
        with open(old_file, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)
        import time
        old_mtime = time.time() - (60 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))

        # Create a "fresh" WAV
        new_file = os.path.join(quarantine, "new_incident.wav")
        with open(new_file, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)

        removed = Storage._cleanup_autodismissed(snippets_dir, retention_days=30)

        assert removed == 1
        assert not os.path.exists(old_file)
        assert os.path.exists(new_file)
