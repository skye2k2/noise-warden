"""
Tests for noise_warden.response — RelayController and PlaylistPlayer.

PlaylistPlayer tests use a temp directory with fake audio files and mock
subprocess.Popen so no actual player process runs.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from noise_warden.response import PlaylistPlayer, RelayController


# ---------------------------------------------------------------------------
# RelayController (boolean stub — tests are trivial but prevent regressions)
# ---------------------------------------------------------------------------

class TestRelayController:

    def test_starts_disabled(self):
        relay = RelayController(gpio_pin=18)
        assert relay.enabled is False

    def test_on_enables(self):
        relay = RelayController(gpio_pin=18)
        relay.on()
        assert relay.enabled is True

    def test_off_disables(self):
        relay = RelayController(gpio_pin=18)
        relay.on()
        relay.off()
        assert relay.enabled is False

    def test_stores_pin(self):
        relay = RelayController(gpio_pin=23)
        assert relay.gpio_pin == 23


# ---------------------------------------------------------------------------
# PlaylistPlayer
# ---------------------------------------------------------------------------

class TestPlaylistPlayer:

    @pytest.fixture
    def playlist_dir(self, tmp_path):
        """Create a temp playlist directory with a few fake audio files."""
        pdir = tmp_path / "playlist"
        pdir.mkdir()
        for name in ["track1.mp3", "track2.wav", "track3.ogg", "readme.txt"]:
            (pdir / name).write_text("fake")
        return str(pdir)

    @pytest.fixture
    def empty_playlist_dir(self, tmp_path):
        """A playlist directory with no audio files."""
        pdir = tmp_path / "empty_playlist"
        pdir.mkdir()
        (pdir / "notes.txt").write_text("not audio")
        return str(pdir)

    def test_pick_file_returns_audio_file(self, playlist_dir):
        player = PlaylistPlayer("/usr/bin/cvlc --play-and-exit", playlist_dir)
        picked = player._pick_file()
        assert picked is not None
        # Should be one of the audio files, not readme.txt
        assert picked.endswith((".mp3", ".wav", ".ogg", ".flac", ".m4a"))

    def test_pick_file_empty_dir_returns_none(self, empty_playlist_dir):
        player = PlaylistPlayer("/usr/bin/cvlc --play-and-exit", empty_playlist_dir)
        assert player._pick_file() is None

    def test_pick_file_nonexistent_dir_returns_none(self, tmp_path):
        player = PlaylistPlayer("/usr/bin/cvlc", str(tmp_path / "no_such_dir"))
        assert player._pick_file() is None

    @patch("noise_warden.response.subprocess.Popen")
    def test_start_launches_process_with_file(self, mock_popen, playlist_dir):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        player = PlaylistPlayer("/usr/bin/cvlc --play-and-exit --no-video", playlist_dir)
        player.start()

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        # Should be: ['/usr/bin/cvlc', '--play-and-exit', '--no-video', '<track path>']
        assert args[0] == "/usr/bin/cvlc"
        assert args[-1].endswith((".mp3", ".wav", ".ogg"))

    @patch("noise_warden.response.subprocess.Popen")
    def test_start_does_nothing_if_no_files(self, mock_popen, empty_playlist_dir):
        player = PlaylistPlayer("/usr/bin/cvlc --play-and-exit", empty_playlist_dir)
        player.start()
        mock_popen.assert_not_called()

    @patch("noise_warden.response.subprocess.Popen")
    def test_start_skips_if_already_running(self, mock_popen, playlist_dir):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        mock_popen.return_value = mock_proc

        player = PlaylistPlayer("/usr/bin/cvlc --play-and-exit", playlist_dir)
        player.start()
        player.start()  # Second call should be a no-op

        assert mock_popen.call_count == 1

    def test_stop_on_no_process_is_safe(self, playlist_dir):
        """Calling stop() when no process has started should not error."""
        player = PlaylistPlayer("/usr/bin/cvlc", playlist_dir)
        player.stop()  # Should not raise

    @patch("noise_warden.response.subprocess.Popen")
    def test_stop_terminates_running_process(self, mock_popen, playlist_dir):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        mock_popen.return_value = mock_proc

        player = PlaylistPlayer("/usr/bin/cvlc --play-and-exit", playlist_dir)
        player.start()
        player.stop()

        mock_proc.terminate.assert_called_once()
