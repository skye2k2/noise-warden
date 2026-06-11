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

    def test_soft_delete_removes_snippet_file(self, tmp_storage, tmp_path, sample_incident):
        """Deleting an incident should remove its snippet WAV from disk
        and hard-delete the DB row entirely."""
        snippets_dir = str(tmp_path / "snippets")
        os.makedirs(snippets_dir)
        snippet_file = os.path.join(snippets_dir, "incident_1.wav")
        with open(snippet_file, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)

        row = dict(sample_incident)
        row["snippet_path"] = snippet_file
        iid = tmp_storage.create_incident(row)
        tmp_storage.finalize_incident(iid, "2026-04-01T12:05:00+00:00", 30, 75.0, 72.0, snippet_file)

        tmp_storage.soft_delete_incident(iid)

        # File should be removed from disk
        assert not os.path.exists(snippet_file)

        # Row should be fully deleted from the DB (hard delete)
        import sqlite3
        c = sqlite3.connect(str(tmp_path / "test.db"))
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
        c.close()
        assert row is None

    def test_soft_delete_removes_autodismissed_snippet(self, tmp_storage, tmp_path, sample_incident):
        """Soft-delete should find and remove snippet from autodismissed/ too."""
        snippets_dir = str(tmp_path / "snippets")
        quarantine = os.path.join(snippets_dir, "autodismissed")
        os.makedirs(quarantine)
        # The snippet_path points to the original location, but the actual
        # file was moved to autodismissed/ by the engine
        orig_path = os.path.join(snippets_dir, "incident_1.wav")
        quarantined_path = os.path.join(quarantine, "incident_1.wav")
        with open(quarantined_path, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)

        row = dict(sample_incident)
        row["snippet_path"] = orig_path
        iid = tmp_storage.create_incident(row)
        tmp_storage.finalize_incident(iid, "2026-04-01T12:05:00+00:00", 30, 75.0, 72.0, orig_path)

        tmp_storage.soft_delete_incident(iid)

        assert not os.path.exists(quarantined_path)

    def test_soft_delete_without_snippet_is_safe(self, tmp_storage, sample_incident):
        """Soft-deleting an incident with no snippet_path should not error."""
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.soft_delete_incident(iid)
        assert tmp_storage.get_incident(iid) is None

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

    def test_hard_clear_resets_id_counter(self, tmp_storage, sample_incident):
        """hard_clear_all_incidents should delete all rows and reset autoincrement
        so the next incident starts at ID 1."""
        for _ in range(5):
            tmp_storage.create_incident(sample_incident)
        assert tmp_storage.count_incidents() == 5

        tmp_storage.hard_clear_all_incidents()
        assert tmp_storage.count_incidents() == 0

        # Next incident should get ID 1, not 6
        new_id = tmp_storage.create_incident(sample_incident)
        assert new_id == 1

    def test_hard_clear_resets_id_after_deletion(self, tmp_storage, sample_incident):
        """hard_clear should reset the autoincrement counter even after
        individual deletions have already removed all rows."""
        iid = tmp_storage.create_incident(sample_incident)
        tmp_storage.soft_delete_incident(iid)
        # Row is already gone (hard-deleted), but autoincrement counter is still at 1
        tmp_storage.hard_clear_all_incidents()

        # ID counter should be reset — next incident gets ID 1
        new_id = tmp_storage.create_incident(sample_incident)
        assert new_id == 1

    def test_hard_clear_on_empty_is_safe(self, tmp_storage):
        """Calling hard_clear on an empty table doesn't error."""
        tmp_storage.hard_clear_all_incidents()
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

    def test_show_classifications_includes_excluded(self, tmp_storage, sample_incident):
        """show_classifications should include excluded incidents matching the filter
        alongside normal non-excluded incidents."""
        # Create a normal incident
        normal = dict(sample_incident)
        normal["classification"] = "music"
        tmp_storage.create_incident(normal)

        # Create an excluded incident
        excluded = dict(sample_incident)
        excluded["classification"] = "drive_by"
        excluded["excluded"] = 1
        tmp_storage.create_incident(excluded)

        # Default view: only normal
        assert tmp_storage.count_incidents() == 1
        assert len(tmp_storage.list_incidents()) == 1

        # With show_classifications: both visible
        assert tmp_storage.count_incidents(show_classifications=["drive_by"]) == 2
        rows = tmp_storage.list_incidents(show_classifications=["drive_by"])
        assert len(rows) == 2
        classifications = [r["classification"] for r in rows]
        assert "music" in classifications
        assert "drive_by" in classifications

    def test_show_classifications_only_selected(self, tmp_storage, sample_incident):
        """Only the selected excluded classifications should appear, not all."""
        for cls, exc in [("music", 0), ("drive_by", 1), ("too_short", 1), ("borderline", 1)]:
            row = dict(sample_incident)
            row["classification"] = cls
            row["excluded"] = exc
            tmp_storage.create_incident(row)

        # Filter for drive_by only — should see music + drive_by, not too_short/borderline
        rows = tmp_storage.list_incidents(show_classifications=["drive_by"])
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Clear auto-dismissed
# ---------------------------------------------------------------------------

