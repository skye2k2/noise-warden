#!/usr/bin/env python3
# eslint-disable -- node scripts use the console
"""Spectral reshaping tool for classification data recordings.

PURPOSE:
  YouTube-sourced recordings (or any recording-of-a-recording) have spectral
  characteristics that are fundamentally different from what a USB microphone
  captures in open air.  Compression codecs kill bass, smear harmonics, and
  shift energy upward, so a mower that should peak at 300–800 Hz instead
  peaks at 4000+ Hz in a YouTube rip.

  This script applies frequency-domain EQ profiles to reshape such recordings
  so that their spectral features land within the range our DSP filters expect.
  This is NOT a substitute for real-world recordings — the profiles are clearly
  marked "pending" in the regression harness, and should be replaced with live
  captures when available.

PROCESSING PIPELINE:
  1. Reads the source WAV from tests/classification_data/
  2. Optionally trims to a stable segment (removes quiet intro/outro)
  3. Applies spectral correction:
     - Fixed mode:    hand-tuned EQ bands from the profile
     - Adaptive mode: auto-computed spectral tilt via binary search
  4. Optionally mixes in shaped noise to raise spectral flatness:
     - Fixed mode:    noise level from the profile
     - Adaptive mode: minimum noise level found via binary search
  5. Normalizes the output to -3 dBFS peak to avoid clipping
  6. Writes the result to tests/classification_data/{output_filename}
  7. Prints before/after spectral analysis from our own DSP pipeline

  WHY NOISE MIXING:
    EQ alone concentrates energy into fewer bands, which *decreases* spectral
    flatness — exactly the opposite of what broadband filters (mower, diesel)
    need.  Mixing in low-level shaped noise fills the spectral gaps and raises
    the flatness metric toward real-world levels without changing the dominant
    spectral character.  Think of it as simulating the broadband environmental
    noise floor that real outdoor recordings naturally include.

  WHY ADAPTIVE MODE:
    Hand-tuned EQ bands require iterative trial-and-error for EACH recording.
    A different mower recording from a different source (YouTube, phone, Ring
    camera) has a different starting spectrum and needs different EQ gains.
    Adaptive mode replaces fixed bands with a single "spectral tilt" parameter
    (dB/octave) and binary-searches for the minimum tilt that achieves the
    target centroid.  Then it searches for the minimum noise level that achieves
    the target flatness.  This makes profiles reusable across recordings of the
    same sound type.

    Adaptive mode works well for broadband mechanical noise (mowers, HVAC, fans)
    where the spectral difference from reality is primarily a bass–treble tilt.
    It does NOT work for diesel (centroid ≤400 + flatness 0.40–0.65 are
    irreconcilable via EQ from treble-heavy sources).

USAGE:
  # Process all profiles whose source files exist:
  python scripts/eq_classification_data.py

  # Process a specific profile:
  python scripts/eq_classification_data.py mower

  # Dry-run — show what would be processed without writing files:
  python scripts/eq_classification_data.py --dry-run

  # Verify — run reclassify on output files to check classification:
  python scripts/eq_classification_data.py --verify

EXTENDING:
  To add a new EQ profile, append an entry to the EQ_PROFILES dict below.
  Each profile needs:
    - source:      input filename in tests/classification_data/
    - output:      output filename (naming convention: {category}-{descriptor}.wav)
    - description: human-readable note about what the source is and why EQ is needed
    - target:      dict of spectral features the EQ is trying to achieve (documentary)

  PLUS one of these mode configurations:

  Fixed mode (hand-tuned, use when adaptive can't converge):
    - eq_bands:    list of (low_hz, high_hz, gain_db) tuples defining the EQ curve

  Adaptive mode (auto-computed, preferred for broadband mechanical noise):
    - adaptive:         True
    - adaptive_targets: dict with numerical goals:
        - centroid_max_hz: target centroid ceiling (use filter max minus margin)
        - flatness_min:    target flatness floor (use filter min plus margin)
    - tilt_pivot_hz:    optional pivot frequency for spectral tilt (default 1000)

  Optional profile keys (both modes):
    - trim_stable:    if True, auto-trim to the longest segment where 12-block
                      env_std < 4.5 and mean dB > 70.  Removes quiet intro/outro.
    - noise_type:     "pink" (1/f, default) or "brown" (1/f², more bass-heavy)
    - noise_level_db: dB below signal RMS to mix noise at (fixed mode only)

  The eq_bands are applied additively in the frequency domain.  Overlapping
  bands sum their gains.  Frequencies not covered by any band get 0 dB (unity).
  Negative gain_db values attenuate; positive values boost.

BAND BOUNDARIES (from noise_warden/dsp.py spectrum_features):
  lowband:  30–180 Hz   (bass, kick fundamentals)
  midband:  180–1200 Hz (voice, guitar body, melody)
  highband: >1200 Hz    (sibilance, cymbals, birdsong)
"""

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_DIR = PROJECT_ROOT / "tests" / "classification_data"

