"""Rotating stdout tee for durable engine logs (NEXT.md 3b).

The engine logs via print(), which the logging module's handlers never see,
so durable capture wraps sys.stdout instead: every write goes to the original
stream (journald) AND to a size-rotated file under shared/. Journald alone
proved insufficient — retention expired and the journal corrupted during the
original memory-starved outage, destroying the diagnostic history exactly when
it was needed.
"""
import os


class TeeStdout:
    """File-like wrapper that mirrors writes to a console stream and a
    size-rotated log file.

    Rotation is the classic numbered scheme: when the file exceeds max_bytes
    it is renamed to <path>.1, existing backups shift up, and anything past
    `backups` is pruned. All file-side failures are swallowed — a logging
    problem must never take down the engine, and the console (journald) side
    always receives the write.
    """

    def __init__(self, console, log_path, max_bytes=5 * 1024 * 1024, backups=3):
        self._console = console
        self._log_path = log_path
        self._max_bytes = max(1, int(max_bytes))
        self._backups = max(1, int(backups))

    def write(self, text):
        self._console.write(text)
        try:
            self._maybe_rotate()
            with open(self._log_path, "a") as fh:
                fh.write(text)
        except OSError:
            pass  # Console still got the line; file side degrades silently.
        return len(text)

    def flush(self):
        try:
            self._console.flush()
        except (OSError, ValueError):
            pass

    def isatty(self):
        """uvicorn probes this to decide on ANSI coloring."""
        return False

    def fileno(self):
        """Delegate so libraries that redirect at the fd level still work."""
        return self._console.fileno()

    def _maybe_rotate(self):
        try:
            size = os.path.getsize(self._log_path)
        except OSError:
            return
        if size < self._max_bytes:
            return
        # Shift backups upward: .2 -> .3, .1 -> .2, current -> .1
        prune = f"{self._log_path}.{self._backups}"
        if os.path.exists(prune):
            os.remove(prune)
        for i in range(self._backups - 1, 0, -1):
            src = f"{self._log_path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{self._log_path}.{i + 1}")
        os.replace(self._log_path, f"{self._log_path}.1")
