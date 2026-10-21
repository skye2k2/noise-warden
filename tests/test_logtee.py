"""
Tests for noise_warden.logtee — the rotating stdout tee (NEXT.md 3b).

The engine logs via print(), which the logging module's handlers never see,
so durable log capture needs a stdout tee with its own size-based rotation.
Journald alone proved insufficient: retention expired / the journal corrupted
during the original memory-starved outage, destroying diagnostic history.
"""
import io
import os

from noise_warden.logtee import TeeStdout


class TestTeeStdout:

    def test_writes_reach_console_and_file(self, tmp_path):
        console = io.StringIO()
        log_path = str(tmp_path / "noise_warden.log")
        tee = TeeStdout(console, log_path, max_bytes=10_000, backups=2)
        tee.write("hello engine\n")
        tee.flush()
        assert "hello engine" in console.getvalue()
        assert "hello engine" in open(log_path).read()

    def test_rotates_at_max_bytes(self, tmp_path):
        console = io.StringIO()
        log_path = str(tmp_path / "noise_warden.log")
        tee = TeeStdout(console, log_path, max_bytes=500, backups=2)
        for i in range(100):
            tee.write(f"line {i:04d} padding padding padding\n")
        tee.flush()
        assert os.path.exists(log_path + ".1"), "Rotation should have produced a .1 backup"
        assert os.path.getsize(log_path) <= 600, "Current log should restart after rotation"
        # Console got every line regardless of rotation
        assert "line 0099" in console.getvalue()

    def test_backup_count_is_bounded(self, tmp_path):
        console = io.StringIO()
        log_path = str(tmp_path / "noise_warden.log")
        tee = TeeStdout(console, log_path, max_bytes=200, backups=2)
        for i in range(400):
            tee.write(f"line {i:04d} padding padding padding\n")
        tee.flush()
        assert os.path.exists(log_path + ".1")
        assert os.path.exists(log_path + ".2")
        assert not os.path.exists(log_path + ".3"), "Backups beyond the limit must be pruned"

    def test_file_failure_never_breaks_console(self, tmp_path):
        """Logging must degrade to console-only if the file becomes unwritable —
        a logging failure must never take down the engine (the whole point is
        diagnosing bad days, which is when disks fill up)."""
        console = io.StringIO()
        log_dir = tmp_path / "gone"
        log_dir.mkdir()
        log_path = str(log_dir / "noise_warden.log")
        tee = TeeStdout(console, log_path, max_bytes=500, backups=1)
        tee.write("before\n")
        # Remove the directory out from under the tee
        os.remove(log_path)
        log_dir.rmdir()
        tee.write("after\n")
        tee.flush()
        assert "after" in console.getvalue()