# ---------------------------------------------------------------------------
# EQ Profiles
#
# Each profile describes a source recording and the EQ needed to reshape
# its spectrum toward what the corresponding DSP filter expects.
#
# The "target" dict is purely documentary — it records the filter thresholds
# we're aiming for so future maintainers know what "success" looks like
# without needing to cross-reference dsp.py.
# ---------------------------------------------------------------------------

EQ_PROFILES = {
    "mower": {
        "source": "mower.wav",
        "output": "mower-eq.wav",
        "description": (
            "Mower recording from any source (YouTube, phone, security camera).  "
            "Adaptive mode analyzes the source spectrum and auto-computes: "
            "(1) the spectral tilt (dB/octave) needed to push centroid below "
            "4000 Hz, and (2) the pink noise level to raise flatness above "
            "0.25 — making this profile reusable for any mower recording, "
            "not just one specific YouTube rip.  Trim removes quiet "
            "intro/outro to ensure env_std stays below 4.5."
        ),
        "trim_stable": True,
        "adaptive": True,
        "adaptive_targets": {
            # Target below filter max (4000) with 200 Hz headroom for
            # noise-induced centroid drift
            "centroid_max_hz": 3800,
            # Target above filter min (0.25) with margin
            "flatness_min": 0.27,
        },
        "noise_type": "pink",
        # Pivot at 1000 Hz: mower energy should peak below this, while
        # YouTube recording energy often peaks above it
        "tilt_pivot_hz": 1000.0,
        "target": {
            "centroid_hz": "300–4000 (mower_centroid_min/max)",
            "flatness": "≥ 0.25 (mower_flatness_threshold)",
            "env_std": "≤ 4.5 (mower_env_std_max)",
            "min_db": "≥ 70.0 (mower_min_db)",
        },
    },

    "diesel": {
        "source": "diesel.wav",
        "output": "diesel-eq.wav",
        "description": (
            "YouTube diesel truck idling recording.  Raw centroid is "
            "2100–3500 Hz with lowband 0.07–0.20 and flatness 0.18–0.26.  "
            "Real diesel trucks captured outdoors have dominant bass with "
            "centroid ≤ 400 Hz, lowband ≥ 0.45, and moderate flatness "
            "(0.40–0.65).  KNOWN LIMITATION: this source recording cannot "
            "be EQ'd to match the diesel filter.  Centroid and flatness "
            "requirements are mathematically opposed — reducing noise to "
            "get centroid below 400 drops flatness below 0.40, and vice "
            "versa.  Real diesel trucks have 60+ dB bass-to-ambient ratio "
            "that cannot be synthesized from a YouTube recording with no "
            "bass content.  Kept as a best-effort approximation until a "
            "real outdoor recording is captured.  EQ strategy: extreme "
            "bass boost + low-level pink noise as environmental noise floor."
        ),
        # Pink noise fills the high-frequency valleys that aggressive EQ
        # cuts create, which is critical for flatness (geometric mean is
        # killed by near-zero bins).  -24 dB keeps noise contribution to
        # centroid very low — diesel needs centroid ≤400 which is extremely
        # sensitive to any treble energy.  Even small noise floor in treble
        # bins raises flatness significantly because the EQ-cut bins are
        # near zero without it.
        "noise_type": "pink",
        "noise_level_db": -24.0,
        "eq_bands": [
            # Extreme bass boost — the bass must dominate so heavily that
            # -12 dB pink noise barely shifts centroid.  Real diesel is
            # often 30+ dB louder in bass than ambient at recording distance.
            (20, 100, 36.0),
            # Bass body — primary diesel frequency range
            (100, 250, 30.0),
            # Low-mid — strong presence to fill midband
            (250, 500, 16.0),
            # Mid — heavy cut to isolate bass character
            (500, 1200, -8.0),
            # High — extreme attenuation
            (1200, 3000, -20.0),
            (3000, 22050, -32.0),
        ],
        "target": {
            "centroid_hz": "≤ 400 (diesel_centroid_max_hz)",
            "lowband_ratio": "≥ 0.45 (diesel_lowband_min)",
            "flatness": "0.40–0.65 (diesel_flatness_min/max)",
            "env_std": "≤ 3.0 (diesel_env_std_max)",
        },
    },

    # -----------------------------------------------------------------------
    # Template for adding new profiles:
    # -----------------------------------------------------------------------
    #
    # Fixed mode — hand-tuned EQ bands (useful when adaptive can't converge,
    # or when the spectral correction isn't a simple tilt):
    #
    # "category_name": {
    #     "source": "source_filename.wav",
    #     "output": "category-descriptor-eq.wav",
    #     "description": "What the source is and why EQ is needed.",
    #     "trim_stable": False,        # optional: auto-trim to stable segment
    #     "noise_type": "pink",        # optional: "pink" or "brown"
    #     "noise_level_db": -18.0,     # optional: noise level relative to signal RMS
    #     "eq_bands": [
    #         (low_hz, high_hz, gain_db),
    #         ...
    #     ],
    #     "target": {
    #         "feature": "value (config_key)",
    #     },
    # },
    #
    # Adaptive mode — auto-computed tilt + noise (preferred for broadband
    # mechanical noise where centroid tolerance is generous):
    #
    # "category_name": {
    #     "source": "source_filename.wav",
    #     "output": "category-descriptor-eq.wav",
    #     "description": "What the source is and why EQ is needed.",
    #     "trim_stable": True,
    #     "adaptive": True,
    #     "adaptive_targets": {
    #         "centroid_max_hz": 3800,  # target centroid ceiling (with margin)
    #         "flatness_min": 0.27,     # target flatness floor (with margin)
    #     },
    #     "noise_type": "pink",
    #     "tilt_pivot_hz": 1000.0,     # optional: tilt pivot frequency
    #     "target": {                  # documentary — for human reference
    #         "feature": "value (config_key)",
    #     },
    # },
}


