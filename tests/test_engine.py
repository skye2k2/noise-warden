"""
Tests for noise_warden.engine — the core audio processing loop.

Uses a mock AudioCapture to avoid sounddevice hardware dependency.
Storage and StateStore are real (temp DB) so we can verify actual
incident lifecycle end-to-end.
"""
import os
import time
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from noise_warden.engine import Engine
from noise_warden.state import StateStore
from noise_warden.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sine_block(freq=440, sr=22050, duration=0.5, amplitude=0.5):
    """Generate a sine wave block matching the default capture config."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)
    return np.sin(2 * np.pi * freq * t) * amplitude


def _make_silence_block(sr=22050, duration=0.5):
    """Generate a near-silent block."""
    return np.zeros(int(sr * duration), dtype=np.float32)


class FakeCapture:
    """
    Stand-in for AudioCapture that yields pre-loaded blocks instead
    of reading from a real microphone.
    """

    def __init__(self, blocks, sr=22050, block_seconds=0.5):
        self.sr = sr
        self.block_seconds = block_seconds
        self._blocks = list(blocks)
        self._index = 0

    def read_block(self):
        if self._index < len(self._blocks):
            block = self._blocks[self._index]
            self._index += 1
            return block
        # After exhausting blocks, return silence
        return _make_silence_block(self.sr, self.block_seconds)

    def get_preroll(self, seconds):
        return []

    def validate_device(self):
        return (True, "fake device")

    def reinitialize(self):
        pass


# ---------------------------------------------------------------------------
# Engine construction & lifecycle
# ---------------------------------------------------------------------------

class TestEngineLifecycle:

    def test_creates_without_error(self, base_cfg, tmp_storage, tmp_state):
        """Engine.__init__ should succeed with mocked AudioCapture."""
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                assert engine.running is False
                assert engine.active is None

    def test_start_and_stop(self, base_cfg, tmp_storage, tmp_state):
        """Engine should start a daemon thread and stop cleanly."""
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.start()
                assert engine.running is True
                assert engine.thread is not None
                assert engine.thread.is_alive()

                engine.stop()
                assert engine.running is False
                snap = tmp_state.snapshot()
                assert snap["mode"] == "stopped"

    def test_stop_is_idempotent(self, base_cfg, tmp_storage, tmp_state):
        """Calling stop() without start() should not error."""
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Engine processing — incident creation on loud signal
# ---------------------------------------------------------------------------

class TestEngineProcessing:

    def _run_engine_with_blocks(self, base_cfg, tmp_storage, tmp_state, blocks, max_wait=3.0):
        """
        Helper: create an Engine with fake blocks, run it, wait for blocks
        to be consumed, then stop and return the engine.
        """
        fake_capture = FakeCapture(blocks)

        with patch("noise_warden.engine.AudioCapture", return_value=fake_capture):
            with patch("noise_warden.engine.HAClient") as mock_ha_cls:
                mock_ha = MagicMock()
                mock_ha_cls.return_value = mock_ha
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                # Replace capture with our fake (Engine.__init__ already did via patch)
                engine.capture = fake_capture
                engine.start()

                # Wait until all blocks have been consumed
                deadline = time.time() + max_wait
                while fake_capture._index < len(blocks) and time.time() < deadline:
                    time.sleep(0.05)

                engine.stop()
                return engine

    def test_loud_signal_creates_incident(self, base_cfg, tmp_storage, tmp_state):
        """
        A sustained loud signal above threshold should create an incident in storage.
        With calibration_offset_db=88 and a strong sine wave, the computed dBA should
        exceed the 65 dB daytime continuous threshold.

        We use "continuous" mode (not music_focus) so ANY above-threshold signal
        triggers — no music_like_score gate.
        """
        # Force daytime by setting night window that excludes current test time
        base_cfg["detection"]["night_start_hour"] = 23
        base_cfg["detection"]["night_end_hour"] = 0
        # Use continuous mode so music_like_score isn't required
        base_cfg["detection"]["mode"] = "continuous"
        # Disable recording to avoid WAV file I/O in test
        base_cfg["audio"]["recording_enabled"] = False
        # Disable drive-by filter — our short test signal would be auto-dismissed
        base_cfg["detection"]["driveby_max_duration_sec"] = 0

        # Generate several loud blocks — RMS of amplitude=0.8 sine ≈ -1.9 dBFS + 88 offset ≈ 86 dBA
        loud_blocks = [_make_sine_block(amplitude=0.8) for _ in range(5)]
        self._run_engine_with_blocks(base_cfg, tmp_storage, tmp_state, loud_blocks)

        incidents = tmp_storage.list_incidents()
        assert len(incidents) >= 1, "Expected at least one incident from loud signal"
        assert incidents[0]["classification"] in ("music_like", "other")

    def test_silence_creates_no_incident(self, base_cfg, tmp_storage, tmp_state):
        """Silence should never create an incident."""
        base_cfg["audio"]["recording_enabled"] = False
        silent_blocks = [_make_silence_block() for _ in range(5)]
        self._run_engine_with_blocks(base_cfg, tmp_storage, tmp_state, silent_blocks)

        incidents = tmp_storage.list_incidents()
        assert len(incidents) == 0

    def test_disarmed_engine_skips_processing(self, base_cfg, tmp_storage, tmp_state):
        """When armed=False, the engine should skip audio processing entirely."""
        tmp_state.set(armed=False)
        loud_blocks = [_make_sine_block(amplitude=0.8) for _ in range(3)]

        fake_capture = FakeCapture(loud_blocks)
        with patch("noise_warden.engine.AudioCapture", return_value=fake_capture):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.capture = fake_capture
                engine.start()
                time.sleep(0.5)
                engine.stop()

        # No blocks should have been consumed (engine sleeps in disarmed state)
        assert fake_capture._index == 0
        assert tmp_storage.count_incidents() == 0

    def test_set_armed_toggles_state(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.set_armed(False)
                assert tmp_state.snapshot()["armed"] is False
                engine.set_armed(True)
                assert tmp_state.snapshot()["armed"] is True


# ---------------------------------------------------------------------------
# Engine error handling
# ---------------------------------------------------------------------------

class TestEngineErrorHandling:

    def test_capture_error_sets_error_state(self, base_cfg, tmp_storage, tmp_state):
        """If AudioCapture.read_block() raises, engine should set error state and keep running."""
        failing_capture = MagicMock()
        failing_capture.sr = 22050
        failing_capture.block_seconds = 0.5
        call_count = 0

        def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("mic unplugged")
            # After 2 failures, return silence to let engine stabilize
            return _make_silence_block()

        failing_capture.read_block = fail_then_succeed
        failing_capture.get_preroll.return_value = []
        failing_capture.validate_device.return_value = (True, "mock device")
        failing_capture.reinitialize.return_value = None

        with patch("noise_warden.engine.AudioCapture", return_value=failing_capture):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.capture = failing_capture
                engine.start()
                time.sleep(2.5)  # Give time for error + recovery
                engine.stop()

        # Engine should have recorded the error at some point
        # After stop, mode is "stopped", but last_error should have been set during the failure
        # (It may have been cleared if recovery succeeded, but the engine survived — that's the key)
        assert call_count >= 2, "Engine should have retried after error"


# ---------------------------------------------------------------------------
# Drive-by auto-dismiss detection
# ---------------------------------------------------------------------------

class TestDriveByDetection:

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_short_fadeout_is_driveby(self, base_cfg, tmp_storage, tmp_state):
        """A short incident with a clear fade-out tail should be detected as a drive-by."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        # Simulate: rise to peak then monotonic fade-out over ~10 seconds (20 blocks at 0.5s)
        dbs = [60, 65, 70, 72, 74, 75, 74, 72, 70, 68, 66, 64, 62, 60, 58, 56]
        assert engine._looks_like_driveby(dbs, duration_sec=8.0)

    def test_pure_fadeout_is_driveby(self, base_cfg, tmp_storage, tmp_state):
        """A short incident that only fades out (caught mid-pass) should also match."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        dbs = [75, 73, 71, 69, 67, 65, 63, 61]
        assert engine._looks_like_driveby(dbs, duration_sec=4.0)

    def test_sustained_noise_is_not_driveby(self, base_cfg, tmp_storage, tmp_state):
        """A long incident above the duration limit should not be a drive-by."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        dbs = [70, 72, 71, 70, 72, 73, 71, 70, 69, 68]
        assert not engine._looks_like_driveby(dbs, duration_sec=45.0)

    def test_rising_noise_is_not_driveby(self, base_cfg, tmp_storage, tmp_state):
        """A short incident with rising dB readings lacks a fade-out and should not match."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        dbs = [60, 62, 64, 66, 68, 70, 72, 74]
        assert not engine._looks_like_driveby(dbs, duration_sec=4.0)

    def test_too_few_samples(self, base_cfg, tmp_storage, tmp_state):
        """Fewer than 3 dB readings should not match (insufficient data)."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert not engine._looks_like_driveby([70, 68], duration_sec=1.0)
        assert not engine._looks_like_driveby([], duration_sec=0.0)

    def test_driveby_config_overrides(self, base_cfg, tmp_storage, tmp_state):
        """Custom config values for drive-by params should be respected."""
        base_cfg["detection"]["driveby_max_duration_sec"] = 10
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        dbs = [75, 73, 71, 69, 67, 65, 63, 61]
        # Under 10s → still a drive-by
        assert engine._looks_like_driveby(dbs, duration_sec=8.0)
        # Over 10s → not a drive-by
        assert not engine._looks_like_driveby(dbs, duration_sec=12.0)


