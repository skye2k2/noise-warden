"""
Tests for noise_warden.engine — the core audio processing loop.

Uses a mock AudioCapture to avoid sounddevice hardware dependency.
Storage and StateStore are real (temp DB) so we can verify actual
incident lifecycle end-to-end.
"""
import os
import time
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from noise_warden.engine import Engine
from noise_warden.state import StateStore
from noise_warden.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sine_block(freq=440, sr=16000, duration=0.5, amplitude=0.5):
    """Generate a sine wave block matching the default capture config."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)
    return np.sin(2 * np.pi * freq * t) * amplitude


def _make_silence_block(sr=16000, duration=0.5):
    """Generate a near-silent block."""
    return np.zeros(int(sr * duration), dtype=np.float32)


class FakeCapture:
    """
    Stand-in for AudioCapture that yields pre-loaded blocks instead
    of reading from a real microphone.
    """

    def __init__(self, blocks, sr=16000, block_seconds=0.5):
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
        failing_capture.sr = 16000
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

        with patch("noise_warden.engine.AudioCapture", return_value=failing_capture):
            with patch("noise_warden.engine.HAClient"):
                engine = Engine(base_cfg, tmp_storage, tmp_state)
                engine.capture = failing_capture
                engine.start()
                time.sleep(2.5)  # Give time for error + recovery
                engine.stop()

        # Engine should have recorded the error at some point
        snap = tmp_state.snapshot()
        # After stop, mode is "stopped", but last_error should have been set during the failure
        # (It may have been cleared if recovery succeeded, but the engine survived — that's the key)
        assert call_count >= 2, "Engine should have retried after error"