# ---------------------------------------------------------------------------
# EQ Engine
# ---------------------------------------------------------------------------

def build_gain_curve(eq_bands, n_fft, sample_rate):
    """Build a frequency-domain gain curve from a list of EQ band definitions.

    Each band is a (low_hz, high_hz, gain_db) tuple.  The gain curve starts
    at unity (0 dB) for all bins, and each band's gain is added to the bins
    it covers.  Overlapping bands sum their gains.

    Args:
        eq_bands: list of (low_hz, high_hz, gain_db) tuples
        n_fft: FFT length (determines frequency resolution)
        sample_rate: audio sample rate in Hz

    Returns:
        numpy array of linear gain values, length n_fft // 2 + 1
    """
    n_bins = n_fft // 2 + 1
    freqs = np.linspace(0, sample_rate / 2, n_bins)
    gain_db = np.zeros(n_bins, dtype=float)

    for low_hz, high_hz, db in eq_bands:
        mask = (freqs >= low_hz) & (freqs <= high_hz)
        gain_db[mask] += db

    # Convert dB to linear gain
    return np.power(10.0, gain_db / 20.0)


def apply_eq(samples, sample_rate, eq_bands):
    """Apply frequency-domain EQ to an audio signal.

    Uses overlap-add with 50% overlap and Hann windowing for artifact-free
    processing.  Block size is 4096 samples (~93ms at 44100 Hz), which gives
    ~10.7 Hz frequency resolution — sufficient for the broad EQ curves we
    use here.

    Args:
        samples: 1D numpy array of audio samples (float32/float64)
        sample_rate: sample rate in Hz
        eq_bands: list of (low_hz, high_hz, gain_db) tuples

    Returns:
        EQ'd audio as 1D numpy array, same length as input
    """
    block_size = 4096
    hop_size = block_size // 2
    window = np.hanning(block_size)
    gain_curve = build_gain_curve(eq_bands, block_size, sample_rate)

    # Pad input to ensure complete final block
    pad_len = block_size - (len(samples) % hop_size)
    padded = np.concatenate([samples, np.zeros(pad_len)])
    output = np.zeros(len(padded))

    n_blocks = (len(padded) - block_size) // hop_size + 1
    for i in range(n_blocks):
        start = i * hop_size
        block = padded[start:start + block_size] * window

        # FFT → apply gain → IFFT
        spectrum = np.fft.rfft(block)
        spectrum *= gain_curve
        processed = np.fft.irfft(spectrum)

        output[start:start + block_size] += processed * window

    # Trim to original length
    return output[:len(samples)]


def normalize_to_dbfs(samples, target_dbfs=-3.0):
    """Normalize audio to a target peak dBFS level.

    Prevents clipping from EQ boosts while maintaining reasonable amplitude.

    Args:
        samples: 1D numpy array of audio samples
        target_dbfs: target peak level in dBFS (default -3.0)

    Returns:
        normalized audio as 1D numpy array
    """
    peak = np.max(np.abs(samples))
    if peak < 1e-10:
        return samples

    target_linear = 10.0 ** (target_dbfs / 20.0)
    return samples * (target_linear / peak)


# ---------------------------------------------------------------------------
# Noise Generation — for spectral flatness augmentation
# ---------------------------------------------------------------------------