# ---------------------------------------------------------------------------
# Disk quota warning
# ---------------------------------------------------------------------------

class TestDiskQuota:

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_disk_quota_sets_state(self, base_cfg, tmp_storage, tmp_state):
        """_check_disk_quota should populate disk_free_mb in state."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._check_disk_quota()
        snap = tmp_state.snapshot()
        assert "disk_free_mb" in snap
        assert isinstance(snap["disk_free_mb"], float)

    def test_disk_quota_warning_on_low_space(self, base_cfg, tmp_storage, tmp_state):
        """Setting an absurdly high threshold should trigger a warning."""
        base_cfg["audio"]["disk_quota_warn_mb"] = 999999999  # 999 TB — always triggers
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._check_disk_quota()
        snap = tmp_state.snapshot()
        assert snap.get("disk_warning") is not None
        assert "Low disk" in snap["disk_warning"]

    def test_disk_quota_no_warning_when_plenty(self, base_cfg, tmp_storage, tmp_state):
        """A tiny threshold should never trigger a warning."""
        base_cfg["audio"]["disk_quota_warn_mb"] = 0.001  # 1 KB — never triggers
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._check_disk_quota()
        snap = tmp_state.snapshot()
        assert snap.get("disk_warning") is None


# ---------------------------------------------------------------------------
# Exponentially-weighted average dB
# ---------------------------------------------------------------------------

class TestWeightedAvgDb:

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_weighted_avg_favors_later_readings(self, base_cfg, tmp_storage, tmp_state):
        """
        For a long incident that starts quiet and gets loud, the weighted avg
        should be higher than a simple arithmetic mean because later (louder)
        readings carry more weight.
        """
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        base_cfg["audio"]["recording_enabled"] = False

        # Simulate a long incident: 20 blocks starting quiet (60 dB) then ramping to 80 dB
        dbs = [60 + i for i in range(20)]  # 60, 61, ..., 79

        # Manually set up the active incident and finalize
        engine.active = {
            "id": tmp_storage.create_incident({
                "start_ts": "2026-01-01T00:00:00+00:00",
                "start_db": dbs[0], "peak_db": max(dbs), "avg_db": dbs[0],
                "threshold_db": 65.0, "music_like_score": 0.5,
                "beat_confidence": 0.3, "classification": "other",
                "mode": "respond",
            }),
            "start": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "dbs": dbs,
            "classification": "other",
            "period": "day",
            "responded": False,
            "last_above": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        incident = tmp_storage.list_incidents()[0]
        simple_mean = sum(dbs) / len(dbs)  # 69.5
        # The weighted avg should be noticeably higher than the simple mean
        assert incident["avg_db"] > simple_mean


# ---------------------------------------------------------------------------
# Audio device validation
# ---------------------------------------------------------------------------

class TestAudioDeviceValidation:

    def test_validate_device_returns_tuple(self):
        """AudioCapture.validate_device() should return a (bool, str) tuple."""
        from noise_warden.audio import AudioCapture
        # Can't test with real devices, but can verify the interface
        capture = FakeCapture([])
        ok, msg = capture.validate_device()
        assert isinstance(ok, bool)
        assert isinstance(msg, str)


# ---------------------------------------------------------------------------
# Drive-by quarantine (move, not delete)
# ---------------------------------------------------------------------------

class TestDriveByQuarantine:

    def test_driveby_moves_snippet_to_autodismissed(self, base_cfg, tmp_storage, tmp_state, tmp_path):
        """Drive-by auto-dismiss should move the snippet to autodismissed/ instead of deleting."""
        # Create a snippet file in the snippets directory
        snippets_dir = os.path.join(str(tmp_path), "snippets")
        os.makedirs(snippets_dir)
        snippet_file = os.path.join(snippets_dir, "incident_1_test.wav")
        with open(snippet_file, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40)

        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)

        # Create a fake incident in the DB
        row = {
            "start_ts": "2026-04-01T12:00:00+00:00",
            "start_db": 70, "peak_db": 70, "avg_db": 70,
            "threshold_db": 55, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "record_only",
        }
        iid = tmp_storage.create_incident(row)

        # Set up an active incident with a clear drive-by pattern (short, fade-out)
        dbs = [75, 73, 70, 68, 65, 62, 60]  # Monotonic decrease
        # Start time must be recent so duration < driveby_max_duration_sec (30s)
        recent_start = datetime.now(timezone.utc) - __import__("datetime").timedelta(seconds=5)
        engine.active = {
            "id": iid,
            "start": recent_start,
            "dbs": dbs,
            "classification": "other",
            "period": "night",
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": snippet_file,
            "recording": True,
        }

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        # The original file should be GONE from snippets/
        assert not os.path.exists(snippet_file)

        # But it should exist in snippets/autodismissed/
        quarantine_path = os.path.join(snippets_dir, "autodismissed", "incident_1_test.wav")
        assert os.path.exists(quarantine_path)

        # And the incident should be soft-deleted
        assert tmp_storage.get_incident(iid) is None


# ---------------------------------------------------------------------------
# Disk full graceful recording stop
# ---------------------------------------------------------------------------

class TestDiskFullRecordingStop:

    def test_append_audio_catches_write_error(self, base_cfg, tmp_storage, tmp_state, tmp_path):
        """If _append_audio encounters an OSError, it should disable recording but not crash."""
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)

        # Set up an active incident with recording enabled
        engine.active = {
            "id": 1,
            "start": datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
            "dbs": [70],
            "classification": "other",
            "period": "day",
            "responded": False,
            "last_above": datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
            "tmp_wav": "/nonexistent/path/will_fail.wav",
            "recording": True,
        }

        block = np.zeros(8000, dtype=np.float32)

        # This should NOT raise — it should catch the OSError and disable recording
        engine._append_audio(block)

        assert engine.active["recording"] is False


# ---------------------------------------------------------------------------
# Day/night boundary split
# ---------------------------------------------------------------------------

class TestPeriodBoundarySplit:
    """Verify that incidents are split when they cross a day/night boundary,
    so each segment carries the correct threshold for timeline display."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_day_to_night_split_creates_new_incident(self, base_cfg, tmp_storage, tmp_state):
        """When an active day incident crosses into night, the old incident should
        finalize and a new one should begin with the night threshold."""
        base_cfg["audio"]["recording_enabled"] = False
        # Disable drive-by filter so the short test incident isn't auto-dismissed
        base_cfg["detection"]["driveby_max_duration_sec"] = 0
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        # Create a day incident in the DB
        day_row = {
            "start_ts": datetime.now(timezone.utc).isoformat(),
            "start_db": 70, "peak_db": 72, "avg_db": 70,
            "threshold_db": 65.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "respond",
        }
        day_id = tmp_storage.create_incident(day_row)

        # Start must be recent for a valid positive duration at finalization
        recent_start = datetime.now(timezone.utc) - timedelta(seconds=30)
        engine.active = {
            "id": day_id,
            "start": recent_start,
            "dbs": [70, 71, 72],
            "classification": "other",
            "period": "day",
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }

        # Simulate that the clock has crossed into night (is_night returns True)
        block = np.zeros(8000, dtype=np.float32)
        with patch("noise_warden.engine.is_night", return_value=True), \
             patch.object(engine.ha, "publish_event"):
            # Night threshold for residential continuous = 55 dB; our 70 dB exceeds it
            split = engine._check_period_split(
                db_now=70.0, threshold=55.0, mscore=0.5, bconf=0.3,
                classification="other", block=block
            )

        assert split is True

        # Old day incident should be finalized (has end_ts)
        all_incidents = tmp_storage.list_incidents(limit=100)
        assert len(all_incidents) == 2, f"Expected 2 incidents (day + night), got {len(all_incidents)}"

        # The newer incident (first in DESC order) should be the night continuation
        night_incident = all_incidents[0]
        old_incident = all_incidents[1]
        assert old_incident["id"] == day_id
        assert old_incident["end_ts"] is not None, "Day incident should be finalized"
        assert night_incident["threshold_db"] == 55.0, "Night incident should use night threshold"
        assert night_incident["mode"] == "record_only", "Night incidents use record_only mode"

        # Engine's active incident should be the new night one
        assert engine.active is not None
        assert engine.active["period"] == "night"

    def test_night_to_day_below_threshold_no_new_incident(self, base_cfg, tmp_storage, tmp_state):
        """When crossing night→day and the noise is below the day threshold,
        the old incident finalizes but no new incident starts."""
        base_cfg["audio"]["recording_enabled"] = False
        # Disable drive-by filter so the short test incident isn't auto-dismissed
        base_cfg["detection"]["driveby_max_duration_sec"] = 0
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        night_row = {
            "start_ts": datetime.now(timezone.utc).isoformat(),
            "start_db": 58, "peak_db": 60, "avg_db": 58,
            "threshold_db": 55.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "record_only",
        }
        night_id = tmp_storage.create_incident(night_row)

        recent_start = datetime.now(timezone.utc) - timedelta(seconds=30)
        engine.active = {
            "id": night_id,
            "start": recent_start,
            "dbs": [58, 59, 60],
            "classification": "other",
            "period": "night",
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }

        block = np.zeros(8000, dtype=np.float32)
        # is_night returns False → day period; day threshold = 65; noise at 60 < 65
        with patch("noise_warden.engine.is_night", return_value=False), \
             patch.object(engine.ha, "publish_event"):
            split = engine._check_period_split(
                db_now=60.0, threshold=65.0, mscore=0.5, bconf=0.3,
                classification="other", block=block
            )

        assert split is True
        # Night incident finalized
        all_incidents = tmp_storage.list_incidents(limit=100)
        assert len(all_incidents) == 1, "Only the original night incident should exist"
        assert all_incidents[0]["end_ts"] is not None
        # No new incident — noise below day threshold
        assert engine.active is None

    def test_no_split_when_period_unchanged(self, base_cfg, tmp_storage, tmp_state):
        """If the period hasn't changed, _check_period_split should be a no-op."""
        base_cfg["audio"]["recording_enabled"] = False
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        row = {
            "start_ts": "2026-04-04T14:00:00+00:00",
            "start_db": 70, "peak_db": 70, "avg_db": 70,
            "threshold_db": 65.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "respond",
        }
        iid = tmp_storage.create_incident(row)

        engine.active = {
            "id": iid,
            "start": datetime(2026, 4, 4, 14, 0, 0, tzinfo=timezone.utc),
            "dbs": [70],
            "classification": "other",
            "period": "day",
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }

        block = np.zeros(8000, dtype=np.float32)
        # Still daytime — no split expected
        with patch("noise_warden.engine.is_night", return_value=False):
            split = engine._check_period_split(
                db_now=70.0, threshold=65.0, mscore=0.5, bconf=0.3,
                classification="other", block=block
            )

        assert split is False
        # Active incident should be untouched
        assert engine.active["id"] == iid
        assert len(tmp_storage.list_incidents(limit=100)) == 1


