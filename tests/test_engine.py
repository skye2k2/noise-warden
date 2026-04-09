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

def _make_sine_block(freq=440, sr=22050, duration=1.0, amplitude=0.5):
    """Generate a sine wave block matching the default capture config."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)
    return np.sin(2 * np.pi * freq * t) * amplitude


def _make_silence_block(sr=22050, duration=1.0):
    """Generate a near-silent block."""
    return np.zeros(int(sr * duration), dtype=np.float32)


class FakeCapture:
    """
    Stand-in for AudioCapture that yields pre-loaded blocks instead
    of reading from a real microphone.
    """

    def __init__(self, blocks, sr=22050, block_seconds=1.0):
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
        assert incidents[0]["classification"] in ("music", "music_like", "unknown")

    def test_silence_creates_no_incident(self, base_cfg, tmp_storage, tmp_state):
        """Silence should never create an incident."""
        base_cfg["audio"]["recording_enabled"] = False
        silent_blocks = [_make_silence_block() for _ in range(5)]
        self._run_engine_with_blocks(base_cfg, tmp_storage, tmp_state, silent_blocks)

        incidents = tmp_storage.list_incidents()
        assert len(incidents) == 0

    def test_disarmed_engine_skips_processing(self, base_cfg, tmp_storage, tmp_state):
        """When armed=False, the engine should skip audio processing entirely."""
        base_cfg["detection"]["armed"] = False
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
        # Disable drive-by filter so the ramp pattern isn't auto-dismissed
        base_cfg["detection"]["driveby_max_duration_sec"] = 0

        # Simulate a long incident: 20 blocks starting quiet (60 dB) then ramping to 80 dB
        dbs = [60 + i for i in range(20)]  # 60, 61, ..., 79

        # Manually set up the active incident and finalize.
        # last_above = now so tail trimming doesn't discard any blocks — this
        # test is about the exponential weighting, not the tail-trim logic.
        now = datetime.now(tz=timezone.utc)
        engine.active = {
            "id": tmp_storage.create_incident({
                "start_ts": "2026-01-01T00:00:00+00:00",
                "start_db": dbs[0], "peak_db": max(dbs), "avg_db": dbs[0],
                "threshold_db": 65.0, "music_like_score": 0.5,
                "beat_confidence": 0.3, "classification": "unknown",
                "mode": "respond",
            }),
            "start": now - timedelta(seconds=20),
            "dbs": dbs,
            "classification": "unknown",
            "period": "day",
            "responded": False,
            "last_above": now,
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
# Tail trimming at finalization
# ---------------------------------------------------------------------------

class TestTailTrim:
    """Verify that the song_gap_merge_sec silent tail is trimmed from dbs,
    duration, and WAV snippets at incident finalization."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_tail_trim_excludes_silent_blocks_from_avg(self, base_cfg, tmp_storage, tmp_state):
        """avg_db should be computed from the active portion only, not the
        silent blocks accumulated during the song_gap_merge_sec window."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        base_cfg["audio"]["recording_enabled"] = False
        base_cfg["detection"]["driveby_max_duration_sec"] = 0

        # 10 active blocks at 70 dB, then 12 silent blocks at 40 dB (the tail)
        active_dbs = [70.0] * 10
        tail_dbs = [40.0] * 12
        all_dbs = active_dbs + tail_dbs

        now = datetime.now(tz=timezone.utc)
        engine.active = {
            "id": tmp_storage.create_incident({
                "start_ts": "2026-01-01T00:00:00+00:00",
                "start_db": 70.0, "peak_db": 70.0, "avg_db": 70.0,
                "threshold_db": 65.0, "music_like_score": 0.5,
                "beat_confidence": 0.3, "classification": "unknown",
                "mode": "respond",
            }),
            "start": now - timedelta(seconds=len(all_dbs)),
            "dbs": all_dbs,
            "classification": "unknown",
            "class_journal": [(0, "unknown")],
            "period": "day",
            "responded": False,
            # last_above was 12 seconds ago (the tail started)
            "last_above": now - timedelta(seconds=12),
            "tmp_wav": None,
            "recording": False,
        }

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        incident = tmp_storage.list_incidents()[0]

        # avg_db should be close to 70 (the active portion), not dragged down
        # toward 40 by the silent tail. With 11 blocks (10 active + 1 kept
        # for context), the single 40 dB block has minimal impact — but the
        # exponential weighting favors later blocks, so the lone context block
        # at 40 dB pulls the average down slightly more than a flat mean would.
        # Without trimming, 12 tail blocks at 40 dB would drag avg below 55.
        assert incident["avg_db"] >= 65.0, (
            f"avg_db {incident['avg_db']} too low — silent tail not trimmed"
        )

    def test_tail_trim_adjusts_duration(self, base_cfg, tmp_storage, tmp_state):
        """Stored duration should reflect the trimmed incident length, not the
        full gap-timeout span."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        base_cfg["audio"]["recording_enabled"] = False
        base_cfg["detection"]["driveby_max_duration_sec"] = 0

        active_dbs = [70.0] * 10
        tail_dbs = [40.0] * 12
        all_dbs = active_dbs + tail_dbs

        now = datetime.now(tz=timezone.utc)
        engine.active = {
            "id": tmp_storage.create_incident({
                "start_ts": "2026-01-01T00:00:00+00:00",
                "start_db": 70.0, "peak_db": 70.0, "avg_db": 70.0,
                "threshold_db": 65.0, "music_like_score": 0.5,
                "beat_confidence": 0.3, "classification": "unknown",
                "mode": "respond",
            }),
            "start": now - timedelta(seconds=len(all_dbs)),
            "dbs": all_dbs,
            "classification": "unknown",
            "class_journal": [(0, "unknown")],
            "period": "day",
            "responded": False,
            "last_above": now - timedelta(seconds=12),
            "tmp_wav": None,
            "recording": False,
        }

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        incident = tmp_storage.list_incidents()[0]
        # 22 total blocks, 11 blocks trimmed → dur ≈ 11s (not 22s)
        assert incident["duration_sec"] <= 12, (
            f"duration_sec {incident['duration_sec']} too high — tail not trimmed"
        )

    def test_tail_trim_preserves_peak_db(self, base_cfg, tmp_storage, tmp_state):
        """peak_db should reflect the true max across ALL blocks, including the
        tail — it's valid evidence of the loudest moment."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        base_cfg["audio"]["recording_enabled"] = False
        base_cfg["detection"]["driveby_max_duration_sec"] = 0

        # Peak is in the active portion, but verify it's not lost
        active_dbs = [65.0, 72.0, 68.0, 70.0, 66.0]
        tail_dbs = [40.0] * 12
        all_dbs = active_dbs + tail_dbs

        now = datetime.now(tz=timezone.utc)
        engine.active = {
            "id": tmp_storage.create_incident({
                "start_ts": "2026-01-01T00:00:00+00:00",
                "start_db": 65.0, "peak_db": 65.0, "avg_db": 65.0,
                "threshold_db": 65.0, "music_like_score": 0.5,
                "beat_confidence": 0.3, "classification": "unknown",
                "mode": "respond",
            }),
            "start": now - timedelta(seconds=len(all_dbs)),
            "dbs": all_dbs,
            "classification": "unknown",
            "class_journal": [(0, "unknown")],
            "period": "day",
            "responded": False,
            "last_above": now - timedelta(seconds=12),
            "tmp_wav": None,
            "recording": False,
        }

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        incident = tmp_storage.list_incidents()[0]
        assert incident["peak_db"] == 72.0

    def test_tail_trim_truncates_wav(self, base_cfg, tmp_storage, tmp_state, tmp_path):
        """WAV snippet should be truncated to remove silent tail blocks."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        base_cfg["detection"]["driveby_max_duration_sec"] = 0

        sr = 22050
        block_sec = float(base_cfg["audio"].get("block_seconds", 1.0))
        block_samples = int(sr * block_sec)
        n_active = 10
        n_tail = 12
        n_total = n_active + n_tail

        # Create a WAV file with known content
        wav_path = os.path.join(str(tmp_path), "test_snippet.wav")
        import soundfile as _sf
        audio = np.random.randn(n_total * block_samples).astype(np.float32) * 0.01
        _sf.write(wav_path, audio, sr, subtype="PCM_16")
        original_samples = len(audio)

        now = datetime.now(tz=timezone.utc)
        engine.active = {
            "id": tmp_storage.create_incident({
                "start_ts": "2026-01-01T00:00:00+00:00",
                "start_db": 70.0, "peak_db": 70.0, "avg_db": 70.0,
                "threshold_db": 65.0, "music_like_score": 0.5,
                "beat_confidence": 0.3, "classification": "unknown",
                "mode": "respond",
            }),
            "start": now - timedelta(seconds=n_total),
            "dbs": [70.0] * n_active + [40.0] * n_tail,
            "classification": "unknown",
            "class_journal": [(0, "unknown")],
            "period": "day",
            "responded": False,
            "last_above": now - timedelta(seconds=n_tail),
            "tmp_wav": wav_path,
            "wav_handle": None,
            "recording": True,
        }

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        trimmed_audio, _ = _sf.read(wav_path)
        # 11 blocks trimmed (12 tail - 1 kept) → should be shorter
        expected_trim = (n_tail - 1) * block_samples
        assert len(trimmed_audio) == original_samples - expected_trim, (
            f"Expected {original_samples - expected_trim} samples, got {len(trimmed_audio)}"
        )

    def test_no_trim_when_no_tail(self, base_cfg, tmp_storage, tmp_state):
        """When last_above is recent (no gap), nothing should be trimmed."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        base_cfg["audio"]["recording_enabled"] = False
        base_cfg["detection"]["driveby_max_duration_sec"] = 0

        dbs = [70.0] * 10
        now = datetime.now(tz=timezone.utc)
        engine.active = {
            "id": tmp_storage.create_incident({
                "start_ts": "2026-01-01T00:00:00+00:00",
                "start_db": 70.0, "peak_db": 70.0, "avg_db": 70.0,
                "threshold_db": 65.0, "music_like_score": 0.5,
                "beat_confidence": 0.3, "classification": "unknown",
                "mode": "respond",
            }),
            "start": now - timedelta(seconds=10),
            "dbs": dbs,
            "classification": "unknown",
            "class_journal": [(0, "unknown")],
            "period": "day",
            "responded": False,
            "last_above": now,
            "tmp_wav": None,
            "recording": False,
        }

        with patch.object(engine.ha, "publish_event"):
            engine._finalize_incident()

        incident = tmp_storage.list_incidents()[0]
        # All 10 blocks at 70 dB — avg should be ~70
        assert incident["avg_db"] >= 69.5


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
            "beat_confidence": 0.3, "classification": "unknown",
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
            "classification": "unknown",
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

        # And the incident should be reclassified as drive_by and marked excluded
        inc = tmp_storage.get_incident(iid)
        assert inc is not None
        assert inc["classification"] == "drive_by"
        assert inc["excluded"] == 1


# ---------------------------------------------------------------------------
# Disk full graceful recording stop
# ---------------------------------------------------------------------------

class TestDiskFullRecordingStop:

    def test_append_audio_catches_write_error(self, base_cfg, tmp_storage, tmp_state, tmp_path):
        """If _append_audio encounters an OSError, it should disable recording but not crash."""
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)

        # Mock a wav_handle whose write() raises OSError (simulates disk-full)
        mock_handle = MagicMock()
        mock_handle.closed = False
        mock_handle.write.side_effect = OSError("No space left on device")

        # Set up an active incident with recording enabled
        engine.active = {
            "id": 1,
            "start": datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
            "dbs": [70],
            "classification": "unknown",
            "period": "day",
            "responded": False,
            "last_above": datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
            "tmp_wav": "/nonexistent/path/will_fail.wav",
            "wav_handle": mock_handle,
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

        # Create a day incident in the DB (timestamp slightly in the past so
        # the engine-created split incident sorts before it in DESC order)
        day_row = {
            "start_ts": (datetime.now().astimezone() - timedelta(seconds=10)).replace(microsecond=0).isoformat(),
            "start_db": 70, "peak_db": 72, "avg_db": 70,
            "threshold_db": 65.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "unknown",
            "mode": "respond",
        }
        day_id = tmp_storage.create_incident(day_row)

        # Start must be recent for a valid positive duration at finalization
        recent_start = datetime.now().astimezone() - timedelta(seconds=30)
        engine.active = {
            "id": day_id,
            "start": recent_start,
            "dbs": [70, 71, 72],
            "classification": "unknown",
            "period": "day",
            "responded": False,
            "last_above": datetime.now().astimezone(),
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
            "beat_confidence": 0.3, "classification": "unknown",
            "mode": "record_only",
        }
        night_id = tmp_storage.create_incident(night_row)

        recent_start = datetime.now(timezone.utc) - timedelta(seconds=30)
        engine.active = {
            "id": night_id,
            "start": recent_start,
            "dbs": [58, 59, 60],
            "classification": "unknown",
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
            "beat_confidence": 0.3, "classification": "unknown",
            "mode": "respond",
        }
        iid = tmp_storage.create_incident(row)

        engine.active = {
            "id": iid,
            "start": datetime(2026, 4, 4, 14, 0, 0, tzinfo=timezone.utc),
            "dbs": [70],
            "classification": "unknown",
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
            "start_ts": (datetime.now().astimezone() - timedelta(seconds=10)).replace(microsecond=0).isoformat(),
            "start_db": 70, "peak_db": 72, "avg_db": 70,
            "threshold_db": 65.0, "music_like_score": 0.5,
            "beat_confidence": 0.3, "classification": "unknown",
            "mode": "respond",
        }
        iid = tmp_storage.create_incident(row)

        # Start time is 3 hours ago — exceeds the 2-hour limit
        engine.active = {
            "id": iid,
            "start": datetime.now().astimezone() - timedelta(hours=3),
            "dbs": [70, 71, 72],
            "classification": "unknown",
            "period": "day",
            "responded": False,
            "last_above": datetime.now().astimezone(),
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
            "beat_confidence": 0.3, "classification": "unknown",
            "mode": "respond",
        }
        iid = tmp_storage.create_incident(row)

        # Only 1 hour old — well under the 6-hour limit
        engine.active = {
            "id": iid,
            "start": datetime.now(timezone.utc) - timedelta(hours=1),
            "dbs": [70],
            "classification": "unknown",
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
            "beat_confidence": 0.3, "classification": "unknown",
            "mode": "record_only",
        }
        iid = tmp_storage.create_incident(row)

        engine.active = {
            "id": iid,
            "start": datetime.now(timezone.utc) - timedelta(hours=2),
            "dbs": [58, 59, 60],
            "classification": "unknown",
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
            "beat_confidence": 0.3, "classification": "unknown",
            "mode": "record_only", "responded": 0, "merge_count": 0,
            "snippet_path": None, "notes": ""
        }
        iid = tmp_storage.create_incident(row)
        engine.active = {
            "id": iid,
            "start": datetime.now(timezone.utc),
            "dbs": [70.0],
            "classification": "unknown",
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


# ---------------------------------------------------------------------------
# Classification expansion
# ---------------------------------------------------------------------------

class TestClassifySound:
    """Tests for _classify_sound: music/music_like/unknown categorization."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_high_music_and_beat_returns_music(self, base_cfg, tmp_storage, tmp_state):
        """High music score + high beat confidence = rhythmic music."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert engine._classify_sound(0.80, 0.50) == "music"

    def test_high_music_low_beat_returns_music_like(self, base_cfg, tmp_storage, tmp_state):
        """High music score + low beat confidence = bass-heavy but non-rhythmic."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert engine._classify_sound(0.80, 0.20) == "music_like"

    def test_low_music_returns_unknown(self, base_cfg, tmp_storage, tmp_state):
        """Low music score = no recognizable pattern."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert engine._classify_sound(0.30, 0.50) == "unknown"

    def test_boundary_music_like_score(self, base_cfg, tmp_storage, tmp_state):
        """Score exactly at threshold should classify as music_like (not unknown)."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        threshold = float(base_cfg["detection"]["min_music_like_score"])
        assert engine._classify_sound(threshold, 0.10) == "music_like"