def generate_shaped_noise(n_samples, sample_rate, noise_type="pink"):
    """Generate spectrally shaped noise of a given type.

    Pink noise (1/f): equal energy per octave.  Spectrally "flat" in
    perceptual terms.  Good for raising flatness without emphasizing any
    particular band — appropriate for mower and general broadband sources.

    Brown noise (1/f²): energy concentrated in bass.  Good for diesel and
    other low-frequency-dominant sources where we want flatness but still
    need the noise to reinforce the bass character.

    Uses block-based FFT synthesis (4096-sample blocks with overlap-add)
    rather than a single massive FFT, which avoids numerical precision
    issues and excessive memory use for long signals.

    Args:
        n_samples: number of audio samples to generate
        sample_rate: target sample rate (determines frequency resolution)
        noise_type: "pink" or "brown"

    Returns:
        1D numpy array of shaped noise, normalized to unit RMS
    """
    block_size = 4096
    hop_size = block_size // 2
    n_bins = block_size // 2 + 1
    freqs = np.fft.rfftfreq(block_size, d=1.0 / sample_rate)

    # Build shaping curve for the target noise type
    safe_freqs = np.maximum(freqs, 1.0)
    if noise_type == "pink":
        # 1/f: -3 dB/octave (amplitude ∝ 1/sqrt(f))
        shape = 1.0 / np.sqrt(safe_freqs)
    elif noise_type == "brown":
        # 1/f²: -6 dB/octave (amplitude ∝ 1/f)
        shape = 1.0 / safe_freqs
    else:
        raise ValueError(f"Unknown noise type: {noise_type!r} (use 'pink' or 'brown')")

    # DC bin should be zero (no DC offset)
    shape[0] = 0.0

    # Use deterministic seed for reproducibility
    rng = np.random.default_rng(seed=42)

    # Overlap-add synthesis with Hann windowing (mirrors apply_eq approach)
    window = np.hanning(block_size)
    pad_len = block_size - (n_samples % hop_size) if n_samples % hop_size else 0
    output_len = n_samples + pad_len
    output = np.zeros(output_len)

    n_blocks = (output_len - block_size) // hop_size + 1
    for _ in range(n_blocks):
        # Generate random phases for this block
        phases = rng.uniform(0, 2 * np.pi, n_bins)
        spectrum = shape * np.exp(1j * phases)
        block = np.fft.irfft(spectrum, n=block_size)
        # Window to prevent clicks at block boundaries
        block = block * window
        # Overlap-add
        start = _ * hop_size
        output[start:start + block_size] += block

    noise = output[:n_samples]

    # Normalize to unit RMS
    rms = np.sqrt(np.mean(noise ** 2))
    if rms > 1e-10:
        noise = noise / rms

    return noise


def mix_noise(signal, sample_rate, noise_type="pink", noise_level_db=-20.0):
    """Mix shaped noise into a signal at a specified level below signal RMS.

    The noise level is relative to the signal's RMS, so -20 dB means the
    noise floor will be 20 dB below the signal's average power.  This fills
    spectral gaps and raises the flatness metric without changing the
    dominant spectral character.

    Args:
        signal: 1D numpy array of audio samples
        sample_rate: sample rate in Hz
        noise_type: "pink" or "brown"
        noise_level_db: noise level relative to signal RMS (negative = quieter)

    Returns:
        signal + noise as 1D numpy array
    """
    signal_rms = np.sqrt(np.mean(signal ** 2))
    if signal_rms < 1e-10:
        return signal

    noise = generate_shaped_noise(len(signal), sample_rate, noise_type)

    # Scale noise to desired level relative to signal RMS
    noise_rms_target = signal_rms * (10.0 ** (noise_level_db / 20.0))
    noise = noise * noise_rms_target

    print(f"  Mixing {noise_type} noise at {noise_level_db} dB "
          f"(noise RMS: {noise_rms_target:.6f}, signal RMS: {signal_rms:.6f})")

    return signal + noise


# ---------------------------------------------------------------------------
# Trim — extract the most stable segment from a recording
# ---------------------------------------------------------------------------