class TestClearAutodismissed:

    def test_clear_removes_excluded_rows_and_files(self, tmp_storage, tmp_path, sample_incident):
        """clear_autodismissed should hard-delete excluded DB rows and quarantined WAVs."""
        snippets_dir = str(tmp_path / "snippets")
        quarantine = os.path.join(snippets_dir, "autodismissed")
        os.makedirs(quarantine)

        # Create a quarantined WAV
        wav_path = os.path.join(quarantine, "incident_1.wav")
        with open(wav_path, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)

        # Create an excluded DB entry
        row = dict(sample_incident)
        row["classification"] = "drive_by"
        row["excluded"] = 1
        row["snippet_path"] = wav_path
        tmp_storage.create_incident(row)

        # Also create a normal incident that should survive
        normal = dict(sample_incident)
        normal["classification"] = "music"
        tmp_storage.create_incident(normal)

        rows_deleted, files_removed = tmp_storage.clear_autodismissed(snippets_dir)
        assert rows_deleted == 1
        assert files_removed == 1
        assert not os.path.exists(wav_path)

        # Normal incident should still exist
        assert tmp_storage.count_incidents() == 1

    def test_clear_on_empty_is_safe(self, tmp_storage, tmp_path):
        """Clearing when no excluded incidents exist should not error."""
        snippets_dir = str(tmp_path / "snippets")
        rows_deleted, files_removed = tmp_storage.clear_autodismissed(snippets_dir)
        assert rows_deleted == 0
        assert files_removed == 0


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


# ---------------------------------------------------------------------------
# Purge orphaned incidents
# ---------------------------------------------------------------------------

class TestPurgeOrphans:

    def test_nulls_snippet_path_for_missing_files(self, tmp_storage, sample_incident):
        """Rows referencing a nonexistent WAV should have snippet_path NULLed."""
        row = dict(sample_incident)
        row["snippet_path"] = "/nonexistent/path/to/snippet.wav"
        iid = tmp_storage.create_incident(row)
        tmp_storage.finalize_incident(iid, "2026-04-01T12:05:00+00:00", 30, 75.0, 72.0,
                                      "/nonexistent/path/to/snippet.wav")

        count = tmp_storage.purge_orphaned_incidents()
        assert count == 1

        inc = tmp_storage.get_incident(iid)
        assert inc["snippet_path"] is None

    def test_leaves_existing_files_alone(self, tmp_storage, tmp_path, sample_incident):
        """Rows referencing an existing WAV should not be touched."""
        snippet_file = str(tmp_path / "existing.wav")
        with open(snippet_file, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)

        row = dict(sample_incident)
        row["snippet_path"] = snippet_file
        iid = tmp_storage.create_incident(row)
        tmp_storage.finalize_incident(iid, "2026-04-01T12:05:00+00:00", 30, 75.0, 72.0,
                                      snippet_file)

        count = tmp_storage.purge_orphaned_incidents()
        assert count == 0

        inc = tmp_storage.get_incident(iid)
        assert inc["snippet_path"] == snippet_file

    def test_skips_null_snippet_path(self, tmp_storage, sample_incident):
        """Rows with NULL snippet_path should not be counted as orphaned."""
        tmp_storage.create_incident(sample_incident)

        count = tmp_storage.purge_orphaned_incidents()
        assert count == 0

    def test_skips_deleted_rows(self, tmp_storage, sample_incident):
        """Soft-deleted rows should be ignored even if their files are missing."""
        row = dict(sample_incident)
        row["snippet_path"] = "/nonexistent/path/to/snippet.wav"
        iid = tmp_storage.create_incident(row)
        tmp_storage.finalize_incident(iid, "2026-04-01T12:05:00+00:00", 30, 75.0, 72.0,
                                      "/nonexistent/path/to/snippet.wav")
        tmp_storage.soft_delete_incident(iid)

        count = tmp_storage.purge_orphaned_incidents()
        assert count == 0


