from __future__ import annotations
import numpy as np

# ---------------------------------------------------------------------------
# DUAL-MICROPHONE PROCESSING PLUGINS — STUBS WITH DOCUMENTED ALGORITHMS
#
# These processors are designed for a dual-microphone deployment where:
#   - Primary mic: pointed at the noise source (neighbor's property)
#   - Reference mic: pointed at our own speaker/amp (captures self-noise)
#
# Both require _CALLBACK_STREAMS_ENABLED = True in audio.py so that two
# AudioCapture instances can run concurrently on separate USB devices.
#
# Neither plugin is wired into the engine loop yet. When ready:
#   1. Enable callback streams in audio.py
#   2. Create a second AudioCapture for the reference device
#   3. Call plugin.process(primary_block, reference_block) before dB calculation
#   4. Use the cleaned output for threshold comparison and WAV recording
# ---------------------------------------------------------------------------


class ReferenceSubtractor:
    """Adaptive noise cancellation using Normalized Least Mean Squares (NLMS).

    Estimates the transfer function from the reference mic (our speaker output)
    to the primary mic (what it picks up of our own playback), then subtracts
    that estimate from the primary signal. The result should be predominantly
    the *neighbor's* noise with our self-noise removed.

    This is the same principle used in echo cancellation (AEC) in conferencing
    systems, adapted for a physical speaker-to-mic acoustic path rather than
    a line-level loopback.

    STRONGLY RECOMMENDED: Use two separate USB audio interfaces (not a single
    stereo device) to avoid clock synchronization issues and crosstalk.

    Algorithm: NLMS (Normalized LMS)
    - Filter length: ~256 taps at 22050 Hz ≈ 11.6 ms impulse response
    - Step size (mu): 0.1 (conservative — prevents divergence in reverberant rooms)
    - Regularization (eps): 1e-8 (prevents division by zero on silence)"""

    def __init__(self, filter_length: int = 256, mu: float = 0.1):
        self.filter_length = filter_length
        self.mu = mu
        self._weights = np.zeros(filter_length, dtype=np.float32)

    def process(self, primary_block, reference_block=None):
        """Subtract estimated self-noise from the primary mic signal.

        If no reference_block is provided (single-mic mode), returns primary
        unmodified — the plugin is a transparent pass-through."""
        if reference_block is None or len(reference_block) == 0:
            return primary_block

        # NLMS adaptive filter — operates sample-by-sample for convergence,
        # but on 0.5-second blocks (11025 samples at 22050 Hz) the cost is
        # acceptable since it runs once per block, not per sample in real-time.
        output = np.copy(primary_block)
        ref = np.array(reference_block, dtype=np.float32)
        n = len(primary_block)
        fl = self.filter_length
        eps = 1e-8

        for i in range(fl, n):
            ref_segment = ref[i - fl:i][::-1]
            estimate = np.dot(self._weights, ref_segment)
            error = primary_block[i] - estimate
            output[i] = error
            norm = np.dot(ref_segment, ref_segment) + eps
            self._weights += (self.mu / norm) * error * ref_segment

        return output


class DualMicDifferential:
    """Spectral subtraction for directional noise rejection.

    Compares the frequency spectrum of the secondary mic to the primary mic.
    Frequencies that are louder on the secondary mic (our side) than the primary
    mic (neighbor's side) are attenuated, since they likely originate from our
    own speaker system.

    Simpler than NLMS but less adaptive — works well for stationary noise
    (constant music playback) but poorly for transient sounds (speech, impacts).

    Use this as a complementary check alongside ReferenceSubtractor, not
    as a replacement. The two approaches have different failure modes."""

    def __init__(self, alpha: float = 1.0):
        # alpha controls how aggressively to subtract secondary spectrum.
        # 1.0 = full subtraction; >1.0 = over-subtraction (more aggressive);
        # <1.0 = partial subtraction (preserves more of our own noise).
        self.alpha = alpha

    def process(self, primary_block, secondary_block=None):
        """Attenuate frequencies in the primary signal that are dominant in
        the secondary signal.

        If no secondary_block is provided (single-mic mode), returns primary
        unmodified — the plugin is a transparent pass-through."""
        if secondary_block is None or len(secondary_block) == 0:
            return primary_block

        # FFT both signals
        primary_fft = np.fft.rfft(primary_block)
        secondary_fft = np.fft.rfft(secondary_block)

        primary_mag = np.abs(primary_fft)
        secondary_mag = np.abs(secondary_fft)
        primary_phase = np.angle(primary_fft)

        # Spectral subtraction: reduce primary magnitude where secondary is strong
        cleaned_mag = primary_mag - self.alpha * secondary_mag
        # Floor at zero to avoid negative magnitudes (which cause phase inversion artifacts)
        cleaned_mag = np.maximum(cleaned_mag, 0.0)

        # Reconstruct with original phase
        cleaned_fft = cleaned_mag * np.exp(1j * primary_phase)
        return np.real(np.fft.irfft(cleaned_fft, n=len(primary_block))).astype(np.float32)