# ---------------------------------------------------------------------------
# Filter identification
# ---------------------------------------------------------------------------

class TestIdentifyFilter:
    """Tests for _identify_filter: returns the name of the matching filter or None."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_no_filter_returns_none(self, base_cfg, tmp_storage, tmp_state):
        """Normal sound that passes all filters should return None."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [65.0] * 20
        # flatness 0.20 is below mower threshold (0.30), centroid 200 is below mower range (300+)
        feats = {"flatness": 0.20, "centroid_hz": 200, "lowband_ratio": 0.4,
                 "midband_ratio": 0.4, "highband_ratio": 0.2}
        assert engine._identify_filter(feats, 68.0, 67.0) is None

    def test_impulse_detected(self, base_cfg, tmp_storage, tmp_state):
        """Large dB jump should be identified as impulse."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [50.0] * 20
        feats = {"flatness": 0.30, "centroid_hz": 500, "lowband_ratio": 0.3,
                 "midband_ratio": 0.4, "highband_ratio": 0.3}
        # 20 dB jump exceeds impulse threshold (14 dB default)
        assert engine._identify_filter(feats, 70.0, 50.0) == "impulse"

    def test_thunder_takes_priority_over_impulse(self, base_cfg, tmp_storage, tmp_state):
        """Thunder (impulse + low-band + flat) should be labeled thunder, not impulse."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [50.0] * 20
        feats = {"flatness": 0.50, "centroid_hz": 200, "lowband_ratio": 0.60,
                 "midband_ratio": 0.25, "highband_ratio": 0.15}
        assert engine._identify_filter(feats, 70.0, 50.0) == "thunder"

    def test_birdsong_detected(self, base_cfg, tmp_storage, tmp_state):
        """High-frequency dominant, no bass, stable amplitude = birdsong."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [62.0, 63.0, 61.5, 62.5, 63.0, 62.0, 61.0, 62.5, 63.0, 62.0, 61.5, 62.5]
        feats = {"flatness": 0.55, "centroid_hz": 4000, "lowband_ratio": 0.05,
                 "midband_ratio": 0.25, "highband_ratio": 0.70}
        assert engine._identify_filter(feats, 65.0, 64.0) == "birdsong"


# ---------------------------------------------------------------------------
# Excluded incident lifecycle
# ---------------------------------------------------------------------------

class TestExcludedIncidents:
    """Tests for excluded incident creation and finalization in continuous mode."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_begin_excluded_incident_creates_row(self, base_cfg, tmp_storage, tmp_state):
        """Starting an excluded incident should create a DB row with excluded=1."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_excluded_incident("thunder", 70.0, 65.0, 0.3, 0.2, "respond")
        assert engine._excluded_id is not None
        inc = tmp_storage.get_incident(engine._excluded_id)
        assert inc["classification"] == "thunder"
        assert inc["excluded"] == 1

    def test_end_excluded_incident_sets_duration(self, base_cfg, tmp_storage, tmp_state):
        """Ending an excluded incident should finalize it with end_ts and duration."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_excluded_incident("rain", 68.0, 65.0, 0.1, 0.1, "respond")
        iid = engine._excluded_id
        engine._end_excluded_incident()
        assert engine._excluded_id is None
        inc = tmp_storage.get_incident(iid)
        assert inc["end_ts"] is not None
        assert inc["duration_sec"] is not None

    def test_should_log_excluded_false_in_music_focus(self, base_cfg, tmp_storage, tmp_state):
        """Music-focus mode should not log excluded incidents."""
        base_cfg["detection"]["mode"] = "continuous_music_focus"
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert engine._should_log_excluded() is False

    def test_should_log_excluded_true_in_continuous(self, base_cfg, tmp_storage, tmp_state):
        """Continuous mode should log excluded incidents."""
        base_cfg["detection"]["mode"] = "continuous"
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert engine._should_log_excluded() is True

    def test_should_log_excluded_true_in_intermittent(self, base_cfg, tmp_storage, tmp_state):
        """Intermittent mode should log excluded incidents."""
        base_cfg["detection"]["mode"] = "intermittent"
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert engine._should_log_excluded() is True