class TestRepairSnippetPaths:
    """repair_snippet_paths reconstructs snippet_path from the WAV files on disk,
    using the incident id embedded in `incident_{id}_{token}.wav` filenames. This
    recovers a database whose snippet references were wiped (e.g. by an orphan
    purge run against a copied DB before path resolution was portable)."""

    def _make_wav(self, directory, iid, token="abc"):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"incident_{iid}_{token}.wav")
        with open(path, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)
        return path

    def test_restores_null_path_from_matching_file(self, tmp_storage, tmp_path, sample_incident):
        snippets = str(tmp_path / "snippets")
        iid = tmp_storage.create_incident(sample_incident)  # snippet_path is NULL
        wav = self._make_wav(snippets, iid)

        count = tmp_storage.repair_snippet_paths(snippets_dir=snippets)
        assert count == 1
        assert tmp_storage.get_incident(iid)["snippet_path"] == wav

    def test_restores_from_autodismissed_subfolder(self, tmp_storage, tmp_path, sample_incident):
        snippets = str(tmp_path / "snippets")
        iid = tmp_storage.create_incident(sample_incident)
        wav = self._make_wav(os.path.join(snippets, "autodismissed"), iid)

        count = tmp_storage.repair_snippet_paths(snippets_dir=snippets)
        assert count == 1
        assert tmp_storage.get_incident(iid)["snippet_path"] == wav

    def test_never_overwrites_existing_path(self, tmp_storage, tmp_path, sample_incident):
        """A row that already has a snippet_path must not be clobbered."""
        snippets = str(tmp_path / "snippets")
        row = dict(sample_incident)
        row["snippet_path"] = "/some/existing/value.wav"
        iid = tmp_storage.create_incident(row)
        self._make_wav(snippets, iid)  # a matching file exists on disk

        count = tmp_storage.repair_snippet_paths(snippets_dir=snippets)
        assert count == 0
        assert tmp_storage.get_incident(iid)["snippet_path"] == "/some/existing/value.wav"

    def test_ignores_files_without_matching_row(self, tmp_storage, tmp_path):
        """A WAV whose id has no DB row should be skipped silently."""
        snippets = str(tmp_path / "snippets")
        self._make_wav(snippets, 9999)

        count = tmp_storage.repair_snippet_paths(snippets_dir=snippets)
        assert count == 0

    def test_missing_snippets_dir_is_safe(self, tmp_storage, tmp_path, sample_incident):
        tmp_storage.create_incident(sample_incident)
        count = tmp_storage.repair_snippet_paths(snippets_dir=str(tmp_path / "does_not_exist"))
        assert count == 0


# ---------------------------------------------------------------------------
# Excluded incidents
# ---------------------------------------------------------------------------

class TestExcludedIncidents:

    def test_excluded_column_defaults_to_zero(self, tmp_storage, sample_incident):
        """Normal incidents should have excluded=0 by default."""
        iid = tmp_storage.create_incident(sample_incident)
        row = tmp_storage.get_incident(iid)
        assert row["excluded"] == 0

    def test_excluded_can_be_set_on_create(self, tmp_storage, sample_incident):
        """Excluded incidents (filter hits) should be stored with excluded=1."""
        row = dict(sample_incident)
        row["classification"] = "thunder"
        row["excluded"] = 1
        iid = tmp_storage.create_incident(row)
        inc = tmp_storage.get_incident(iid)
        assert inc["excluded"] == 1
        assert inc["classification"] == "thunder"

    def test_list_excludes_excluded_by_default(self, tmp_storage, sample_incident):
        """Default list_incidents should not include excluded incidents."""
        tmp_storage.create_incident(sample_incident)
        excluded = dict(sample_incident)
        excluded["classification"] = "mower"
        excluded["excluded"] = 1
        tmp_storage.create_incident(excluded)
        rows = tmp_storage.list_incidents()
        assert len(rows) == 1
        assert rows[0]["classification"] == "music_like"

    def test_list_includes_excluded_when_requested(self, tmp_storage, sample_incident):
        """include_excluded=True should return all incidents."""
        tmp_storage.create_incident(sample_incident)
        excluded = dict(sample_incident)
        excluded["classification"] = "mower"
        excluded["excluded"] = 1
        tmp_storage.create_incident(excluded)
        rows = tmp_storage.list_incidents(include_excluded=True)
        assert len(rows) == 2

    def test_schema_migration_adds_excluded_column(self, tmp_path):
        """A fresh database should have the excluded column via migration 2+."""
        s = Storage(str(tmp_path / "test.db"))
        with s.conn() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            assert version == 4
            # Verify the column exists by inserting with excluded=1
            row = {
                "start_ts": "2026-04-01T12:00:00+00:00", "start_db": 70, "peak_db": 70,
                "avg_db": 70, "threshold_db": 65, "music_like_score": 0.5,
                "classification": "rain", "mode": "respond",
                "excluded": 1,
            }
            iid = s.create_incident(row)
            inc = s.get_incident(iid)
            assert inc["excluded"] == 1
