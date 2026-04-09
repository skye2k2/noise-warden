"""
Tests for noise_warden.audio — AudioCapture continuous callback streaming.

These tests mock sounddevice to avoid hardware dependency. They verify:
- read_block() consumes blocks from the callback queue
- Pre-roll buffer accumulates blocks correctly
- Callback handles queue overflow gracefully
- Timeout raises RuntimeError on stalled devices
- reinitialize() drains stale callback queue data
- close() stops the InputStream gracefully
"""
import queue
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Patch sounddevice before importing AudioCapture so it doesn't try to init PortAudio
with patch("sounddevice.query_devices", return_value={"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}):
    from noise_warden.audio import AudioCapture


class TestAudioCapture:
    """Tests for the sd.InputStream callback-based capture pipeline."""

    @patch("noise_warden.audio.sd")
    def test_read_block_returns_array(self, mock_sd):
        """read_block() should consume from the queue and return a 1-D array."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_sd.InputStream.return_value = mock_stream

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        test_block = np.random.randn(22050, 1).astype(np.float32)
        cap._queue.put(test_block)

        result = cap.read_block()
        assert result.shape == (22050,)

    @patch("noise_warden.audio.sd")
    def test_preroll_buffer_fills(self, mock_sd):
        """Successive read_block() calls should fill the pre-roll buffer."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_sd.InputStream.return_value = mock_stream

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        for _ in range(5):
            cap._queue.put(np.zeros((22050, 1), dtype=np.float32))
            cap.read_block()
        assert len(cap.pre_blocks) == 5

    @patch("noise_warden.audio.sd")
    def test_get_preroll_returns_recent_blocks(self, mock_sd):
        """get_preroll(seconds) should return the most recent N blocks."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_sd.InputStream.return_value = mock_stream

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        for _ in range(10):
            cap._queue.put(np.zeros((22050, 1), dtype=np.float32))
            cap.read_block()
        pre = cap.get_preroll(2.0)  # 2 seconds = 2 blocks at 1.0s
        assert len(pre) == 2

    @patch("noise_warden.audio.sd")
    def test_callback_timeout_raises(self, mock_sd):
        """If no data arrives within timeout, should raise RuntimeError."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_sd.InputStream.return_value = mock_stream

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        cap._queue = queue.Queue()

        with pytest.raises(RuntimeError, match="No audio data"):
            cap.read_block()

    @patch("noise_warden.audio.sd")
    def test_audio_callback_populates_queue(self, mock_sd):
        """The _audio_callback method should push data into the queue."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        test_data = np.random.randn(22050, 1).astype(np.float32)
        cap._audio_callback(test_data, 22050, None, None)

        assert not cap._queue.empty()
        result = cap._queue.get_nowait()
        np.testing.assert_array_equal(result, test_data)

    @patch("noise_warden.audio.sd")
    def test_callback_queue_full_drops_oldest(self, mock_sd):
        """When the queue is full, callback should drop the oldest block to make room."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        # Use a tiny queue to make it easy to fill
        cap._queue = queue.Queue(maxsize=2)
        old_block = np.ones((22050, 1), dtype=np.float32)
        cap._queue.put(old_block)
        cap._queue.put(old_block)
        assert cap._queue.full()

        # Push a new block — should succeed by dropping the oldest
        new_block = np.full((22050, 1), 42.0, dtype=np.float32)
        cap._audio_callback(new_block, 22050, None, None)

        # Queue should still have 2 items, and the newest should be our 42.0 block
        assert cap._queue.qsize() == 2
        # Drain to find the new block
        blocks = []
        while not cap._queue.empty():
            blocks.append(cap._queue.get_nowait())
        assert any(np.allclose(b, 42.0) for b in blocks), "New block should be in queue"

    @patch("noise_warden.audio.sd")
    def test_reinitialize_drains_queue(self, mock_sd):
        """reinitialize() should empty the callback queue."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        # Push stale data
        for _ in range(5):
            cap._queue.put(np.zeros(100))
        assert not cap._queue.empty()

        cap.reinitialize()
        assert cap._queue.empty()
        assert len(cap.pre_blocks) == 0

    @patch("noise_warden.audio.sd")
    def test_close_stops_stream(self, mock_sd):
        """close() should stop and close any active InputStream."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_stream = MagicMock()
        mock_sd.InputStream.return_value = mock_stream

        cap = AudioCapture(sample_rate=22050, block_seconds=1.0)
        cap._stream = mock_stream
        cap.close()

        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()
        assert cap._stream is None
