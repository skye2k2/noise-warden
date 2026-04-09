from __future__ import annotations
import queue
import sounddevice as sd
import numpy as np
from collections import deque

# ---------------------------------------------------------------------------
# CONTINUOUS CALLBACK STREAMING
#
# Uses sd.InputStream with a callback that pushes audio blocks into a
# thread-safe queue. The audio hardware captures continuously — no samples
# are lost between blocks regardless of how long the engine takes to process.
#
# An earlier version used blocking mode (sd.rec() + sd.wait()), which
# created a gap between blocks: after sd.wait() returned, the engine ran
# DSP analysis + WAV I/O + threshold checks before starting the next
# sd.rec() call. During that processing window, all microphone data was
# lost. This produced audible clicks at every 1-second block boundary —
# phase discontinuities confirmed by sine-wave analysis
# (17/23 boundaries showed discontinuity ratios up to 22x typical
# sample-to-sample change). Blocking mode has been removed entirely;
# there is no valid use case for intentionally losing audio samples.
# ---------------------------------------------------------------------------


class AudioCapture:
    """Captures audio from a single input device via continuous callback streaming.

    Uses sd.InputStream with a callback that pushes blocks into a thread-safe
    queue. The engine consumes blocks at its own pace via read_block(). Audio
    capture never pauses, so zero samples are lost between blocks regardless
    of engine processing time."""

    def __init__(self, sample_rate=22050, block_seconds=1.0, channels=1, device=None):
        self.sr = sample_rate
        self.block_seconds = block_seconds
        self.frames = int(sample_rate * block_seconds)
        self.channels = channels
        self.device = device
        self.pre_blocks = deque(maxlen=64)
        # Capture a fingerprint of the initial device for drift detection
        self._device_fingerprint = self._get_device_fingerprint()

        # Callback-mode resources (initialized lazily on first read_block)
        self._stream = None
        self._queue = queue.Queue(maxsize=128)

    def _get_device_fingerprint(self):
        """Return a string identifying the current input device (name + channels + sample rate).
        Used to detect when a microphone is swapped or the system default changes."""
        try:
            info = sd.query_devices(self.device, kind="input")
            return f"{info['name']}|{info['max_input_channels']}|{info['default_samplerate']}"
        except (sd.PortAudioError, ValueError):
            return None

    def validate_device(self):
        """Check that the current audio input device matches the one present at startup.
        Returns (ok, message) tuple. A mismatch likely means the mic was replaced or
        unplugged and a different device picked up as system default."""
        current = self._get_device_fingerprint()
        if self._device_fingerprint is None:
            # No baseline — can't validate, but capture a new one if available
            if current:
                self._device_fingerprint = current
            return (True, "no baseline")

        if current is None:
            return (False, "audio device not found")

        if current != self._device_fingerprint:
            return (False, f"device changed: was [{self._device_fingerprint}], now [{current}]")

        return (True, "ok")

    def reinitialize(self):
        """Re-query the audio subsystem after a device error. Clears pre-roll buffer
        and updates the device fingerprint. Call this after a transient USB disconnect."""
        self._stop_stream()
        self.pre_blocks.clear()
        # Drain any stale blocks from the callback queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._device_fingerprint = self._get_device_fingerprint()

    # --- Stream management ---

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice from a separate audio thread. Copies the block
        into the queue for consumption by read_block() on the engine thread."""
        if status:
            print(f"[audio] Stream callback status: {status}")
        # indata is a numpy array of shape (frames, channels) — copy to avoid
        # referencing the transient buffer that sounddevice reuses.
        try:
            self._queue.put(indata.copy(), block=False)
        except queue.Full:
            # Engine is >128 seconds behind — drop oldest block to make room.
            # This should never happen in practice, but better to lose one old
            # block than to silently drop the current one.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put(indata.copy(), block=False)

    def _ensure_stream(self):
        """Start the InputStream if it is not already running."""
        if self._stream is not None and self._stream.active:
            return
        self._stream = sd.InputStream(
            samplerate=self.sr,
            blocksize=self.frames,
            channels=self.channels,
            dtype="float32",
            device=self.device,
            callback=self._audio_callback,
        )
        self._stream.start()

    def _stop_stream(self):
        """Stop and close the InputStream if active."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def read_block(self):
        """Return a single audio block as a 1-D numpy array.

        Blocks arrive asynchronously from the InputStream callback and are
        consumed from the queue. Appended to the pre-roll buffer for
        snippet pre-trigger capture."""
        self._ensure_stream()
        try:
            data = self._queue.get(timeout=2.0)
        except queue.Empty:
            # No data arrived — possible device disconnect or stall
            raise RuntimeError("No audio data received within timeout")
        arr = np.squeeze(data)

        self.pre_blocks.append(arr.copy())
        return arr

    def get_preroll(self, seconds: float):
        """Return the most recent N seconds of pre-trigger audio blocks."""
        blocks = int(seconds / self.block_seconds)
        return list(self.pre_blocks)[-blocks:]

    def close(self):
        """Release audio resources. Call during shutdown."""
        self._stop_stream()