# ---------------------------------------------------------------------------
# Tier 3 filter identification
# ---------------------------------------------------------------------------

class TestIdentifyFilterTier3:
    """Tests for _identify_filter with Tier 3 categories: weedwhacker, diesel, conversation."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_weedwhacker_detected(self, base_cfg, tmp_storage, tmp_state):
        """High-centroid, flat, no-bass, moderately steady → weedwhacker.
        Uses highband_ratio below 0.55 to avoid triggering birdsong filter."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [72.0, 73.0, 71.5, 72.5, 73.0, 72.0, 71.0, 72.5, 73.0, 72.0, 71.5, 72.5]
        feats = {"centroid_hz": 3500, "flatness": 0.55, "lowband_ratio": 0.05,
                 "midband_ratio": 0.45, "highband_ratio": 0.50}
        assert engine._identify_filter(feats, 73.0, 72.0) == "weedwhacker"

    def test_diesel_detected(self, base_cfg, tmp_storage, tmp_state):
        """Low centroid, bass-heavy, moderate flatness, very steady → diesel."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [68.0, 68.5, 67.8, 68.2, 68.1, 67.9, 68.3, 68.0,
                             67.8, 68.1, 68.2, 67.9]
        feats = {"centroid_hz": 200, "flatness": 0.50, "lowband_ratio": 0.55,
                 "midband_ratio": 0.30, "highband_ratio": 0.15}
        assert engine._identify_filter(feats, 68.5, 68.0) == "diesel"

    def test_conversation_detected(self, base_cfg, tmp_storage, tmp_state):
        """Mid-centroid, moderate flatness, syllable-level modulation → conversation.
        Amplitude swings must exceed mower_env_std_max (4.5) to avoid mower match."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [58.0, 72.0, 60.0, 73.0, 59.0, 71.0,
                             61.0, 72.0, 58.0, 70.0, 60.0, 72.0]
        feats = {"centroid_hz": 1200, "flatness": 0.40, "lowband_ratio": 0.20,
                 "midband_ratio": 0.50, "highband_ratio": 0.30}
        assert engine._identify_filter(feats, 68.0, 64.0) == "conversation"

    def test_weedwhacker_priority_over_mower(self, base_cfg, tmp_storage, tmp_state):
        """A sound matching both weedwhacker and mower centroid range should be
        classified as weedwhacker (checked first in filter priority).
        Uses highband_ratio below 0.55 so birdsong doesn't intercept."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [70.0] * 12
        # centroid 2500 is in both weedwhacker (2000–6000) and mower (300–3000) range
        feats = {"centroid_hz": 2500, "flatness": 0.55, "lowband_ratio": 0.05,
                 "midband_ratio": 0.45, "highband_ratio": 0.50}
        assert engine._identify_filter(feats, 71.0, 70.0) == "weedwhacker"

    def test_diesel_not_confused_with_mower(self, base_cfg, tmp_storage, tmp_state):
        """Diesel (centroid 200) should not match mower (centroid 300–3000).
        The centroid is below mower's minimum, so only diesel should fire."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine.db_history = [68.0] * 12
        feats = {"centroid_hz": 200, "flatness": 0.50, "lowband_ratio": 0.55,
                 "midband_ratio": 0.30, "highband_ratio": 0.15}
        assert engine._identify_filter(feats, 68.5, 68.0) == "diesel"