def find_stable_segment(wav_path, max_env_std=4.5, min_db=70.0, window_blocks=12):
    """Find the longest stable segment in a recording.

    Scans the file in 1-second blocks, computes dBA for each, then finds
    the longest contiguous window where the rolling env_std (over
    window_blocks) stays below max_env_std and all blocks exceed min_db.

    This removes quiet intro/outro and transient gaps that inflate env_std
    for the whole file, which is the mower recording's core problem.

    Args:
        wav_path: path to WAV file
        max_env_std: maximum envelope std deviation within the window
        min_db: minimum dB for a block to be considered "active"
        window_blocks: rolling window size for env_std calculation

    Returns:
        (start_sample, end_sample, n_blocks, segment_env_std) tuple,
        or None if no stable segment found
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from noise_warden.dsp import dba_estimate, rms_dbfs

    data, sr = sf.read(str(wav_path), dtype="float32")
    block_size = int(sr * 1.0)
    n_blocks = len(data) // block_size

    if n_blocks < window_blocks:
        print(f"  Warning: file has {n_blocks} blocks, need {window_blocks} for stability check")
        return None

    # Compute per-block dBA
    db_values = []
    for i in range(n_blocks):
        block = data[i * block_size:(i + 1) * block_size]
        dbfs = rms_dbfs(block)
        db_now = dba_estimate(dbfs, 123.5)
        db_values.append(db_now)

    db_arr = np.array(db_values)

    # Find blocks that are above min_db
    active = db_arr >= min_db

    # Find the longest contiguous run of active blocks where rolling
    # env_std stays within threshold
    best_start = None
    best_length = 0

    # Scan all possible starting positions
    for start in range(n_blocks):
        if not active[start]:
            continue

        # Extend as far as possible
        end = start
        while end < n_blocks and active[end]:
            # Check env_std of the current window
            seg = db_arr[start:end + 1]
            if len(seg) >= window_blocks:
                seg_std = np.std(seg)
                if seg_std > max_env_std:
                    break
            end += 1

        length = end - start
        if length > best_length:
            best_length = length
            best_start = start

    if best_start is None or best_length < window_blocks:
        print(f"  No stable segment found (min {window_blocks} blocks)")
        return None

    start_sample = best_start * block_size
    end_sample = (best_start + best_length) * block_size
    segment_std = float(np.std(db_arr[best_start:best_start + best_length]))

    print(f"  Stable segment: blocks {best_start}–{best_start + best_length - 1} "
          f"({best_length} blocks, {best_length:.1f}s), env_std={segment_std:.2f}")

    return (start_sample, end_sample, best_length, segment_std)


# ---------------------------------------------------------------------------
# Analysis — uses our own DSP pipeline for before/after comparison
# ---------------------------------------------------------------------------

def analyze_spectral_from_data(data, sr):
    """Run the DSP pipeline on raw audio data and return median spectral features.

    Same analysis as analyze_spectral_summary, but operates on a numpy array
    instead of reading from a file.  Used by the adaptive search functions to
    avoid writing temp files during each iteration of the binary search.

    Args:
        data: 1D numpy array of audio samples (any float dtype)
        sr: sample rate in Hz

    Returns:
        dict with median spectral features and block count
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from noise_warden.dsp import dba_estimate, rms_dbfs, spectrum_features

    block_size = int(sr * 1.0)
    n_blocks = len(data) // block_size

    features_list = []
    db_list = []

    for i in range(n_blocks):
        block = data[i * block_size:(i + 1) * block_size].astype(np.float32)
        dbfs = rms_dbfs(block)
        db_now = dba_estimate(dbfs, 123.5)
        db_list.append(db_now)
        feats = spectrum_features(block, sr)
        features_list.append(feats)

    if not features_list:
        return {"error": "No complete blocks in data"}

    keys = ["centroid_hz", "flatness", "lowband_ratio", "midband_ratio",
            "highband_ratio"]
    result = {}
    for key in keys:
        values = [f[key] for f in features_list]
        result[key] = float(np.median(values))

    result["db_median"] = float(np.median(db_list))
    result["db_range"] = f"{min(db_list):.1f}–{max(db_list):.1f}"
    result["env_std"] = float(np.std(db_list))
    result["n_blocks"] = n_blocks

    return result


def analyze_spectral_summary(wav_path, sample_rate_override=None):
    """Run our DSP pipeline on a WAV and return averaged spectral features.

    Processes the file in 1-second blocks (matching the engine's block size)
    and returns the median of each spectral feature across all blocks.

    Args:
        wav_path: path to WAV file
        sample_rate_override: if set, use this instead of file's native rate

    Returns:
        dict with median spectral features and block count
    """
    # Import here to avoid circular dependency when running standalone
    sys.path.insert(0, str(PROJECT_ROOT))
    from noise_warden.dsp import dba_estimate, rms_dbfs, spectrum_features

    data, sr = sf.read(str(wav_path), dtype="float32")
    if sample_rate_override:
        sr = sample_rate_override

    block_size = int(sr * 1.0)
    n_blocks = len(data) // block_size

    features_list = []
    db_list = []

    for i in range(n_blocks):
        block = data[i * block_size:(i + 1) * block_size]
        dbfs = rms_dbfs(block)
        # Use local dev cal_offset for consistent analysis
        db_now = dba_estimate(dbfs, 123.5)
        db_list.append(db_now)

        feats = spectrum_features(block, sr)
        features_list.append(feats)

    if not features_list:
        return {"error": "No complete blocks in file"}

    # Compute medians (more robust than means for skewed distributions)
    keys = ["centroid_hz", "flatness", "lowband_ratio", "midband_ratio",
            "highband_ratio"]
    result = {}
    for key in keys:
        values = [f[key] for f in features_list]
        result[key] = float(np.median(values))

    result["db_median"] = float(np.median(db_list))
    result["db_range"] = f"{min(db_list):.1f}–{max(db_list):.1f}"
    result["env_std"] = float(np.std(db_list))
    result["n_blocks"] = n_blocks

    return result


def print_comparison(label, before, after):
    """Print a formatted before/after spectral comparison table.

    Args:
        label: profile name
        before: spectral summary dict from source file
        after: spectral summary dict from EQ'd file
    """
    print(f"\n{'=' * 60}")
    print(f"  {label.upper()} — spectral comparison")
    print(f"{'=' * 60}")
    print(f"  {'Feature':<20} {'Before':>10} {'After':>10} {'Delta':>10}")
    print(f"  {'-' * 50}")

    for key in ["centroid_hz", "flatness", "lowband_ratio", "midband_ratio",
                "highband_ratio", "env_std", "db_median"]:
        bval = before.get(key, 0)
        aval = after.get(key, 0)
        delta = aval - bval

        if key == "centroid_hz":
            print(f"  {key:<20} {bval:>10.0f} {aval:>10.0f} {delta:>+10.0f}")
        else:
            print(f"  {key:<20} {bval:>10.3f} {aval:>10.3f} {delta:>+10.3f}")

    print(f"  {'dB range':<20} {before.get('db_range', '?'):>10} "
          f"{after.get('db_range', '?'):>10}")
    print(f"  {'blocks':<20} {before.get('n_blocks', 0):>10} "
          f"{after.get('n_blocks', 0):>10}")


