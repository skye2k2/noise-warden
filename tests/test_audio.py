"""
Tests for noise_warden.audio — AudioCapture in both blocking and callback modes.

These tests mock sounddevice to avoid hardware dependency. They verify:
- The read_block() API surface is identical in both modes
- Callback mode pushes blocks through a queue correctly
- Pre-roll buffer works in both modes
- reinitialize() drains stale callback queue data
- close() stops the InputStream gracefully
"""
import queue
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Patch sounddevice before importing AudioCapture so it doesn't try to init PortAudio
with patch("sounddevice.query_devices", return_value={"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}):
    from noise_warden.audio import AudioCapture, _CALLBACK_STREAMS_ENABLED


# ---------------------------------------------------------------------------
# Blocking mode (default — _CALLBACK_STREAMS_ENABLED = False)
# ---------------------------------------------------------------------------

class TestBlockingMode:
    """Tests for the default blocking sd.rec() + sd.wait() path."""

    @patch("noise_warden.audio.sd")
    def test_read_block_returns_array(self, mock_sd):
        """read_block() should return a 1-D numpy array of the expected length."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        block = np.random.randn(11025).astype(np.float32)
        mock_sd.rec.return_value = block.reshape(-1, 1)
        mock_sd.wait.return_value = None

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
        result = cap.read_block()
        assert result.shape == (11025,)
        mock_sd.rec.assert_called_once()
        mock_sd.wait.assert_called_once()

    @patch("noise_warden.audio.sd")
    def test_preroll_buffer_fills(self, mock_sd):
        """Successive read_block() calls should fill the pre-roll buffer."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        block = np.zeros(11025, dtype=np.float32)
        mock_sd.rec.return_value = block.reshape(-1, 1)
        mock_sd.wait.return_value = None

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
        for _ in range(5):
            cap.read_block()
        assert len(cap.pre_blocks) == 5

    @patch("noise_warden.audio.sd")
    def test_get_preroll_returns_recent_blocks(self, mock_sd):
        """get_preroll(seconds) should return the most recent N blocks."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_sd.rec.return_value = np.zeros((11025, 1), dtype=np.float32)
        mock_sd.wait.return_value = None

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
        for _ in range(10):
            cap.read_block()
        pre = cap.get_preroll(2.0)  # 2 seconds = 4 blocks
        assert len(pre) == 4


# ---------------------------------------------------------------------------
# Callback mode (_CALLBACK_STREAMS_ENABLED = True)
# ---------------------------------------------------------------------------

class TestCallbackMode:
    """Tests for the sd.InputStream callback path."""

    @patch("noise_warden.audio._CALLBACK_STREAMS_ENABLED", True)
    @patch("noise_warden.audio.sd")
    def test_read_block_from_queue(self, mock_sd):
        """In callback mode, read_block() should consume from the queue."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_sd.InputStream.return_value = mock_stream

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
        # Manually push a block into the queue (simulating callback)
        test_block = np.random.randn(11025, 1).astype(np.float32)
        cap._queue.put(test_block)

        result = cap.read_block()
        assert result.shape == (11025,)

    @patch("noise_warden.audio._CALLBACK_STREAMS_ENABLED", True)
    @patch("noise_warden.audio.sd")
    def test_callback_timeout_raises(self, mock_sd):
        """If no data arrives within timeout, should raise PortAudioError."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}
        mock_sd.PortAudioError = type("PortAudioError", (Exception,), {})
        mock_stream = MagicMock()
        mock_stream.active = True
        mock_sd.InputStream.return_value = mock_stream

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
        # Empty queue, short timeout — should raise
        cap._queue = queue.Queue()

        with pytest.raises(Exception, match="No audio data"):
            cap.read_block()

    @patch("noise_warden.audio._CALLBACK_STREAMS_ENABLED", True)
    @patch("noise_warden.audio.sd")
    def test_audio_callback_populates_queue(self, mock_sd):
        """The _audio_callback method should push data into the queue."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
        test_data = np.random.randn(11025, 1).astype(np.float32)
        cap._audio_callback(test_data, 11025, None, None)

        assert not cap._queue.empty()
        result = cap._queue.get_nowait()
        np.testing.assert_array_equal(result, test_data)

    @patch("noise_warden.audio.sd")
    def test_reinitialize_drains_queue(self, mock_sd):
        """reinitialize() should empty the callback queue."""
        mock_sd.query_devices.return_value = {"name": "FakeMic", "max_input_channels": 1, "default_samplerate": 22050}

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
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

        cap = AudioCapture(sample_rate=22050, block_seconds=0.5)
        cap._stream = mock_stream
        cap.close()

        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()
        assert cap._stream is None