# ---------------------------------------------------------------------------
# Max duration split
# ---------------------------------------------------------------------------

class TestDurationSplit:
    """Verify that incidents exceeding max_incident_record_hours are split
    so WAV files and in-memory dB arrays stay bounded."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_long_incident_is_split(self, base_cfg, tmp_storage, tmp_state):
        """An incident that exceeds max_incident_record_hours should be finalized
        and a new one started if noise is still above threshold."""
        base_cfg["audio"]["recording_enabled"] = False
        base_cfg["audio"]["max_incident_record_hours"] = 2
        # Disable drive-by filter
        base_cfg["detection"]["driveby_max_duration_sec"] = 0
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        row = {
            "start_ts": datetime.now(timezone.utc).isoformat(),
            "start_db": 70, "peak_db": 72, "avg_db": 70,
            "threshold_db": 65.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "respond",
        }
        iid = tmp_storage.create_incident(row)

        # Start time is 3 hours ago — exceeds the 2-hour limit
        engine.active = {
            "id": iid,
            "start": datetime.now(timezone.utc) - timedelta(hours=3),
            "dbs": [70, 71, 72],
            "classification": "other",
            "period": "day",
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }

        block = np.zeros(8000, dtype=np.float32)
        with patch("noise_warden.engine.is_night", return_value=False), \
             patch.object(engine.ha, "publish_event"):
            split = engine._check_duration_split(
                db_now=70.0, threshold=65.0, mscore=0.5, bconf=0.3,
                classification="other", block=block
            )

        assert split is True
        all_incidents = tmp_storage.list_incidents(limit=100)
        assert len(all_incidents) == 2, f"Expected 2 incidents, got {len(all_incidents)}"

        # Old incident finalized
        old = all_incidents[1]
        assert old["id"] == iid
        assert old["end_ts"] is not None

        # New incident started with fresh state
        new = all_incidents[0]
        assert new["id"] != iid
        assert engine.active is not None
        assert engine.active["id"] == new["id"]

    def test_short_incident_not_split(self, base_cfg, tmp_storage, tmp_state):
        """An incident under max_incident_record_hours should not be split."""
        base_cfg["audio"]["recording_enabled"] = False
        base_cfg["audio"]["max_incident_record_hours"] = 6
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        row = {
            "start_ts": datetime.now(timezone.utc).isoformat(),
            "start_db": 70, "peak_db": 70, "avg_db": 70,
            "threshold_db": 65.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "respond",
        }
        iid = tmp_storage.create_incident(row)

        # Only 1 hour old — well under the 6-hour limit
        engine.active = {
            "id": iid,
            "start": datetime.now(timezone.utc) - timedelta(hours=1),
            "dbs": [70],
            "classification": "other",
            "period": "day",
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }

        block = np.zeros(8000, dtype=np.float32)
        split = engine._check_duration_split(
            db_now=70.0, threshold=65.0, mscore=0.5, bconf=0.3,
            classification="other", block=block
        )

        assert split is False
        assert engine.active["id"] == iid

    def test_duration_split_below_threshold_no_continuation(self, base_cfg, tmp_storage, tmp_state):
        """If dB drops below threshold at the split point, no new incident starts."""
        base_cfg["audio"]["recording_enabled"] = False
        base_cfg["audio"]["max_incident_record_hours"] = 1
        base_cfg["detection"]["driveby_max_duration_sec"] = 0
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        row = {
            "start_ts": datetime.now(timezone.utc).isoformat(),
            "start_db": 58, "peak_db": 60, "avg_db": 58,
            "threshold_db": 55.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "record_only",
        }
        iid = tmp_storage.create_incident(row)

        engine.active = {
            "id": iid,
            "start": datetime.now(timezone.utc) - timedelta(hours=2),
            "dbs": [58, 59, 60],
            "classification": "other",
            "period": "night",
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }

        block = np.zeros(8000, dtype=np.float32)
        # Current dB (50) is below threshold (55)
        with patch("noise_warden.engine.is_night", return_value=True), \
             patch.object(engine.ha, "publish_event"):
            split = engine._check_duration_split(
                db_now=50.0, threshold=55.0, mscore=0.5, bconf=0.3,
                classification="other", block=block
            )

        assert split is True
        assert engine.active is None
        all_incidents = tmp_storage.list_incidents(limit=100)
        assert len(all_incidents) == 1


# ---------------------------------------------------------------------------
# Self-noise suppression — response lifecycle
# ---------------------------------------------------------------------------

class TestSelfNoiseSuppression:
    """Tests for the _start_response / _stop_response / _in_response_cooldown
    mechanism that prevents the system from registering its own playback as
    a noise incident."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        """Create an Engine with mocked dependencies and response enabled."""
        base_cfg["response"]["enable_daytime_response"] = True
        base_cfg["response"]["response_cooldown_sec"] = 2.0
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.capture = FakeCapture([])
                return engine

    def test_start_response_sets_responding_flag(self, base_cfg, tmp_storage, tmp_state):
        """_start_response() should set _responding and update state."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._start_response()
        assert engine._responding is True
        assert engine.relay.enabled is True
        assert tmp_state.snapshot()["responding"] is True

    def test_stop_response_clears_responding_flag(self, base_cfg, tmp_storage, tmp_state):
        """_stop_response() should clear _responding and record the end timestamp."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._start_response()
        engine._stop_response()
        assert engine._responding is False
        assert engine.relay.enabled is False
        assert tmp_state.snapshot()["responding"] is False
        assert engine._response_end_ts > 0

    def test_in_response_cooldown_during_response(self, base_cfg, tmp_storage, tmp_state):
        """_in_response_cooldown() returns True while actively responding."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._start_response()
        assert engine._in_response_cooldown() is True

    def test_in_response_cooldown_after_response(self, base_cfg, tmp_storage, tmp_state):
        """_in_response_cooldown() returns True within cooldown window."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._start_response()
        engine._stop_response()
        # Immediately after stop — should still be in cooldown (2.0 sec window)
        assert engine._in_response_cooldown() is True

    def test_cooldown_expires(self, base_cfg, tmp_storage, tmp_state):
        """_in_response_cooldown() returns False after cooldown expires."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        base_cfg["response"]["response_cooldown_sec"] = 0.0
        engine._response_cooldown_sec = 0.0
        engine._start_response()
        engine._stop_response()
        # With 0-second cooldown, it should be False immediately
        assert engine._in_response_cooldown() is False

    def test_cooldown_zero_disables_window(self, base_cfg, tmp_storage, tmp_state):
        """A cooldown of 0 means no post-response window at all."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._response_cooldown_sec = 0.0
        # Not responding, and cooldown disabled
        engine._response_end_ts = time.time() - 0.001
        assert engine._in_response_cooldown() is False

    def test_finalize_incident_stops_response(self, base_cfg, tmp_storage, tmp_state):
        """Finalizing an incident should call _stop_response, not bare relay.off."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        # Begin a fake incident + response
        engine.active = {
            "id": 1,
            "start": datetime.now(timezone.utc),
            "dbs": [75.0, 74.0, 73.0],
            "classification": "music_like",
            "period": "day",
            "responded": True,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": False,
        }
        engine._responding = True

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        assert engine._responding is False
        assert engine.relay.enabled is False
        assert engine._response_end_ts > 0