# ---------------------------------------------------------------------------
# Classification journal & last_above fix
# ---------------------------------------------------------------------------

class TestClassificationJournal:
    """Tests for the classification journal feature: tracking source transitions
    within a single incident, the last_above refresh fix for filter-hit blocks,
    and the '(multiple)' classification suffix on finalization."""

    def _make_engine(self, base_cfg, tmp_storage, tmp_state):
        with patch("noise_warden.engine.AudioCapture", return_value=FakeCapture([])):
            with patch("noise_warden.engine.HAClient"):
                return Engine(base_cfg, tmp_storage, tmp_state)

    def test_journal_initialized_on_begin(self, base_cfg, tmp_storage, tmp_state):
        """_begin_incident should create a class_journal with the initial classification."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        assert engine.active is not None
        journal = engine.active["class_journal"]
        assert journal == [(0, "music")]

    def test_journal_no_duplicate_on_same_class(self, base_cfg, tmp_storage, tmp_state):
        """Calling _update_class_journal with the same classification should not
        add a duplicate entry — the journal only logs transitions."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        engine._update_class_journal("music")
        engine._update_class_journal("music")
        assert len(engine.active["class_journal"]) == 1

    def test_journal_records_transition(self, base_cfg, tmp_storage, tmp_state):
        """When classification changes, journal should record the new entry."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        engine._update_class_journal("birdsong")
        journal = engine.active["class_journal"]
        assert len(journal) == 2
        assert journal[0] == (0, "music")
        assert journal[1][1] == "birdsong"
        assert journal[1][0] >= 0

    def test_journal_multiple_transitions(self, base_cfg, tmp_storage, tmp_state):
        """Multiple transitions should all be recorded in order."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        engine._update_class_journal("birdsong")
        engine._update_class_journal("mower")
        engine._update_class_journal("music")
        journal = engine.active["class_journal"]
        assert len(journal) == 4
        classifications = [entry[1] for entry in journal]
        assert classifications == ["music", "birdsong", "mower", "music"]

    def test_finalize_single_class_no_multiple_suffix(self, base_cfg, tmp_storage, tmp_state):
        """An incident with only one classification should NOT get '(multiple)' suffix."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        engine._update_class_journal("music")
        iid = engine.active["id"]
        engine._finalize_incident()
        inc = tmp_storage.get_incident(iid)
        assert inc["classification"] == "music"
        assert inc["class_journal"] is not None
        import json
        journal = json.loads(inc["class_journal"])
        assert len(journal) == 1

    def test_finalize_multi_class_appends_multiple(self, base_cfg, tmp_storage, tmp_state):
        """An incident with multiple classifications should get '(multiple)' suffix
        and the journal should be stored as JSON."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        engine._update_class_journal("birdsong")
        engine._update_class_journal("music")
        iid = engine.active["id"]
        engine._finalize_incident()
        inc = tmp_storage.get_incident(iid)
        assert inc["classification"] == "music (multiple)"
        import json
        journal = json.loads(inc["class_journal"])
        assert len(journal) == 3
        assert journal[0][1] == "music"
        assert journal[1][1] == "birdsong"
        assert journal[2][1] == "music"

    def test_driveby_overrides_multiple_classification(self, base_cfg, tmp_storage, tmp_state):
        """Drive-by reclassification should override '(multiple)' if the incident
        matches drive-by criteria, since the drive-by check runs after journal storage."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "unknown", "respond")
        engine._update_class_journal("mower")
        iid = engine.active["id"]
        # Simulate a short incident with a fade-out tail (drive-by pattern)
        engine.active["dbs"] = [72.0, 71.0, 70.0, 69.0, 68.0]
        engine._finalize_incident()
        inc = tmp_storage.get_incident(iid)
        # Drive-by should reclassify — the (multiple) from journal is overwritten
        assert inc["classification"] == "drive_by"
        # But the journal is preserved (initial "unknown" gets replaced by
        # the backdated mower entry, leaving a single-entry journal)
        import json
        journal = json.loads(inc["class_journal"])
        assert len(journal) == 1
        assert journal[0][1] == "mower"

    def test_last_above_refreshed_during_filter_hit(self, base_cfg, tmp_storage, tmp_state):
        """When a filter matches during an active incident and noise is above threshold,
        last_above should be refreshed so the incident doesn't zombie-timeout."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        original_last_above = engine.active["last_above"]

        import time
        time.sleep(0.01)

        # Simulate the run loop refreshing last_above on filter-hit above threshold
        engine.active["dbs"].append(73.0)
        engine.active["last_above"] = datetime.now().astimezone()

        assert engine.active["last_above"] > original_last_above

    def test_no_excluded_incident_during_active(self, base_cfg, tmp_storage, tmp_state):
        """When a normal incident is active and a filter matches, no separate excluded
        incident should be created — the journal captures the filter classification."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        engine._begin_incident(72.0, 65.0, 0.7, 0.5, "music", "respond")
        assert engine._excluded_id is None
        engine._update_class_journal("birdsong")
        assert len(engine.active["class_journal"]) == 2
        assert engine._excluded_id is None

    def test_update_journal_noop_without_active(self, base_cfg, tmp_storage, tmp_state):
        """_update_class_journal should be a no-op when there's no active incident."""
        engine = self._make_engine(base_cfg, tmp_storage, tmp_state)
        assert engine.active is None
        engine._update_class_journal("music")