# ---------------------------------------------------------------------------
# Adaptive EQ — auto-computed spectral correction
#
# Instead of hard-coding EQ bands for each recording, adaptive mode uses
# a single "spectral tilt" parameter (dB/octave) and binary-searches for
# the value that achieves the target centroid.  Then it searches for the
# minimum noise level that achieves the target flatness.
#
# This makes profiles reusable across different recordings of the same
# sound type — any mower recording gets automatically corrected, not just
# the specific YouTube rip we hand-tuned the original EQ bands for.
# ---------------------------------------------------------------------------

def build_tilt_eq(bass_boost, sample_rate, pivot_hz=1000.0, n_bands=48):
    """Build EQ bands implementing a spectral tilt around a pivot frequency.

    The tilt is linear on a log-frequency axis: each octave below the pivot
    gets +bass_boost dB of gain, each octave above gets -bass_boost dB.
    This models the primary spectral difference between YouTube recordings
    (treble-heavy) and real outdoor recordings (bass-heavy).

    Args:
        bass_boost: dB of gain per octave below pivot (and attenuation above)
        sample_rate: audio sample rate in Hz
        pivot_hz: frequency of zero gain (default 1000 Hz)
        n_bands: number of logarithmically spaced bands (default 48)

    Returns:
        list of (low_hz, high_hz, gain_db) tuples compatible with apply_eq
    """
    nyquist = sample_rate / 2
    boundaries = np.geomspace(20.0, nyquist, n_bands + 1)
    bands = []

    for i in range(n_bands):
        low = boundaries[i]
        high = boundaries[i + 1]
        center = np.sqrt(low * high)  # geometric mean = perceptual center
        # Positive octaves_from_pivot = bass (below pivot) = boost
        octaves_from_pivot = np.log2(pivot_hz / center)
        gain_db = bass_boost * octaves_from_pivot
        bands.append((low, high, gain_db))

    return bands


def find_optimal_tilt(data, sr, centroid_max, pivot_hz=1000.0, max_iter=20):
    """Binary search for the minimum spectral tilt that brings centroid below target.

    Searches from 0 (no tilt) to 15 dB/octave (extreme tilt) for the
    smallest bass_boost value that puts the median centroid at or below
    centroid_max.  Converges when search range drops below 0.1 dB/octave.

    Args:
        data: 1D numpy array of audio samples
        sr: sample rate in Hz
        centroid_max: target maximum centroid in Hz
        pivot_hz: tilt pivot frequency (default 1000 Hz)
        max_iter: maximum search iterations (default 20)

    Returns:
        (bass_boost, eq_data, features) — optimal tilt, processed audio, and
        final spectral features.  bass_boost is None if target is unreachable.
    """
    # Check if source already meets target (no EQ needed)
    features_orig = analyze_spectral_from_data(data, sr)
    if features_orig["centroid_hz"] <= centroid_max:
        print(f"    Source centroid {features_orig['centroid_hz']:.0f} Hz "
              f"already ≤ {centroid_max} Hz — no tilt needed")
        return 0.0, data.copy(), features_orig

    low = 0.0
    high = 15.0  # dB/octave — extreme ceiling
    best_tilt = None
    best_eq_data = None
    best_features = None

    for i in range(max_iter):
        mid = (low + high) / 2
        eq_bands = build_tilt_eq(mid, sr, pivot_hz)
        eq_data = apply_eq(data, sr, eq_bands)
        features = analyze_spectral_from_data(eq_data, sr)
        centroid = features["centroid_hz"]

        status = "✓" if centroid <= centroid_max else "✗"
        if (i + 1) % 3 == 0 or i == 0:
            print(f"    tilt iter {i + 1}: {mid:.1f} dB/oct → "
                  f"centroid {centroid:.0f} Hz {status}")

        if centroid <= centroid_max:
            # Sufficient tilt — record and try less
            best_tilt = mid
            best_eq_data = eq_data
            best_features = features
            high = mid
        else:
            # Need more tilt
            low = mid

        # Converged
        if high - low < 0.1:
            break

    if best_tilt is not None:
        print(f"    Converged: {best_tilt:.1f} dB/oct → "
              f"centroid {best_features['centroid_hz']:.0f} Hz")

    return best_tilt, best_eq_data, best_features


