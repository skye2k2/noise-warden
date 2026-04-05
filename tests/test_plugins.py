"""
Tests for noise_warden.plugins — ReferenceSubtractor and DualMicDifferential.

These tests verify the signal processing correctness of the dual-mic plugins
without requiring any audio hardware. Both plugins should be transparent
pass-throughs when no reference/secondary signal is provided (single-mic mode).
"""
import numpy as np
import pytest

from noise_warden.plugins import DualMicDifferential, ReferenceSubtractor


# ---------------------------------------------------------------------------
# ReferenceSubtractor (NLMS adaptive filter)
# ---------------------------------------------------------------------------

class TestReferenceSubtractor:

    def test_passthrough_without_reference(self):
        """With no reference signal, primary should pass through unmodified."""
        sub = ReferenceSubtractor()
        primary = np.random.randn(1024).astype(np.float32)
        result = sub.process(primary, reference_block=None)
        np.testing.assert_array_equal(result, primary)

    def test_passthrough_with_empty_reference(self):
        """An empty reference array should also pass through."""
        sub = ReferenceSubtractor()
        primary = np.random.randn(1024).astype(np.float32)
        result = sub.process(primary, reference_block=np.array([]))
        np.testing.assert_array_equal(result, primary)

    def test_reduces_correlated_noise(self):
        """When the primary mic picks up our speaker output (reference signal)
        mixed with neighbor noise, the NLMS filter should reduce the speaker
        component after adapting over several blocks.

        Simulates a realistic scenario: the reference is a 200 Hz tone (our
        speaker), and the primary mic hears that same tone (attenuated and
        slightly delayed by the room) plus an independent 800 Hz neighbor tone."""
        sub = ReferenceSubtractor(filter_length=128, mu=0.3)
        sr = 22050
        block_dur = 0.5
        block_len = int(sr * block_dur)

        # Build coherent signals that persist across blocks
        t_per_block = np.arange(block_len, dtype=np.float32) / sr

        for block_idx in range(10):
            t = t_per_block + block_idx * block_dur
            # Our speaker output (reference mic hears this directly)
            reference = (np.sin(2 * np.pi * 200 * t) * 0.5).astype(np.float32)
            # Primary mic hears a scaled-down version (room attenuation) plus
            # the neighbor's independent 800 Hz signal
            echo_in_primary = reference * 0.6
            neighbor = (np.sin(2 * np.pi * 800 * t) * 0.3).astype(np.float32)
            primary = echo_in_primary + neighbor

            result = sub.process(primary, reference)

        # After 10 blocks of adaptation on a coherent signal, check the last
        # result's 200 Hz component is reduced relative to the unfiltered primary
        result_fft = np.abs(np.fft.rfft(result))
        primary_fft = np.abs(np.fft.rfft(primary))
        freqs = np.fft.rfftfreq(block_len, 1.0 / sr)
        idx_200 = np.argmin(np.abs(freqs - 200))

        assert result_fft[idx_200] < primary_fft[idx_200] * 0.7, (
            f"NLMS should reduce the 200 Hz echo component. "
            f"Primary 200Hz: {primary_fft[idx_200]:.2f}, "
            f"Filtered 200Hz: {result_fft[idx_200]:.2f}"
        )

    def test_output_shape_matches_input(self):
        """Output should have the same shape as the primary input."""
        sub = ReferenceSubtractor()
        primary = np.random.randn(1024).astype(np.float32)
        reference = np.random.randn(1024).astype(np.float32)
        result = sub.process(primary, reference)
        assert result.shape == primary.shape


# ---------------------------------------------------------------------------
# DualMicDifferential (spectral subtraction)
# ---------------------------------------------------------------------------

class TestDualMicDifferential:

    def test_passthrough_without_secondary(self):
        """With no secondary signal, primary should pass through unmodified."""
        diff = DualMicDifferential()
        primary = np.random.randn(1024).astype(np.float32)
        result = diff.process(primary, secondary_block=None)
        np.testing.assert_array_equal(result, primary)

    def test_passthrough_with_empty_secondary(self):
        """An empty secondary array should also pass through."""
        diff = DualMicDifferential()
        primary = np.random.randn(1024).astype(np.float32)
        result = diff.process(primary, secondary_block=np.array([]))
        np.testing.assert_array_equal(result, primary)

    def test_reduces_shared_frequencies(self):
        """When both signals share a tone, the output should have less of it."""
        diff = DualMicDifferential(alpha=1.0)
        sr = 22050
        t = np.linspace(0, 0.5, int(sr * 0.5), dtype=np.float32)
        # Shared 440 Hz tone (our speaker output)
        shared_tone = np.sin(2 * np.pi * 440 * t) * 0.5
        # Neighbor has an additional 1000 Hz tone
        neighbor_only = np.sin(2 * np.pi * 1000 * t) * 0.3
        primary = shared_tone + neighbor_only
        secondary = shared_tone  # Reference mic hears only our output

        result = diff.process(primary, secondary)

        # The 440 Hz component should be attenuated in the result
        result_fft = np.abs(np.fft.rfft(result))
        primary_fft = np.abs(np.fft.rfft(primary))
        freqs = np.fft.rfftfreq(len(t), 1.0 / sr)

        # Find the bin closest to 440 Hz
        idx_440 = np.argmin(np.abs(freqs - 440))
        assert result_fft[idx_440] < primary_fft[idx_440] * 0.5, \
            "Shared 440 Hz tone should be significantly attenuated"

    def test_output_shape_matches_input(self):
        """Output should have the same shape as the primary input."""
        diff = DualMicDifferential()
        primary = np.random.randn(1024).astype(np.float32)
        secondary = np.random.randn(1024).astype(np.float32)
        result = diff.process(primary, secondary)
        assert result.shape == primary.shape

    def test_output_is_float32(self):
        """Spectral subtraction should preserve float32 dtype."""
        diff = DualMicDifferential()
        primary = np.random.randn(1024).astype(np.float32)
        secondary = np.random.randn(1024).astype(np.float32)
        result = diff.process(primary, secondary)
        assert result.dtype == np.float32