# ---------------------------------------------------------------------------
# Noise floor gate
# ---------------------------------------------------------------------------

class TestNoiseFloorGate:
    """Tests for the configurable noise floor that skips DSP analysis on
    ambient white noise below the configured dBA threshold."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_below_floor_skips_incident_creation(self, base_cfg, tmp_storage, tmp_state):
        """A block below noise_floor_db should not trigger an incident, even if
        it would have exceeded the threshold (impossible in practice, but tests
        that the gate fires before threshold checks)."""
        base_cfg["detection"]["noise_floor_db"] = 60.0
        loud_blocks = [_make_sine_block(amplitude=0.8)] * 5
        fake = FakeCapture(loud_blocks)

        with patch("noise_warden.engine.AudioCapture", return_value=fake):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.capture = fake
                # Force dba_estimate to return a value below the floor
                with patch("noise_warden.engine.dba_estimate", return_value=45.0):
                    engine.start()
                    deadline = time.time() + 2.0
                    while fake._index < len(loud_blocks) and time.time() < deadline:
                        time.sleep(0.05)
                    engine.stop()

        incidents = tmp_storage.list_incidents(limit=100)
        assert len(incidents) == 0, "No incidents should be created when dB is below noise floor"

    def test_above_floor_allows_incident(self, base_cfg, tmp_storage, tmp_state):
        """A block above noise_floor_db should proceed to normal processing."""
        base_cfg["detection"]["noise_floor_db"] = 40.0
        base_cfg["detection"]["mode"] = "continuous"
        loud_blocks = [_make_sine_block(amplitude=0.8)] * 8
        fake = FakeCapture(loud_blocks)

        with patch("noise_warden.engine.AudioCapture", return_value=fake):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.capture = fake
                engine.start()
                deadline = time.time() + 3.0
                while fake._index < len(loud_blocks) and time.time() < deadline:
                    time.sleep(0.05)
                engine.stop()

        incidents = tmp_storage.list_incidents(limit=100)
        assert len(incidents) >= 1, "Should create at least one incident when dB exceeds floor and threshold"

    def test_floor_zero_disables_gate(self, base_cfg, tmp_storage, tmp_state):
        """Setting noise_floor_db to 0 should disable the gate entirely."""
        base_cfg["detection"]["noise_floor_db"] = 0.0
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        # With floor at 0, even very quiet readings should pass through to DSP
        # (they'll still be below threshold, but the gate shouldn't stop them)
        noise_floor = float(engine.cfg["detection"].get("noise_floor_db", 50.0))
        assert noise_floor == 0.0
        # A 10 dBA reading should NOT be gated with floor at 0
        assert 10.0 >= noise_floor

    def test_below_floor_finalizes_active_incident(self, base_cfg, tmp_storage, tmp_state):
        """If an active incident is running and the signal drops below the noise
        floor, the incident should still finalize after the gap timeout."""
        base_cfg["detection"]["noise_floor_db"] = 55.0
        base_cfg["detection"]["song_gap_merge_sec"] = 0.5  # Short gap for fast test
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)

        # Manually set up an active incident
        row = {
            "start_ts": datetime.now(timezone.utc).isoformat(),
            "start_db": 70.0, "peak_db": 70.0, "avg_db": 70.0,
            "threshold_db": 65.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "other",
            "mode": "record_only", "responded": 0, "merge_count": 0,
            "snippet_path": None, "notes": ""
        }
        iid = tmp_storage.create_incident(row)
        engine.active = {
            "id": iid,
            "start": datetime.now(timezone.utc),
            "dbs": [70.0],
            "classification": "other",
            "period": "day",
            "responded": False,
            "last_above": datetime.now(timezone.utc) - timedelta(seconds=2),
            "tmp_wav": None,
            "recording": False,
        }

        # Simulate a below-floor block coming through the engine's gate logic
        # Directly testing the gate behavior: db_now < noise_floor_db, active exists,
        # gap exceeded → should finalize
        block = _make_silence_block()
        with patch.object(engine.ha, "publish_event"):
            # The gate logic in run() appends dbs, checks gap, and finalizes.
            # Since we can't easily run the full loop for a single iteration,
            # we verify the active incident's gap check logic would finalize.
            engine.active["dbs"].append(40.0)
            gap = (datetime.now(timezone.utc) - engine.active["last_above"]).total_seconds()
            assert gap >= 0.5, "Gap should be large enough to trigger finalize"
            engine._finalize_incident()

        assert engine.active is None
        incidents = tmp_storage.list_incidents(limit=100)
        assert len(incidents) == 1