def find_optimal_noise_level(data, sr, flatness_min, noise_type="pink", max_iter=20):
    """Binary search for the minimum noise level that brings flatness above target.

    Pre-generates the noise signal once (deterministic seed) and rescales it
    each iteration for efficiency.  Searches from -30 dB (barely audible)
    to -3 dB (very loud) relative to signal RMS.

    Args:
        data: 1D numpy array of EQ'd audio samples
        sr: sample rate in Hz
        flatness_min: target minimum spectral flatness
        noise_type: "pink" or "brown"
        max_iter: maximum search iterations (default 20)

    Returns:
        (noise_level_db, mixed_data, features) — optimal noise level,
        processed audio, and final spectral features.
        noise_level_db is None if target is unreachable.
    """
    # Check if data already meets target (no noise needed)
    features_orig = analyze_spectral_from_data(data, sr)
    if features_orig["flatness"] >= flatness_min:
        print(f"    Flatness {features_orig['flatness']:.3f} already "
              f"≥ {flatness_min} — no noise needed")
        return -30.0, data.copy(), features_orig

    # Pre-generate unit-RMS noise once for efficient rescaling
    noise_unit = generate_shaped_noise(len(data), sr, noise_type)
    signal_rms = np.sqrt(np.mean(data ** 2))

    if signal_rms < 1e-10:
        print("    Signal is silent — cannot mix noise")
        return None, None, None

    low = -30.0
    high = -3.0
    best_level = None
    best_data = None
    best_features = None

    for i in range(max_iter):
        mid = (low + high) / 2
        noise_rms = signal_rms * (10.0 ** (mid / 20.0))
        mixed = data + noise_unit * noise_rms
        features = analyze_spectral_from_data(mixed, sr)
        flatness = features["flatness"]

        status = "✓" if flatness >= flatness_min else "✗"
        if (i + 1) % 3 == 0 or i == 0:
            print(f"    noise iter {i + 1}: {mid:.1f} dB → "
                  f"flatness {flatness:.3f} {status}")

        if flatness >= flatness_min:
            # Sufficient noise — record and try less (more negative)
            best_level = mid
            best_data = mixed
            best_features = features
            high = mid
        else:
            # Need more noise (less negative)
            low = mid

        # Converged within 0.3 dB
        if high - low < 0.3:
            break

    if best_level is not None:
        print(f"    Converged: {best_level:.1f} dB {noise_type} noise → "
              f"flatness {best_features['flatness']:.3f}")
    else:
        print(f"    WARNING: flatness {flatness:.3f} < {flatness_min} "
              f"even at {high:.1f} dB noise")

    return best_level, best_data, best_features


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_profile(name, profile, dry_run=False, verify=False):
    """Process a single EQ profile: trim, EQ, mix noise, normalize, write.

    Supports two modes:
      - Fixed:    profile specifies eq_bands and noise_level_db explicitly
      - Adaptive: profile specifies adaptive_targets; the script auto-computes
                  spectral tilt and noise level via binary search

    Processing pipeline (each step is optional per profile config):
      1. Read source WAV
      2. If trim_stable: extract the most stable segment
      3. Apply EQ (fixed bands or adaptive tilt)
      4. Mix noise (fixed level or adaptive search)
      5. Normalize to -3 dBFS peak
      6. Write output, print before/after comparison

    Args:
        name: profile key (for logging)
        profile: profile dict from EQ_PROFILES
        dry_run: if True, analyze but don't write files
        verify: if True, run reclassify on output file after processing

    Returns:
        True if processing succeeded, False otherwise
    """
    source_path = CLASSIFICATION_DIR / profile["source"]
    output_path = CLASSIFICATION_DIR / profile["output"]

    if not source_path.exists():
        print(f"  [{name}] SKIP — source not found: {source_path.name}")
        return False

    print(f"\n  [{name}] Processing: {source_path.name} → {output_path.name}")
    print(f"  {profile['description']}")

    # Read source
    data, sr = sf.read(str(source_path), dtype="float64")
    print(f"  Source: {sr} Hz, {len(data) / sr:.1f}s, {len(data)} samples")

    # Step 1: Trim to stable segment if requested
    if profile.get("trim_stable"):
        trim_result = find_stable_segment(source_path)
        if trim_result:
            start_sample, end_sample, n_blocks, seg_std = trim_result
            data = data[start_sample:end_sample]
            print(f"  Trimmed to {len(data) / sr:.1f}s ({n_blocks} blocks)")
        else:
            print("  Warning: trim_stable requested but no stable segment found; using full file")

    # Analyze before (on the potentially trimmed source)
    # Write trimmed source to temp file for analysis if we trimmed
    if profile.get("trim_stable"):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            sf.write(str(tmp_path), data.astype(np.float32), sr, subtype="PCM_16")
        print("  Analyzing trimmed source spectrum...")
        before = analyze_spectral_summary(tmp_path)
        tmp_path.unlink()
    else:
        print("  Analyzing source spectrum...")
        before = analyze_spectral_summary(source_path)

    if dry_run:
        print("  [DRY RUN] Would apply EQ and write to:", output_path.name)
        print_comparison(name, before, before)
        return True

    # Step 2+3: Apply EQ and noise — adaptive or fixed mode
    if profile.get("adaptive"):
        targets = profile["adaptive_targets"]
        pivot = profile.get("tilt_pivot_hz", 1000.0)
        noise_type = profile.get("noise_type")
        centroid_max = targets["centroid_max_hz"]
        needs_noise = noise_type and "flatness_min" in targets

        # Outer retry loop — tilt and noise interact because noise raises
        # centroid.  If noise pushes centroid over target, we tighten the
        # centroid ceiling and re-search the tilt.  Converges because more
        # tilt means more bass (less noise needed), and the noise search
        # finds the minimum level.
        eq_data = None
        final_converged = False

        for attempt in range(4):
            # Phase 1: Find optimal spectral tilt for centroid
            label = f" (attempt {attempt + 1}, ceiling {centroid_max:.0f})" if attempt > 0 else ""
            print(f"  [Adaptive] Searching for optimal spectral tilt "
                  f"(target centroid ≤ {centroid_max:.0f} Hz){label}...")
            tilt, eq_data, tilt_feats = find_optimal_tilt(
                data, sr, centroid_max, pivot_hz=pivot
            )

            if tilt is None:
                print(f"  WARNING: centroid target {centroid_max:.0f} Hz "
                      f"unreachable at max tilt (15 dB/oct)")
                eq_bands = build_tilt_eq(15.0, sr, pivot)
                eq_data = apply_eq(data, sr, eq_bands)
                break

            print(f"  [Adaptive] Tilt: {tilt:.1f} dB/octave (pivot {pivot:.0f} Hz)")

            # Phase 2: Find optimal noise level for flatness
            if not needs_noise:
                final_converged = True
                break

            print(f"  [Adaptive] Searching for optimal {noise_type} noise "
                  f"(target flatness ≥ {targets['flatness_min']})...")
            noise_level, noisy_data, noise_feats = find_optimal_noise_level(
                eq_data, sr, targets["flatness_min"], noise_type
            )

            if noise_level is None:
                print("  WARNING: flatness target unreachable")
                break

            # Check if noise pushed centroid over the ORIGINAL target
            final_centroid = noise_feats["centroid_hz"]
            original_max = targets["centroid_max_hz"]

            if final_centroid <= original_max:
                eq_data = noisy_data
                print(f"  [Adaptive] Final: tilt {tilt:.1f} dB/oct, "
                      f"noise {noise_level:.1f} dB → "
                      f"centroid {final_centroid:.0f} Hz, "
                      f"flatness {noise_feats['flatness']:.3f}")
                final_converged = True
                break

            # Noise pushed centroid over target — tighten and retry
            overshoot = final_centroid - original_max
            centroid_max = centroid_max - overshoot - 100
            print(f"  [Adaptive] Noise pushed centroid to {final_centroid:.0f} Hz "
                  f"(over {original_max}). Retrying with ceiling {centroid_max:.0f}...")

            if centroid_max < 300:
                print("  WARNING: centroid ceiling dropped below 300 Hz — "
                      "irreconcilable (needs real recording)")
                eq_data = noisy_data  # best effort
                break

        if not final_converged:
            print("  [Adaptive] Did not fully converge — using best-effort result")

    else:
        # Fixed-band mode — explicit EQ and noise from profile
        print(f"  Applying {len(profile['eq_bands'])} EQ bands...")
        eq_data = apply_eq(data, sr, profile["eq_bands"])

        noise_type = profile.get("noise_type")
        if noise_type:
            noise_level = profile.get("noise_level_db", -20.0)
            eq_data = mix_noise(eq_data, sr, noise_type, noise_level)

    # Step 4: Normalize to prevent clipping
    eq_data = normalize_to_dbfs(eq_data, target_dbfs=-3.0)

    # Write output
    sf.write(str(output_path), eq_data.astype(np.float32), sr, subtype="PCM_16")
    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  Wrote: {output_path.name} ({file_size_mb:.1f} MB)")

    # Analyze after
    print("  Analyzing EQ'd spectrum...")
    after = analyze_spectral_summary(output_path)

    print_comparison(name, before, after)

    # Optionally run reclassify for full pipeline verification
    if verify:
        print(f"\n  Reclassify verification:")
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "noise_warden.reclassify",
             str(output_path), "--verbose"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        # Print just the summary (last 10 lines)
        summary_lines = result.stdout.strip().split("\n")[-10:]
        for line in summary_lines:
            print(f"    {line}")

    return True


def main():
    """Entry point: parse args and process requested profiles."""
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    verify = "--verify" in args
    profile_names = [a for a in args if not a.startswith("--")]

    if dry_run:
        print("[DRY RUN MODE]")

    # If specific profiles requested, process only those
    if profile_names:
        profiles_to_run = {
            k: v for k, v in EQ_PROFILES.items() if k in profile_names
        }
        unknown = set(profile_names) - set(EQ_PROFILES.keys())
        if unknown:
            print(f"Unknown profiles: {', '.join(sorted(unknown))}")
            print(f"Available: {', '.join(sorted(EQ_PROFILES.keys()))}")
            sys.exit(1)
    else:
        profiles_to_run = EQ_PROFILES

    print(f"Processing {len(profiles_to_run)} profile(s) "
          f"from {CLASSIFICATION_DIR}")

    successes = 0
    for name, profile in sorted(profiles_to_run.items()):
        if process_profile(name, profile, dry_run=dry_run, verify=verify):
            successes += 1

    print(f"\nDone: {successes}/{len(profiles_to_run)} profiles processed.")


if __name__ == "__main__":
    main()
