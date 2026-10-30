"""Pure signal-processing functions for noise classification.

Pipeline overview (called per audio block in the engine's main loop):
  1. rms_dbfs        → raw amplitude in dBFS
  2. dba_estimate    → calibrated dBA via mic offset
  3. spectrum_features → band-energy ratios, spectral centroid, flatness
  4. music_like_score → composite heuristic combining bass energy + tonality
  5. Exclusion filters (impulse, thunder, rain, mower, birdsong)

All thresholds are either function parameters (configurable via YAML) or
documented inline with rationale. "Magic numbers" in this file fall into
three categories:

  Epsilon values (1e-12)
    Prevent log(0) and division-by-zero. The value is small enough to be
    inaudible (below any mic's noise floor) but large enough to avoid
    floating-point denormals.

  Band-split boundaries (30 / 180 / 1200 Hz)
    Chosen to roughly separate sub-bass rumble from voiced fundamentals
    (bass/midrange) from overtone/sibilance content. These are coarse
    splits — good enough for classification, not meant for mastering.
    - 30 Hz: below most music fundamentals; cuts DC and infrasonic rumble
    - 180 Hz: upper limit of bass guitar/kick drum fundamentals
    - 1200 Hz: above most speech fundamentals; separates melody from treble

  Autocorrelation / scoring constants
    Documented per-function below. In general, these were set empirically
    by running the engine against known-music and known-noise recordings,
    then adjusted to minimize false positives against mower/traffic/rain.
"""
from __future__ import annotations
import numpy as np

def rms_dbfs(arr: np.ndarray) -> float:
    """Root-mean-square amplitude in dBFS (decibels relative to full scale).

    The 1e-12 epsilon is added to both the RMS and the log argument to prevent
    log(0) on silence. 1e-12 is ~-240 dBFS — well below any real signal.
    """
    rms = float(np.sqrt(np.mean(np.square(arr))) + 1e-12)
    return 20.0 * np.log10(rms + 1e-12)

def dba_estimate(dbfs: float, calibration_offset_db: float) -> float:
    """Convert dBFS to approximate dBA by adding the mic calibration offset.

    The offset is determined during the calibration wizard by comparing the
    mic's dBFS reading against a reference SPL meter. Typical USB mics on a
    Pi yield offsets in the 80-95 dB range.
    """
    return dbfs + calibration_offset_db

def resample_audio(data, orig_sr, target_sr):
    """Resample audio to a target sample rate using FFT (sinc interpolation).

    Normalizes audio to a canonical sample rate before spectral feature
    extraction, ensuring that centroid, flatness, and band-ratio thresholds
    remain consistent regardless of the source file's native rate.

    This is the same algorithm as scipy.signal.resample (Fourier method):
    FFT → truncate or zero-pad frequency domain → IFFT. For integer-ratio
    conversions (e.g. 44100→22050, a 2:1 downsample), this is mathematically
    exact. For arbitrary ratios, quality is still excellent for classification
    features — we're not targeting audiophile playback.

    Downsampling naturally anti-aliases by discarding frequency bins above
    the new Nyquist. No separate low-pass filter is needed.

    Args:
        data: 1-D numpy float32 array of audio samples.
        orig_sr: source sample rate in Hz.
        target_sr: desired sample rate in Hz.

    Returns:
        Resampled 1-D numpy array. Returns the original array unchanged
        if orig_sr == target_sr.
    """
    if orig_sr == target_sr:
        return data

    target_len = int(round(len(data) * target_sr / orig_sr))
    freq = np.fft.rfft(data)
    target_freq_len = target_len // 2 + 1

    if target_freq_len <= len(freq):
        # Downsampling: truncate high-frequency bins above new Nyquist
        new_freq = freq[:target_freq_len]
    else:
        # Upsampling: zero-pad (adds silent high-frequency bins)
        new_freq = np.zeros(target_freq_len, dtype=freq.dtype)
        new_freq[:len(freq)] = freq

    result = np.fft.irfft(new_freq, n=target_len)

    # Scale amplitude to compensate for the change in FFT length.
    # Without this, downsampled signals would be quieter and upsampled
    # signals louder, skewing dB calculations.
    result *= target_len / len(data)

    return result.astype(np.float32)


def _compute_harmonic_ratio(low_mag, low_freqs):
    """Compute fraction of low-band energy concentrated at harmonic peaks.

    Finds the strongest frequency in the low band, then checks how much of
    the total low-band energy is at that fundamental and its integer
    multiples (harmonics). A tolerance of ±5 Hz around each harmonic
    accounts for FFT bin quantization.

    Music bass (e.g., kick drum at 60 Hz) shows clear peaks at 60, 120, 180 Hz.
    Engine rumble distributes energy broadly without a dominant harmonic series.

    Returns a ratio in [0, 1]: higher = more harmonic structure.
    """
    # Find the fundamental (strongest bin in the low band)
    peak_idx = np.argmax(low_mag)
    fundamental = low_freqs[peak_idx]

    # Need a meaningful fundamental above DC hum
    if fundamental < 35:
        return 0.0

    # Gather energy at fundamental + overtones within 30–180 Hz
    total_energy = float(np.sum(low_mag))
    harmonic_energy = 0.0
    tolerance_hz = 5.0

    harmonic = fundamental
    while harmonic <= 180:
        mask = np.abs(low_freqs - harmonic) <= tolerance_hz
        harmonic_energy += float(np.sum(low_mag[mask]))
        harmonic += fundamental

    return min(1.0, harmonic_energy / (total_energy + 1e-12))

def spectrum_features(arr: np.ndarray, sr: int):
    """Extract spectral features from a single audio block.

    Returns a dict with:
      centroid_hz      — amplitude-weighted mean frequency (brightness indicator)
      flatness         — geometric/arithmetic mean ratio of magnitudes (0=tonal, 1=noise)
      lowband_ratio    — energy fraction in 30–180 Hz (bass/kick fundamentals)
      midband_ratio    — energy fraction in 180–1200 Hz (voice, guitar body, melody)
      highband_ratio   — energy fraction above 1200 Hz (sibilance, cymbals, birdsong)
      envelope_cv      — coefficient of variation (std/mean) of the intra-block RMS
                         envelope at 10ms hop. Music has dynamic amplitude modulation
                         (kicks, beats) producing CV 0.15–0.80; mechanical drones
                         (engines, mowers) maintain near-constant amplitude (CV < 0.10).
                         0.0 = perfectly flat amplitude, higher = more dynamic.
      harmonic_ratio   — fraction of low-band energy concentrated at harmonic peaks
                         (integer multiples of detected fundamental). Music bass has
                         clear harmonics (ratio 0.5–0.9); engine rumble is broadband
                         with distributed energy (ratio 0.1–0.4). 0.0 when insufficient
                         low-band energy to analyze.

    Band boundaries (30 / 180 / 1200 Hz):
      These are coarse spectral regions, not precision crossover points.
      30 Hz cuts DC drift and infrasonic rumble that cheap mics pick up.
      180 Hz is the upper limit of bass guitar and kick drum fundamentals.
      1200 Hz separates midrange melody from treble/overtone content.
      The boundaries were chosen to make lowband_ratio a reliable proxy
      for "has bass" (the primary music indicator) without being thrown
      off by sub-bass mic noise.
    """
    win = np.hanning(len(arr))
    x = arr * win
    fft = np.fft.rfft(x)
    mag = np.abs(fft) + 1e-12  # epsilon prevents log(0) in flatness calc
    freqs = np.fft.rfftfreq(len(x), d=1.0/sr)

    centroid = float(np.sum(freqs * mag) / np.sum(mag))
    geo = np.exp(np.mean(np.log(mag)))
    arith = np.mean(mag)
    flatness = float(geo / (arith + 1e-12))  # 0 = pure tone, 1 = white noise

    low = mag[(freqs >= 30) & (freqs <= 180)].sum()
    mid = mag[(freqs > 180) & (freqs <= 1200)].sum()
    high = mag[(freqs > 1200)].sum()
    total = low + mid + high + 1e-12

    lowband_ratio = float(low / total)
    midband_ratio = float(mid / total)
    highband_ratio = float(high / total)

    # -- Intra-block envelope variance (CV = std/mean of RMS at 10ms hop) --
    # Music has dynamic amplitude (kicks, syncopation) producing CV 0.15–0.80.
    # Mechanical drones maintain near-constant amplitude (CV < 0.10).
    # Uses the same 10ms hop as the spectral envelope analysis for consistency.
    hop = int(sr * 0.01)
    n_frames = len(arr) // hop
    envelope_cv = 0.0

    if n_frames >= 10:
        envelope = np.array([
            np.sqrt(np.mean(arr[i * hop:(i + 1) * hop] ** 2))
            for i in range(n_frames)
        ])
        env_mean = np.mean(envelope)

        if env_mean > 1e-8:
            envelope_cv = float(np.std(envelope) / env_mean)

    # -- Harmonic ratio: fraction of low-band energy at harmonic peaks --
    # Music bass has clear fundamental + integer-multiple overtones.
    # Engine rumble distributes energy broadly across the low band.
    # Only computed when meaningful low-band energy exists (lowband_ratio > 0.10).
    harmonic_ratio = 0.0
    low_mask = (freqs >= 30) & (freqs <= 180)
    low_mag = mag[low_mask]
    low_freqs = freqs[low_mask]

    if lowband_ratio > 0.10 and len(low_mag) > 3:
        harmonic_ratio = _compute_harmonic_ratio(low_mag, low_freqs)

    return {
        "centroid_hz": centroid,
        "envelope_cv": envelope_cv,
        "flatness": flatness,
        "harmonic_ratio": harmonic_ratio,
        "highband_ratio": highband_ratio,
        "lowband_ratio": lowband_ratio,
        "midband_ratio": midband_ratio,
    }

def envelope_window_metrics(samples, sr, frame_sec=0.25):
    """Peak-to-trough envelope contrast and alternation count for one analysis
    window — the quantified form of the rendered waveform's visual signature
    (DECISIONS D13).

    The envelope is plain RMS per frame_sec, matching the incidents-page
    waveform's per-pixel buckets, so the numbers correspond to what the
    operator sees: through-wall music falls 15–25 dB into troughs between
    beats and alternates at ~1 Hz; machine duty-cycle noise (planes, mowers)
    modulates too fast to drop at this timescale and measures nearly flat.

    Returns (depth_db, swings): depth_db = p90 − p10 of the frame envelope in
    dB; swings = count of envelope crossings of the depth midline (tall/short
    bar alternations). Returns (None, None) when the window holds fewer than
    8 frames (2 s at the default frame size) — too little envelope to judge.
    """
    frame = int(sr * frame_sec)
    if frame <= 0:
        return None, None
    n_frames = len(samples) // frame
    if n_frames < 8:
        return None, None
    frames = np.asarray(samples[:n_frames * frame], dtype=np.float64).reshape(n_frames, frame)
    env_db = 20.0 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-12)
    p10, p90 = np.percentile(env_db, [10, 90])
    depth_db = float(p90 - p10)
    midline = (p90 + p10) / 2.0
    above = env_db > midline
    swings = int(np.sum(above[1:] != above[:-1]))
    return depth_db, swings


def music_like_score(features: dict):
    """Composite heuristic: how much does this sound resemble music?

    Combines four signals:
      1. Bass energy (lowband_ratio) — music almost always has bass content.
         Multiplied by 1.6 to boost the 0–0.625 lowband_ratio range into 0–1.
         The 1.6 factor was chosen so that a lowband_ratio of ~0.40 (typical
         bass-heavy music through a wall) maps to ~0.64, comfortably above
         the min_music_like_score threshold of 0.62.

      2. Tonal window — a triangle function centered at flatness=0.35 with
         half-width 0.35. Peaks at 1.0 when flatness=0.35 (music's typical
         mix of tonal and broadband content) and falls to 0.0 at flatness=0
         (pure tone, like a siren) or flatness=0.70 (broadband noise, like
         rain). The 0.35 center was determined empirically: recorded music
         through walls/floors consistently lands in the 0.25–0.45 range.

      3. Midband penalty — engine sounds concentrate energy in the 180–1200 Hz
         band, while music follows a "smiley EQ" (boosted bass + treble,
         scooped mids). Ramps from 0% penalty at midband=0.40 to 30% at 1.0.

      4. Envelope variance penalty (new) — mechanical drones (engines, mowers)
         maintain near-constant amplitude within a block (envelope_cv < 0.10),
         while music has dynamic amplitude modulation from rhythmic content
         (envelope_cv 0.15–0.80). When bass is present (lowband > 0.15, enough
         to push the score toward threshold) but the envelope is flat (cv < 0.10),
         the score is reduced by up to 20%. This catches the major false-positive
         pathway: steady mechanical rumble with moderate bass fooling the formula.

      5. Harmonic bonus (new) — music bass has clear harmonic series (fundamental
         + overtones), while engine rumble is broadband. When harmonic_ratio > 0.50,
         the score receives a small boost (up to +0.08). This helps borderline
         music blocks confidently cross the threshold.

    Weighting: 0.6 * bass + 0.4 * tonal. Bass is weighted higher because
    it's the more reliable indicator (most non-music sounds lack sustained
    low-frequency energy). Tonal helps disambiguate bass-heavy non-music
    (truck idle at 0.60 flatness) from actual music.

    Final score is clamped to [0, 1].
    """
    lowband = features.get("lowband_ratio", 0.0)

    # 1.6x boost maps typical music lowband (0.30–0.50) into the 0.48–0.80 range
    low = max(0.0, min(1.0, lowband * 1.6))
    # Triangle peaking at flatness=0.35, zero at 0.0 and 0.70
    tonal_window = max(0.0, 1.0 - abs(features["flatness"] - 0.35) / 0.35)

    raw = 0.6 * low + 0.4 * tonal_window

    # Midband penalty: engine sounds concentrate energy in the 180–1200 Hz
    # band (midband_ratio 0.30–0.73), while music follows a "smiley EQ"
    # (boosted bass + treble, scooped mids). When midband dominates (>0.40),
    # reduce the score proportionally. Penalty ramps from 0% at midband=0.40
    # to 30% at midband=1.0. Validated against existing clips:
    #   Plane flyover midband median 0.31: no penalty (below 0.40)
    #   Engine speed midband median 0.63: penalty 38% → 12% score reduction
    #   Diesel midband median 0.34: no penalty
    #   Bass music midband ~0.33: no penalty (music preserved)
    midband = features.get("midband_ratio", 0.0)
    if midband > 0.40:
        midband_penalty = (midband - 0.40) / 0.60  # 0 at 0.40, 1.0 at 1.0
        raw = raw * (1.0 - 0.30 * midband_penalty)

    # Envelope variance penalty: steady-amplitude bass is almost certainly
    # mechanical (engines, mowers, compressors), not music. Music's rhythmic
    # content (kicks, bass drops) produces amplitude modulation visible in the
    # intra-block envelope. Only applies when there's meaningful bass energy
    # (lowband > 0.15) — without bass, the score is already low and the penalty
    # would be redundant. Penalty ramps from 0% at cv=0.10 to 20% at cv=0.
    envelope_cv = features.get("envelope_cv", 0.5)
    if lowband > 0.15 and envelope_cv < 0.10:
        # Linear ramp: cv=0.10 → 0% penalty, cv=0.0 → 20% penalty
        steady_penalty = (0.10 - envelope_cv) / 0.10  # 0 at 0.10, 1.0 at 0.0
        raw = raw * (1.0 - 0.20 * steady_penalty)

    # Harmonic bonus: clear harmonic series in the low band is a strong
    # indicator of tonal music content (kick drum, bass guitar, synth bass).
    # Engine rumble is broadband — energy distributed across the low band
    # without harmonic peaks. A small bonus (up to +0.08) helps borderline
    # music blocks cross the threshold without inflating non-music scores.
    # Only applies when harmonic_ratio > 0.50 (well above engine broadband).
    harmonic = features.get("harmonic_ratio", 0.0)
    if harmonic > 0.50:
        harmonic_bonus = (harmonic - 0.50) / 0.50  # 0 at 0.50, 1.0 at 1.0
        raw = raw + 0.08 * harmonic_bonus

    return max(0.0, min(1.0, raw))

def is_impulse(db_now, db_prev, delta_threshold):
    """Single-block transient: dB jump from previous block exceeds threshold.

    The threshold is a config parameter (default 14 dB). This fires for any
    sudden spike — gunshot, door slam, firework, or the leading edge of
    thunder. Thunder is checked first in the filter priority chain to avoid
    labeling it as a generic impulse.
    """
    return (db_now - db_prev) >= delta_threshold

def looks_like_thunder(features, db_now, db_prev, delta_threshold,
                       lowband_min=0.55, flatness_min=0.45,
                       recent_db=None, rumble_centroid_max=1500,
                       rumble_flatness_max=0.15, rumble_midband_min=0.40,
                       rumble_min_db=40.0, rumble_min_history=6,
                       rumble_window=12):
    """Detect thunder via two paths: sharp crack or sustained rumble.

    Path A (original) — single-block impulse with dominant bass:
      Requires a sharp dB spike (≥ delta_threshold) with concentrated
      low-frequency energy (lowband > 0.55) and broadband character
      (flatness > 0.45). Catches close lightning strikes and sharp cracks.

    Path B (sustained rumble) — mellow storm thunder:
      Recorded thunderstorms (especially mellow/distant ones) don't produce
      the sharp 18+ dB spikes that Path A requires. Instead, they ramp up
      over 2–3 seconds with centroid plummeting below 1500 Hz, extremely
      concentrated energy (flatness < 0.15 — the opposite of Path A's
      broadband requirement), and dominant midband from the rumble body.
      The key differentiator from mower: mower flatness is always ≥ 0.25,
      while thunder rumble flatness is < 0.15. The spectral criteria
      (centroid + flatness + midband) are highly restrictive on their own;
      a minimal dB floor (40 dBA) just rejects sub-noise-floor silence.
      The centroid ceiling of 1500 Hz (vs. the original 1300) captures the
      rumble's decaying tail where centroid drifts up to ~1450 Hz — this
      extra headroom lets holdover activate (5 consecutive blocks) so that
      thunder persists through inter-crack gaps, implementing mutual
      exclusion with flyover (the two are physically exclusive events).

    Args:
        features: spectrum_features() output dict
        db_now: current block dBA
        db_prev: previous block dBA
        delta_threshold: minimum dB jump for Path A (config: thunder_peak_delta_db)
        lowband_min: minimum lowband_ratio for Path A (default 0.55)
        flatness_min: minimum spectral flatness for Path A (default 0.45)
        recent_db: recent dB history for Path B stability check
        rumble_centroid_max: max centroid for Path B (default 1500 Hz)
        rumble_flatness_max: max flatness for Path B (default 0.15)
        rumble_midband_min: min midband ratio for Path B (default 0.40)
        rumble_min_db: minimum dBA for Path B (default 40.0 — just above
            sub-noise-floor silence; the spectral criteria are the real guards)
        rumble_min_history: min blocks of history for Path B (default 6)
        rumble_window: lookback window for Path B variance (default 12)
    """
    # Path A: sharp crack — original detection
    if ((db_now - db_prev) >= delta_threshold and
            features["lowband_ratio"] > lowband_min and
            features["flatness"] > flatness_min):
        return True

    # Path B: sustained rumble — mellow/distant thunder
    if recent_db is not None and len(recent_db) >= rumble_min_history:
        if (db_now >= rumble_min_db and
                features["centroid_hz"] <= rumble_centroid_max and
                features["flatness"] <= rumble_flatness_max and
                features["midband_ratio"] >= rumble_midband_min):
            return True

    return False

def looks_like_amplified_bass(features, recent_db, min_music_score=0.45,
                              lowband_min=0.16, centroid_max=4000.0,
                              env_std_max=3.0, min_history=6, window=12,
                              flatness_min=0.20):
    """Bass-heavy music — the neighborhood thump filter.

    Detects amplified music with boosted bass in two scenarios:

    Through walls/garage (strong bass, attenuated highs):
        lowband:  0.36–0.61 (median 0.43)
        centroid: 1833–3301 Hz
        flatness: 0.18–0.33
        mscore:   0.69–0.79 (median 0.73, all blocks ≥ 0.60)

    Open windows/doors (broader spectrum, less bass-dominant):
        lowband:  0.09–0.44 (median 0.19)
        centroid: 2131–4678 Hz
        flatness: 0.21–0.44 (median 0.28)
        mscore:   0.40–0.73 (median 0.50)

    HOWEVER, comma, this filter MUST run before rain and mower because those
    filters would steal bass-music blocks without the music score guard or
    this dedicated filter.

    The flatness floor (≥ 0.20) is critical for diesel separation — diesel
    engines have flatness median 0.151 (max 0.285), while bass music always
    exceeds 0.20 (min 0.207 across all recordings). This is the primary diesel
    guard, robust even when overlapping noise sources (e.g., truck + music)
    are present.

    Safety margins vs. false-positive sources at current thresholds:
        rain:    blocked by lowband (rain max 0.137 < 0.16 floor, margin 0.023)
        mower:   blocked by lowband (mower max 0.061) and mscore (max 0.449)
        diesel:  blocked by flatness (diesel median 0.151 < 0.20 floor)

    No dB floor is needed — the noise_floor_db gate (50 dBA) and ordinance
    recording thresholds already ensure only nuisance-level sounds reach DSP
    analysis, and the min_music_score check is a far stronger discriminator
    than a dB floor for this category.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        min_music_score: minimum music_like_score to qualify (default 0.45).
            Through-wall bass scores 0.60+; open-window bass median is 0.50.
        lowband_min: minimum lowband energy ratio (default 0.16). Through-wall
            bass has median 0.43; open-window bass median 0.19. Floor of 0.16
            sits 0.023 above rain's max (0.137).
        centroid_max: maximum centroid Hz (default 4000). Wall-filtered music
            centroid sits 2041–3545; open-window extends to ~4678 but bulk is
            below 4000.
        env_std_max: maximum dB std dev over window (default 3.0). Steady
            bass thump has low amplitude variation (0.68–2.05).
        min_history: minimum readings before filter activates (default 6)
        window: number of recent readings to evaluate (default 12)
        flatness_min: minimum spectral flatness (default 0.20). Critical
            diesel guard — diesel flatness median 0.151, bass music min 0.207.
    """
    if len(recent_db) < min_history:
        return False

    mscore = music_like_score(features)

    # The music score is the primary discriminator — if it doesn't register
    # as music, it's probably just a diesel/mower/rain source
    if mscore < min_music_score:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))
    return (
        features["flatness"] >= flatness_min and
        features["lowband_ratio"] >= lowband_min and
        features["centroid_hz"] <= centroid_max and
        env_std <= env_std_max
    )

def looks_like_rain(features, recent_db, flatness_threshold, variance_db,
                    min_history=6, window=12, lowband_min=0.07,
                    centroid_max=5000.0, max_music_score=0.70):
    """Steady broadband noise with very low amplitude variation.

    Rain produces a moderately flat spectrum with almost no amplitude
    fluctuation once established. Real-world calibration (outdoor rain at
    100+ dBA) showed flatness 0.27–0.38, lowband 0.08–0.14 (more bass
    than mechanical sources), centroid 3130–4023, and env_std < 0.50 once
    steady.

    The lowband minimum is the key separator from mower: mowers have very
    little bass (lowband 0.02–0.06) because engine vibration is mechanical
    mid-frequency drone, while rain is genuinely broadband and excites the
    low-frequency bands more evenly.

    The centroid ceiling prevents birdsong blocks with incidental bass
    content (lowband 0.12–0.19 from ambient fan/HVAC) from being absorbed.
    Rain centroid maxes at ~4023 Hz; birdsong false-positives start at 5800+.

    Music score guard: bass-heavy music through walls can mimic rain's flat,
    steady spectral profile (especially the steady thump of boosted bass).
    Real rain mscore maxes at 0.593; bass music scores 0.70+. The guard
    rejects blocks that score too highly as music before rain claims them.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        flatness_threshold: minimum spectral flatness (config: rain_flatness_threshold)
        variance_db: maximum dB std dev (config: rain_low_variance_db)
        min_history: minimum readings before filter activates (default 6)
        window: number of recent readings to evaluate (default 12)
        lowband_min: minimum lowband energy ratio (config: rain_lowband_min,
            default 0.07). Rain excites bass more than mechanical sources.
        centroid_max: maximum centroid Hz (config: rain_centroid_max_hz,
            default 5000). Rain is mid-frequency broadband, not high-pitched.
        max_music_score: maximum music_like_score before rejecting (default 0.70).
            Rain maxes at 0.593; bass music scores 0.70+.
    """
    if len(recent_db) < min_history:
        return False

    # Bass-heavy music through walls mimics rain's flat, steady profile
    if music_like_score(features) > max_music_score:
        return False

    arr = np.array(recent_db[-window:], dtype=float)
    return (
        features["flatness"] >= flatness_threshold and
        features["lowband_ratio"] >= lowband_min and
        features["centroid_hz"] <= centroid_max and
        float(np.std(arr)) <= variance_db
    )

def looks_like_wind(features, recent_db, flatness_min=0.30,
                    centroid_min=2500.0, centroid_max=8000.0,
                    lowband_min=0.12, lowband_max=0.26,
                    highband_min=0.30, highband_max=0.65,
                    env_std_max=4.0,
                    min_history=6, window=12, max_music_score=0.70):
    """Broadband wind noise — aero turbulence across the microphone.

    Wind across a microphone (even with a deadcat windscreen) produces broadband
    noise concentrated in mid-to-high frequencies, with some low-frequency
    content from air pressure fluctuations. Building features (roof edges,
    eaves, vents) can cause wind whistle, pushing centroid higher.

    Real-world calibration (moderate wind with roof-edge whistle):
      centroid:  3313–5858 Hz (median 4496 — higher than rain/mower)
      flatness:  0.316–0.545 (median 0.426 — broadband aero noise)
      lowband:   0.148–0.255 (median 0.203 — air pressure fluctuations)
      midband:   0.257–0.359 (median 0.303)
      highband:  0.402–0.595 (median 0.472 — wind whistle component)
      env_std:   0–2.548 (median 1.611 — steady once established)

    Key separators from similar categories:
      vs. rain — lowband_min (wind ≥ 0.12 vs rain 0.08–0.14). Wind has
        more bass from air pressure; rain bass is more uniform.
        Centroid also separates: wind median 4496, rain maxes at ~4023.
      vs. birdsong — birdsong has lowband ≤ 0.15 (shared ceiling); wind
        has lower highband (0.40–0.60 vs birdsong 0.70+). Birdsong is
        checked first in priority and catches dominant-highband blocks.
        HOWEVER, comma, birdsong blocks during the birdsong filter's warmup
        period (before min_history is met) can match wind if they share the
        spectral envelope. highband_max (0.65) prevents this: real wind maxes
        at 0.595, while birdsong's non-caught blocks have highband 0.65+.
      vs. mower — wind has much higher centroid (3300+ vs mower 300–4000).
        Mower centroid rarely exceeds 4000 in steady operation.

    NOTE: Wind thresholds were calibrated from a moderate-wind recording
    with roof-edge whistle. Stronger winds or different mic placement will
    produce different profiles. Adjust centroid and flatness ranges based on
    your deployment environment (open field vs building-mounted).

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        flatness_min: minimum spectral flatness (default 0.30 — broadband aero noise)
        centroid_min: minimum centroid Hz (default 2500 — wind sits higher than mower/diesel)
        centroid_max: maximum centroid Hz (default 8000 — wind whistle from obstructions)
        lowband_min: minimum lowband_ratio (default 0.12 — separates from rain max 0.14)
        lowband_max: maximum lowband_ratio (default 0.26 — wind is not bass-dominant,
            real wind maxes at 0.255; tight ceiling separates from plane lowband 0.28+)
        highband_min: minimum highband_ratio (default 0.30 — wind energy is mid-to-high)
        highband_max: maximum highband_ratio (default 0.65 — wind maxes at 0.595;
            prevents birdsong blocks (highband 0.65+) from matching during
            birdsong filter warmup)
        env_std_max: maximum dB std dev (default 4.0 — wind is relatively steady)
        min_history: minimum readings before filter activates (default 6)
        window: number of recent readings to evaluate (default 12)
        max_music_score: maximum music_like_score before rejecting (default 0.70).
            Prevents bass-heavy music from being misclassified as wind.
    """
    if len(recent_db) < min_history:
        return False

    # Music guard: bass music through walls can overlap wind's flatness range
    if music_like_score(features) > max_music_score:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))

    return (
        features["flatness"] >= flatness_min and
        centroid_min <= features["centroid_hz"] <= centroid_max and
        lowband_min <= features["lowband_ratio"] <= lowband_max and
        highband_min <= features["highband_ratio"] <= highband_max and
        env_std <= env_std_max
    )

def looks_like_mower(features, recent_db, flatness_threshold, cmin, cmax,
                     env_std_max=4.5, min_history=6, window=12, min_db=70.0,
                     db_now=None, highband_max=0.75, max_music_score=0.70):
    """Sustained mechanical drone in the 300–4000 Hz range.

    Mowers/blowers produce a mid-frequency drone with moderate flatness and
    fairly stable amplitude. Real-world calibration showed flatness as low as
    0.28 during steady operation and centroid up to 3920 Hz.

    A minimum dB floor rejects quiet sources (fans, HVAC) whose spectral shape
    mimics a mower. Real mowers at any meaningful distance produce 70+ dBA;
    computer fans and indoor equipment sit at 55–65 dBA.

    A highband ceiling rejects sounds dominated by high-frequency energy. Real
    mowers peak at highband 0.664 (rain-on-mower) / 0.630 (gas mower). Bird
    choruses with low centroids can mimic mower flatness + centroid but have
    highband 0.80+ — well above what any mechanical drone produces.

    Music score guard: bass-heavy music through walls can land in the mower's
    centroid + flatness range. Real mower mscore maxes at 0.548 (gas) / 0.637
    (electric, 3 blocks); bass music scores 0.70+. The guard rejects blocks
    that clearly register as music before mower claims them.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        flatness_threshold: minimum spectral flatness (config: mower_flatness_threshold)
        cmin: minimum centroid Hz (config: mower_centroid_min_hz, default 300)
        cmax: maximum centroid Hz (config: mower_centroid_max_hz, default 4000)
        env_std_max: maximum dB std dev over window (default 4.5). Mowers are
            remarkably steady; 4.5 dB allows for throttle variation and startup
            transitions without matching the wider swings of traffic or music.
        min_history: minimum readings before filter activates (default 6)
        window: number of recent readings to evaluate (default 12)
        min_db: minimum current-block dBA to qualify (default 70.0). Rejects
            quiet fan/HVAC hum that spectrally resembles a mower.
        db_now: current block dBA. When provided, must meet min_db floor.
        highband_max: maximum highband energy ratio (default 0.75). Rejects
            sounds dominated by high-frequency energy (birdsong, insects).
        max_music_score: maximum music_like_score before rejecting (default 0.70).
            Mower gas maxes at 0.548; bass music scores 0.70+.
    """
    if len(recent_db) < min_history:
        return False

    # Quiet sources (fans, HVAC) can mimic mower spectrally but are far too soft
    if db_now is not None and db_now < min_db:
        return False

    # Bass-heavy music can land in mower's centroid + flatness range
    if music_like_score(features) > max_music_score:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))
    return (
        features["flatness"] >= flatness_threshold and
        cmin <= features["centroid_hz"] <= cmax and
        features["highband_ratio"] <= highband_max and
        env_std <= env_std_max
    )

def looks_like_birdsong(features, recent_db, highband_min=0.70, lowband_max=0.15,
                        flatness_min=0.30, variance_max=3.0, min_history=8,
                        peak_highband_min=0.89, peak_centroid_min=2800.0,
                        peak_db_threshold=10.0, peak_variance_min=8.0,
                        purity_highband_min=0.95, purity_db_margin=15.0,
                        feature_history=None,
                        chorus_highband_std_min=0.10, chorus_lowband_max=0.12,
                        chorus_min_history=12):
    """Detect birdsong: high-frequency energy with minimal bass.

    Uses four detection paths to handle different bird call patterns:

    PATH A — Sustained/tonal birdsong (warblers, wrens, robins):
      High-frequency energy dominates with stable amplitude. Catches both
      broadband trills (high flatness) and tonal chirps (lower flatness).
      Flatness lowered from 0.50 to 0.30 after incident 5 showed robin
      chirps have sharp harmonic peaks (flatness 0.09–0.43) that never
      reached the original threshold.

    PATH B — Bursty birdsong (robins, doves, seagulls):
      Loud chirps alternating with quiet gaps create high amplitude variance
      that fails Path A's stability check. Instead, we check whether the
      *current block* is a loud chirp with unmistakably birdy spectral features:
      extreme highband (≥0.89) and high centroid (≥2800 Hz). These values are
      well above what mowers produce even at their highest-frequency blocks
      (mower max highband ~0.80, max centroid ~2600 Hz in our calibration data).
      The high amplitude variance (env_std ≥ 8.0) confirms a bursty pattern
      rather than a sustained monotone, and the block must be significantly
      louder than the window mean to confirm it's a chirp peak, not a gap.

    PATH C — Extreme spectral purity (clean recordings, isolated birds):
      When ≥95% of energy resides above half-Nyquist with a high centroid and
      minimal bass (already checked), the spectral shape alone is diagnostic.
      No common mechanical or environmental noise source concentrates energy
      this extremely in the upper spectrum (mowers max ~0.80, HVAC/fans are
      broadband, traffic is low-frequency dominant).
      This catches clean bursty recordings where:
        - Path A fails because env_std is too high (quiet gaps between chirps)
        - Path B fails because consecutive loud chirps keep the running mean
          elevated, preventing any single chirp from being 10+ dB above mean
      A dB floor (mean - margin) rejects near-silence blocks where random
      noise artifacts can produce misleading spectral shapes.

    PATH D — Multi-species chorus (mixed calls, moderate variance):
      Multiple bird species calling simultaneously produce amplitude variance
      too high for Path A (env_std median ~5 vs threshold 1.0) and spectral
      characteristics too moderate for Paths B/C (median highband 0.68, median
      centroid 1980). The temporal signature is the key discriminator: across
      a window of blocks, highband_ratio oscillates significantly as different
      species alternate calls. Mowers and other mechanical sources produce
      stable highband values. Requires feature_history (list of recent
      spectrum_features dicts) from the engine.
      Safety margins: tighter lowband ceiling (0.12 vs shared 0.15) and
      minimum 12 blocks of history to prevent premature triggering.

    Lowband ceiling raised from 0.10 to 0.15 after discovering that continuous
    capture (vs. blocking mode) faithfully represents background bass from fans,
    HVAC, and traffic rumble. Outdoor ambient + fan hum pushed lowband to 0.11
    on quiet blocks, incorrectly rejecting obvious robin chirps.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings for amplitude stability check
        highband_min: PATH A minimum highband_ratio (default 0.70)
        lowband_max: maximum lowband_ratio — shared by all paths (default 0.15)
        flatness_min: PATH A minimum spectral flatness (default 0.30)
        variance_max: PATH A maximum dB std dev (default 3.0)
        min_history: minimum dB readings required (default 8)
        peak_highband_min: PATH B minimum highband for loud chirp (default 0.89)
        peak_centroid_min: PATH B/C minimum centroid Hz (default 2800)
        peak_db_threshold: PATH B dB above window mean to qualify as peak (default 10.0)
        peak_variance_min: PATH B minimum env_std to confirm bursty pattern (default 8.0)
        purity_highband_min: PATH C minimum highband for spectral purity (default 0.95)
        purity_db_margin: PATH C max dB below window mean (default 15.0)
        feature_history: list of recent spectrum_features() dicts (for Path D)
        chorus_highband_std_min: PATH D minimum std dev of highband over window (default 0.06)
        chorus_lowband_max: PATH D tighter lowband ceiling (default 0.12)
        chorus_min_history: PATH D minimum feature history entries (default 12)
    """
    if len(recent_db) < min_history:
        return False

    window = np.array(recent_db[-12:], dtype=float)
    env_std = float(np.std(window))

    # Shared requirement: minimal bass content (allows incidental fan/HVAC hum)
    if features["lowband_ratio"] > lowband_max:
        return False

    # PATH A — sustained/tonal birdsong
    # Flatness requirement is relaxed for extreme highband (≥ peak_highband_min,
    # typically 0.89). Robin chirps concentrate energy in narrow harmonic peaks,
    # producing very low flatness (0.09–0.20) despite being unmistakably birdsong.
    # At extreme highband + near-zero lowband, nothing else looks like this.
    effective_flatness_min = flatness_min
    if features["highband_ratio"] >= peak_highband_min:
        effective_flatness_min = 0.0  # Extreme highband is sufficient

    if (features["highband_ratio"] >= highband_min and
            features["flatness"] >= effective_flatness_min and
            env_std <= variance_max):
        return True

    # PATH B — bursty birdsong (peak-weighted detection)
    # Current block must be a loud peak with extreme high-frequency signature,
    # and the overall window must show high amplitude variance (bursty pattern).
    if (env_std >= peak_variance_min and
            features["highband_ratio"] >= peak_highband_min and
            features["centroid_hz"] >= peak_centroid_min and
            recent_db[-1] >= float(np.mean(window)) + peak_db_threshold):
        return True

    # PATH C — extreme spectral purity
    # When nearly all energy (≥ 95%) resides above half-Nyquist with a high
    # centroid, the spectral shape alone is diagnostic of birdsong. The dB
    # floor (mean - margin) prevents near-silence blocks from triggering,
    # where random noise artifacts can produce misleading spectral shapes.
    if (features["highband_ratio"] >= purity_highband_min and
            features["centroid_hz"] >= peak_centroid_min and
            recent_db[-1] >= float(np.mean(window)) - purity_db_margin):
        return True

    # PATH D — multi-species chorus (temporal highband variance)
    # Multiple species calling simultaneously create significant block-to-block
    # variation in highband_ratio as different calls overlap and alternate.
    # Mowers and other mechanical sources produce stable highband values.
    # Uses feature_history (maintained by engine) to compute std dev of
    # highband_ratio over recent blocks.
    # Two safety margins prevent false positives:
    #   1. Tighter lowband ceiling applied to the ENTIRE window (not just
    #      current block). This kills thunder (lowband 0.55+ on crack blocks),
    #      mower (window always has some blocks at 0.12+), rain (0.16+), and
    #      diesel (0.19+). Chorus max lowband in any block: 0.133.
    #   2. Minimum highband std (0.06) rejects monotone steady-state sources.
    if feature_history and len(feature_history) >= chorus_min_history:
        recent = feature_history[-chorus_min_history:]
        recent_lb = [f["lowband_ratio"] for f in recent]

        # All blocks in the window must have low bass — no exceptions.
        # This is the primary mower/thunder/rain discriminator.
        if max(recent_lb) <= chorus_lowband_max:
            recent_hb = [f["highband_ratio"] for f in recent]
            hb_std = float(np.std(recent_hb))
            if hb_std >= chorus_highband_std_min:
                return True

    return False

def looks_like_weedwhacker(features, recent_db, centroid_min=2000.0,
                           centroid_max=6000.0, flatness_min=0.50,
                           lowband_max=0.15, env_std_max=5.0,
                           min_history=6, window=12):
    """Detect weedwhacker/string trimmer: high-pitched mechanical whine.

    Weedwhackers produce a distinctive high-frequency sound from the spinning
    nylon line (2–6 kHz) combined with the motor. Key differences from similar
    categories:
      vs. birdsong — weedwhackers have higher spectral flatness (0.50+ vs
        birdsong's 0.50+) but lower highband (motor adds midband energy).
      vs. mower — weedwhackers have a higher centroid (2000+ vs 300–4000 Hz).
        Mowers concentrate energy in the midrange; weedwhackers push into
        the treble due to the line whip.
      Amplitude is moderately steady (operator moves it around but the motor
        is constant), so env_std_max is relaxed to 5.0 vs mower's 3.5.

    Flatness raised from 0.45 to 0.50 after real-world mower calibration showed
    mower flatness spikes to 0.46, causing overlap.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        centroid_min: minimum centroid Hz (default 2000 — above mower range)
        centroid_max: maximum centroid Hz (default 6000 — above this is likely sibilance)
        flatness_min: minimum spectral flatness (default 0.50 — raised from 0.45)
        lowband_max: maximum lowband_ratio (default 0.15 — no significant bass)
        env_std_max: maximum dB std dev (default 5.0 — operator movement allowed)
        min_history: minimum readings before filter activates (default 6)
        window: number of recent readings to evaluate (default 12)
    """
    if len(recent_db) < min_history:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))

    return (
        centroid_min <= features["centroid_hz"] <= centroid_max and
        features["flatness"] >= flatness_min and
        features["lowband_ratio"] <= lowband_max and
        env_std <= env_std_max
    )

def looks_like_diesel(features, recent_db, centroid_min=1200.0,
                      centroid_max=3600.0,
                      flatness_max=0.20, lowband_min=0.10,
                      midband_min=0.20, env_std_max=3.0,
                      min_history=8, window=12):
    """Detect diesel engine sound: sustained tonal harmonics in the mid-frequency range.

    Real-world calibration (diesel car at ~71 dBA steady-state) showed:
    flatness 0.12–0.16, centroid 1441–2023 Hz, lowband 0.14–0.25,
    midband 0.22–0.35, highband 0.27–0.51, env_std ~2.0 once steady.

    Diesel engines produce strong tonal harmonics (very low flatness) from
    the firing cycle. This is the defining characteristic — real diesel is
    far more tonal than any other exclusion category:
      vs. mower — mower flatness ≥ 0.28, diesel ≤ 0.20. Clean gap.
      vs. rain — rain flatness ≥ 0.27, diesel ≤ 0.20. Clean gap.
      vs. birdsong — birdsong has very low lowband (≤ 0.09 for chorus,
        ≤ 0.19 for morning) vs. diesel lowband ≥ 0.10. Birdsong is checked
        first and rarely reaches the diesel filter.
      vs. thunder — thunder rumble sections can mimic diesel spectrally.
        Thunder is checked first in priority for its characteristic blocks,
        but ambient rumble between claps can match diesel if thresholds are
        too loose. Tight flatness (≤ 0.20) and env_std (≤ 3.0) constraints
        limit false positives in storm recordings.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        centroid_min: minimum centroid Hz (default 1200 — diesel has mid-frequency
            fundamentals, separates from very low thunder rumble)
        centroid_max: maximum centroid Hz (default 3600 — diesel is mid-frequency,
            below mower range of 3808+)
        flatness_max: maximum spectral flatness (default 0.20 — diesel is very tonal
            from engine harmonics; clean gap from mower ≥ 0.28 and rain ≥ 0.27)
        lowband_min: minimum lowband_ratio (default 0.10 — diesel has some bass
            content; separates from birdsong which is mostly ≤ 0.09)
        midband_min: minimum midband_ratio (default 0.20 — engine energy sits
            in mid frequencies)
        env_std_max: maximum dB std dev (default 3.0 — diesel is very steady once
            running; real car ~2.0 stable)
        min_history: minimum readings before filter activates (default 8)
        window: number of recent readings to evaluate (default 12)
    """
    if len(recent_db) < min_history:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))

    return (
        centroid_min <= features["centroid_hz"] <= centroid_max and
        features["flatness"] <= flatness_max and
        features["lowband_ratio"] >= lowband_min and
        features["midband_ratio"] >= midband_min and
        env_std <= env_std_max
    )

def looks_like_flyover(features, recent_db, flatness_min=0.15,
                       flatness_max=0.55, centroid_min=1400.0,
                       centroid_max=12000.0, midband_min=0.15,
                       lowband_min=0.10, lowband_max=0.50,
                       highband_max=0.60, env_std_max=8.0,
                       min_history=4, window=12,
                       max_music_score=0.55):
    """Aerial vehicle or transient engine — planes, helicopters, fast-pass vehicles.

    This filter runs late in the priority chain (after wind, rain, weedwhacker,
    mower, and diesel) and catches remaining engine-like sounds that slip through
    more specific filters. Because it runs after all specific engine profiles,
    blocks matching diesel (very tonal), mower (steady broadband), etc. are
    already caught. What remains here are engine sounds falling in the gaps
    between those profiles.

    Real-world calibration — propeller-driven plane at recording distance:
      centroid:  2508–10631 Hz (median 4499 — Doppler + distance effects)
      flatness:  0.218–0.465 (median 0.393 — broadband engine + aero noise)
      lowband:   0.164–0.471 (median 0.283 — engine rumble, attenuated by distance)
      midband:   0.186–0.416 (median 0.310 — engine fundamentals)
      highband:  0.220–0.651 (median 0.400)
      env_std:   0–6.16 (median 2.714 — approach/sustain/departure arc)

    Real-world calibration — vehicle acceleration (engine speed launch):
      flatness:  0.107–0.472 (median 0.214 — tonal engine harmonics)
      centroid:  1412–5283 (median 2408 — mid-frequency engine sound)
      midband:   0.326–0.725 (median 0.632 — dominant midband = engine signature)
      env_std:   0–7.87 (median 7.04 — rapidly changing RPMs)

    Key separator from mower: highband ceiling. Mower blocks that slip past
    their own filter (centroid > 4000) have very high highband (0.56–0.82).
    Real flyovers have moderate highband (plane median 0.40, engine speed
    max 0.48). The highband_max (default 0.60) prevents flyover from
    absorbing escaped mower blocks.

    Key separator from mower/birdsong: lowband floor. Escaped mower blocks
    have very low lowband (0.02–0.06), well below the 0.10 floor. Real
    flyovers always have at least some engine rumble (plane min 0.16).

    Key separator from amplified bass music: music_like_score ceiling.
    Through-wall bass music lands squarely inside the flyover spectral
    ranges (centroid 1400–4000, flatness 0.15–0.40, midband 0.15–0.35,
    lowband 0.16–0.40) and was being misclassified as flyover in 8 out
    of 8 confirmed IDIOT incidents reviewed in v19. Real flyovers have
    mscore median 0.26–0.57; confirmed music has mscore median 0.62–0.73.
    The 0.55 ceiling provides clean separation and matches the guard
    pattern already used by rain, wind, and mower filters.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        flatness_min: minimum spectral flatness (default 0.15 — above pure-tone diesel)
        flatness_max: maximum spectral flatness (default 0.55 — below broadband rain)
        centroid_min: minimum centroid Hz (default 1400 — real plane min 2508
            at 22050 Hz. Threshold is sample-rate dependent; analyze_clip
            resamples source audio to the config sample rate before feature
            extraction, ensuring this value works consistently regardless
            of the source file's native rate)
        centroid_max: maximum centroid Hz (default 12000 — Doppler-shifted flyovers)
        midband_min: minimum midband_ratio (default 0.15 — engine energy indicator)
        lowband_min: minimum lowband_ratio (default 0.10 — some engine bass;
            separates from escaped mower blocks with lowband 0.02–0.06)
        lowband_max: maximum lowband_ratio (default 0.50 — not pure bass thump)
        highband_max: maximum highband_ratio (default 0.60 — separates from
            escaped mower blocks with highband 0.56–0.82. Real flyovers have
            moderate highband: plane median 0.40, engine speed max 0.48)
        env_std_max: maximum dB std dev (default 8.0 — allows approach/departure arc)
        min_history: minimum readings before filter activates (default 4 — short for
            transient flyovers)
        window: number of recent readings to evaluate (default 12)
        max_music_score: maximum music_like_score before rejecting (default 0.55).
            Blocks with high music scores are more likely amplified bass music
            than engine noise, even if their spectral shape falls within the
            flyover ranges. Real flyovers rarely exceed 0.55; confirmed IDIOT
            music incidents consistently score 0.62+.
    """
    if len(recent_db) < min_history:
        return False

    if music_like_score(features) > max_music_score:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))

    return (
        flatness_min <= features["flatness"] <= flatness_max and
        centroid_min <= features["centroid_hz"] <= centroid_max and
        features["midband_ratio"] >= midband_min and
        lowband_min <= features["lowband_ratio"] <= lowband_max and
        features["highband_ratio"] <= highband_max and
        env_std <= env_std_max
    )

def looks_like_conversation(features, recent_db, centroid_min=500.0,
                            centroid_max=2500.0, lowband_max=0.35,
                            flatness_max=0.55, env_std_min=4.0,
                            env_std_max=8.0, db_range_max=15.0,
                            min_history=10, window=12,
                            max_music_score=0.55, min_db=0.0,
                            midband_min=0.25, db_now=0.0):
    """Detect human conversation: mid-frequency speech with syllable-level amplitude modulation.

    Conversation is the broadest and least distinctive category — it is checked
    last in filter priority because many other sounds share overlapping spectral
    features. The key distinguisher is the amplitude modulation pattern:
      env_std_min (default 4.0) — speech has natural syllable dynamics, producing
        more amplitude variation than steady mechanical sounds (mower ~1.5 dB,
        rain ~1.0 dB). This rejects all steady drones.
      env_std_max (default 8.0) — but not as wild as random traffic or erratic
        music. This rejects spiky environmental noise.
      db_range_max (default 15.0) — maximum dB range (max - min) within the
        analysis window. Real speech modulates ~3-8 dB; a 25+ dB range means
        the window straddles a major level transition (e.g. mower starting or
        stopping), not actual syllable modulation. Without this guard,
        any sudden dB cliff inflates env_std into the conversation band.

    Other checks narrow the spectral signature:
      centroid: 500–2500 Hz covers the voice fundamental range plus first
        few formants. Below 500 Hz is rumble; above 2500 Hz is sibilance-only.
      lowband_max: 0.35 — conversation has some chest resonance but is not
        bass-dominant. Rejects diesel/mower/music.
      flatness_max: 0.55 — speech has harmonics (lower flatness than rain/mower)
        but isn't as tonal as a pure instrument note.
      max_music_score: 0.55 — music-with-vocals overlaps conversation's centroid
        and modulation ranges. The filter runs before music classification in the
        engine, so without this guard, vocal music that passes other spectral
        checks would be misclassified as conversation. Real conversation scores
        0.43-0.53 (light bass, moderate tonal); music through walls scores 0.65+
        (heavy bass + strong tonal). The 0.55 threshold sits in the clean gap
        between these distributions.
      min_db: 0.0 — optional dB floor. Quiet ambient conversations below
        nuisance threshold should not trigger the filter. Set to 60+ to reject
        indoor/background-level speech.
      midband_min: 0.25 — speech concentrates energy in the midband (250–4000 Hz
        formant region). Blocks with very little midband energy are more likely
        broad environmental noise than human voices.

    Caveats:
      - Single speakers are easier to detect than large groups (groups approach
        broadband noise and may trigger rain filter instead).
      - Requires at least 10 readings (10 seconds) for reliable syllable pattern
        detection.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        centroid_min: minimum centroid Hz (default 500)
        centroid_max: maximum centroid Hz (default 2500)
        lowband_max: maximum lowband_ratio (default 0.35 — not bass-dominant)
        flatness_max: maximum spectral flatness (default 0.55 — speech has harmonics)
        env_std_min: minimum dB std dev (default 4.0 — requires syllable modulation)
        env_std_max: maximum dB std dev (default 8.0 — not wildly erratic)
        db_range_max: maximum dB range in window (default 15.0 — rejects level transitions)
        min_history: minimum readings before filter activates (default 10)
        window: number of recent readings to evaluate (default 12)
        max_music_score: reject if music_like_score exceeds this (default 0.45)
        min_db: minimum dBA for conversation to be relevant (default 0.0 — disabled)
        midband_min: minimum midband_ratio (default 0.25 — speech is midband-dominant)
        db_now: current block dBA (used for min_db check)
    """
    if len(recent_db) < min_history:
        return False

    # Reject quiet background-level speech below nuisance threshold
    if min_db > 0 and db_now < min_db:
        return False

    # Reject blocks that score as music — vocal music overlaps conversation's
    # spectral range but has stronger bass and tonal structure. Without this
    # guard, the filter chain would label music-with-vocals as conversation
    # before the engine's classify_sound() ever runs.
    if max_music_score > 0:
        mscore = music_like_score(features)
        if mscore >= max_music_score:
            return False

    # Reject blocks without sufficient midband energy — speech concentrates
    # in the formant region. Environmental noise with a matching centroid but
    # flat energy distribution across bands is not conversation.
    if features.get("midband_ratio", 0.0) < midband_min:
        return False

    window_db = np.array(recent_db[-window:], dtype=float)
    env_std = float(np.std(window_db))

    # Reject if the dB range within the window is too large — this indicates
    # the window straddles a major level transition (mower start/stop), not
    # real syllable-level speech modulation.
    db_range = float(np.max(window_db) - np.min(window_db))
    if db_range > db_range_max:
        return False

    return (
        centroid_min <= features["centroid_hz"] <= centroid_max and
        features["lowband_ratio"] <= lowband_max and
        features["flatness"] <= flatness_max and
        env_std_min <= env_std <= env_std_max
    )

# ===========================================================================
# Filter orchestration — priority-ordered chain with config plumbing
# ===========================================================================
#
# Each _check_* function bridges the gap between the detection config dict
# (flat keys with string values) and the individual looks_like_* functions
# (explicit typed parameters). This keeps the looks_like_* functions clean
# and independently testable while centralizing config-reading in one place.
#
# To add a new filter:
#   1. Write a looks_like_*() function above with explicit parameters
#   2. Write a _check_*() wrapper below that reads config with defaults
#   3. Add a (name, checker) tuple to FILTER_CHAIN at the right priority
#
# The engine never touches individual filter parameters — it just calls
# identify_filter() with the raw detection config dict.

def _check_thunder(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_thunder(
        features, db_now, prev_db,
        float(det.get("thunder_peak_delta_db", 18.0)),
        lowband_min=float(det.get("thunder_lowband_min", 0.55)),
        flatness_min=float(det.get("thunder_flatness_min", 0.45)),
        recent_db=db_history,
        rumble_centroid_max=int(det.get("thunder_rumble_centroid_max", 1500)),
        rumble_flatness_max=float(det.get("thunder_rumble_flatness_max", 0.15)),
        rumble_midband_min=float(det.get("thunder_rumble_midband_min", 0.40)),
        rumble_min_db=float(det.get("thunder_rumble_min_db", 40.0)),
        rumble_min_history=int(det.get("thunder_rumble_min_history", 6)),
        rumble_window=int(det.get("thunder_rumble_window", 12)),
    )

def _check_impulse(features, db_history, db_now, prev_db, det, feature_history=None):
    if not is_impulse(db_now, prev_db, float(det.get("impulse_peak_delta_db", 14.0))):
        return False

    # Exempt high-frequency, low-bass transients that look like bird chirps.
    # Real impulses (slams, fireworks) have broadband or low-frequency energy,
    # while bird chirps concentrate energy above 2 kHz with very little bass.
    # Without this guard, robin chirps (30+ dB jumps) always trigger impulse
    # detection and prevent the birdsong filter from ever seeing them.
    hb_thresh = float(det.get("impulse_birdsong_highband_min", 0.89))
    lb_thresh = float(det.get("impulse_birdsong_lowband_max", 0.15))
    if (features["highband_ratio"] >= hb_thresh and
            features["lowband_ratio"] <= lb_thresh):
        return False

    return True

def _check_birdsong(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_birdsong(
        features, db_history,
        highband_min=float(det.get("birdsong_highband_min", 0.70)),
        lowband_max=float(det.get("birdsong_lowband_max", 0.15)),
        flatness_min=float(det.get("birdsong_flatness_min", 0.30)),
        variance_max=float(det.get("birdsong_amplitude_std_max", 1.0)),
        min_history=int(det.get("birdsong_min_history", 8)),
        peak_highband_min=float(det.get("birdsong_peak_highband_min", 0.89)),
        peak_centroid_min=float(det.get("birdsong_peak_centroid_min", 2800.0)),
        peak_db_threshold=float(det.get("birdsong_peak_db_threshold", 10.0)),
        peak_variance_min=float(det.get("birdsong_peak_variance_min", 8.0)),
        purity_highband_min=float(det.get("birdsong_purity_highband_min", 0.95)),
        purity_db_margin=float(det.get("birdsong_purity_db_margin", 15.0)),
        feature_history=feature_history,
        chorus_highband_std_min=float(det.get("birdsong_chorus_highband_std_min", 0.10)),
        chorus_lowband_max=float(det.get("birdsong_chorus_lowband_max", 0.12)),
        chorus_min_history=int(det.get("birdsong_chorus_min_history", 12)),
    )

def _check_amplified_bass(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_amplified_bass(
        features, db_history,
        min_music_score=float(det.get("amplified_bass_min_music_score", 0.45)),
        lowband_min=float(det.get("amplified_bass_lowband_min", 0.16)),
        centroid_max=float(det.get("amplified_bass_centroid_max_hz", 4000)),
        env_std_max=float(det.get("amplified_bass_env_std_max", 3.0)),
        min_history=int(det.get("amplified_bass_min_history", 6)),
        flatness_min=float(det.get("amplified_bass_flatness_min", 0.20)),
    )

def _check_rain(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_rain(
        features, db_history,
        float(det.get("rain_flatness_threshold", 0.27)),
        float(det.get("rain_low_variance_db", 1.5)),
        lowband_min=float(det.get("rain_lowband_min", 0.07)),
        centroid_max=float(det.get("rain_centroid_max_hz", 5000)),
        max_music_score=float(det.get("rain_max_music_score", 0.70)),
    )

def _check_wind(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_wind(
        features, db_history,
        flatness_min=float(det.get("wind_flatness_min", 0.30)),
        centroid_min=float(det.get("wind_centroid_min_hz", 2500)),
        centroid_max=float(det.get("wind_centroid_max_hz", 8000)),
        lowband_min=float(det.get("wind_lowband_min", 0.12)),
        lowband_max=float(det.get("wind_lowband_max", 0.26)),
        highband_min=float(det.get("wind_highband_min", 0.30)),
        highband_max=float(det.get("wind_highband_max", 0.65)),
        env_std_max=float(det.get("wind_env_std_max", 4.0)),
        min_history=int(det.get("wind_min_history", 6)),
        max_music_score=float(det.get("wind_max_music_score", 0.70)),
    )

def _check_weedwhacker(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_weedwhacker(
        features, db_history,
        centroid_min=float(det.get("weedwhacker_centroid_min_hz", 2000)),
        centroid_max=float(det.get("weedwhacker_centroid_max_hz", 6000)),
        flatness_min=float(det.get("weedwhacker_flatness_min", 0.50)),
        lowband_max=float(det.get("weedwhacker_lowband_max", 0.15)),
        env_std_max=float(det.get("weedwhacker_env_std_max", 5.0)),
    )

def _check_mower(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_mower(
        features, db_history,
        float(det.get("mower_flatness_threshold", 0.28)),
        float(det.get("mower_centroid_min_hz", 300)),
        float(det.get("mower_centroid_max_hz", 4000)),
        env_std_max=float(det.get("mower_env_std_max", 4.5)),
        min_db=float(det.get("mower_min_db", 70.0)),
        db_now=db_now,
        highband_max=float(det.get("mower_highband_max", 0.75)),
        max_music_score=float(det.get("mower_max_music_score", 0.70)),
    )

def _check_diesel(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_diesel(
        features, db_history,
        centroid_min=float(det.get("diesel_centroid_min_hz", 1200)),
        centroid_max=float(det.get("diesel_centroid_max_hz", 3600)),
        flatness_max=float(det.get("diesel_flatness_max", 0.20)),
        lowband_min=float(det.get("diesel_lowband_min", 0.10)),
        midband_min=float(det.get("diesel_midband_min", 0.20)),
        env_std_max=float(det.get("diesel_env_std_max", 3.0)),
        min_history=int(det.get("diesel_min_history", 8)),
    )

def _check_flyover(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_flyover(
        features, db_history,
        flatness_min=float(det.get("flyover_flatness_min", 0.15)),
        flatness_max=float(det.get("flyover_flatness_max", 0.55)),
        centroid_min=float(det.get("flyover_centroid_min_hz", 1400)),
        centroid_max=float(det.get("flyover_centroid_max_hz", 12000)),
        midband_min=float(det.get("flyover_midband_min", 0.15)),
        lowband_min=float(det.get("flyover_lowband_min", 0.10)),
        lowband_max=float(det.get("flyover_lowband_max", 0.50)),
        highband_max=float(det.get("flyover_highband_max", 0.60)),
        env_std_max=float(det.get("flyover_env_std_max", 8.0)),
        min_history=int(det.get("flyover_min_history", 4)),
        max_music_score=float(det.get("flyover_max_music_score", 0.55)),
    )

def _check_conversation(features, db_history, db_now, prev_db, det, feature_history=None):
    return looks_like_conversation(
        features, db_history,
        centroid_min=float(det.get("conversation_centroid_min_hz", 500)),
        centroid_max=float(det.get("conversation_centroid_max_hz", 2500)),
        lowband_max=float(det.get("conversation_lowband_max", 0.35)),
        flatness_max=float(det.get("conversation_flatness_max", 0.55)),
        env_std_min=float(det.get("conversation_env_std_min", 4.0)),
        env_std_max=float(det.get("conversation_env_std_max", 8.0)),
        db_range_max=float(det.get("conversation_db_range_max", 15.0)),
        min_history=int(det.get("conversation_min_history", 10)),
        max_music_score=float(det.get("conversation_max_music_score", 0.55)),
        min_db=float(det.get("conversation_min_db", 0.0)),
        midband_min=float(det.get("conversation_midband_min", 0.25)),
        db_now=db_now,
    )

# Priority-ordered filter chain. More specific patterns first, broadest last.
# Thunder before impulse (thunder IS an impulse, but more descriptive).
# Birdsong before weedwhacker (birdsong is more specific high-freq pattern).
# Amplified bass before wind/rain/mower (bass music mimics their steady profiles;
#   the music score guard on rain/mower provides defense-in-depth, but this
#   dedicated filter gives the recording a proper classification label).
# Wind before rain (overlapping broadband profiles; separated by lowband).
# Weedwhacker before mower (overlapping centroid ranges; weedwhacker is higher).
# Flyover after mower/diesel (catches remaining engine-like sounds that fall
#   between the more specific mechanical filters).
# Diesel after mower (lower centroid, different spectral shape).
# Conversation last (broadest catch, most overlap with other categories).
FILTER_CHAIN = [
    ("thunder", _check_thunder),
    ("impulse", _check_impulse),
    ("birdsong", _check_birdsong),
    ("amplified_bass", _check_amplified_bass),
    ("wind", _check_wind),
    ("rain", _check_rain),
    ("weedwhacker", _check_weedwhacker),
    ("mower", _check_mower),
    ("diesel", _check_diesel),
    ("flyover", _check_flyover),
    ("conversation", _check_conversation),
]

# Priority lookup derived from FILTER_CHAIN order. Lower index = higher priority.
# Used by apply_filter_holdover() to let higher-priority raw matches break
# through an active lower-priority holdover. For example, if thunder fires
# during a mower holdover, thunder wins because it's more specific and its own
# internal checks (Path B min_history, etc.) are already satisfied.
FILTER_PRIORITY = {name: idx for idx, (name, _) in enumerate(FILTER_CHAIN)}

class ModulationTracker:
    """Rolling 8-second envelope/tone state for engine-signature detection.

    Streams per-block audio into three parallel views of the last 8 seconds,
    matching the offline analysis that calibrated the thresholds in
    looks_like_engine_rev (2026-10-29, 238-clip labeled corpus):

      - a 10 ms RMS envelope (modulation spectrum source),
      - per-0.25 s-frame low-frequency tone track (fundamental + prominence),
      - per-0.25 s-frame RMS (DECISIONS D13 depth, derived from the envelope).

    The tracker is pure DSP state — no I/O, no config. The engine owns one
    across its whole run (so it is already warm with ambient audio when an
    incident opens); analyze_clip owns one per replayed file (whose first
    ~8 s therefore cannot veto — the live engine is strictly better off).

    Usage caveat: update() assumes blocks arrive contiguously at a fixed
    sample rate. Feeding it blocks with gaps dilutes the modulation spectrum
    toward zero prominence, which fails SAFE (no veto).
    """

    ENV_HOP_SEC = 0.01
    FRAME_SEC = 0.25
    WINDOW_SEC = 8.0
    LF_LO_HZ = 25.0
    LF_HI_HZ = 300.0
    TONE_PROMINENCE_MIN = 6.0

    def __init__(self):
        self._env = []          # 10 ms RMS values, newest last
        self._frames = []       # (f0_hz or None, prominence) per 0.25 s frame
        self._frame_rms = []    # RMS per 0.25 s frame (depth source)
        self._residual = np.zeros(0, dtype=np.float32)
        self._env_max = int(self.WINDOW_SEC / self.ENV_HOP_SEC)
        self._frames_max = int(self.WINDOW_SEC / self.FRAME_SEC)

    def update(self, block, sr):
        """Fold one audio block into the rolling window."""
        hop = max(1, int(sr * self.ENV_HOP_SEC))
        frame_len = int(sr * self.FRAME_SEC)
        samples = np.concatenate([self._residual, np.asarray(block, dtype=np.float32)])

        n_hops = len(samples) // hop
        usable = n_hops * hop
        if n_hops > 0:
            hopped = samples[:usable].reshape(n_hops, hop)
            self._env.extend(np.sqrt(np.mean(hopped ** 2, axis=1)).tolist())

        # Frame-level work consumes the same sample stream in 0.25 s steps.
        # Residual carries the partial frame into the next block so frame
        # boundaries stay exact regardless of block size.
        offset = 0
        while offset + frame_len <= len(samples):
            frame = samples[offset:offset + frame_len]
            self._frame_rms.append(float(np.sqrt(np.mean(frame ** 2))))
            self._frames.append(self._lf_tone(frame, sr))
            offset += frame_len
        self._residual = samples[offset:]

        if len(self._env) > self._env_max:
            self._env = self._env[-self._env_max:]
        if len(self._frames) > self._frames_max:
            self._frames = self._frames[-self._frames_max:]
            self._frame_rms = self._frame_rms[-self._frames_max:]

    def _lf_tone(self, frame, sr):
        """Dominant prominent low-frequency peak of one 0.25 s frame.

        Prominence is peak/median magnitude within the 25-300 Hz band —
        scale-free, so capture level and normalization cannot shift it.
        """
        mag = np.abs(np.fft.rfft(frame * np.hanning(len(frame))))
        freqs = np.fft.rfftfreq(len(frame), d=1.0 / sr)
        band = (freqs >= self.LF_LO_HZ) & (freqs <= self.LF_HI_HZ)
        band_mag = mag[band]
        if len(band_mag) < 3:
            return (None, 0.0)
        median = float(np.median(band_mag))
        if median <= 0:
            return (None, 0.0)
        peak_idx = int(np.argmax(band_mag))
        prominence = float(band_mag[peak_idx] / median)
        if prominence >= self.TONE_PROMINENCE_MIN:
            return (float(freqs[band][peak_idx]), prominence)
        return (None, prominence)

    def metrics(self):
        """Window metrics for looks_like_engine_rev, or None while warming up.

        Requires the full 8 s window before reporting, so the veto thresholds
        keep exactly the semantics they were calibrated with; a partial window
        coarsens the modulation spectrum and inflates prominence variance.
        """
        if len(self._env) < self._env_max or len(self._frames) < self._frames_max:
            return None

        env = np.asarray(self._env, dtype=np.float64)
        env = env - np.mean(env)
        spectrum = np.abs(np.fft.rfft(env * np.hanning(len(env))))
        mod_freqs = np.fft.rfftfreq(len(env), d=self.ENV_HOP_SEC)
        band = (mod_freqs >= 5.0) & (mod_freqs <= 45.0)
        band_spec = spectrum[band]
        median = float(np.median(band_spec))
        if median > 0:
            peak_idx = int(np.argmax(band_spec))
            mod_hz = float(mod_freqs[band][peak_idx])
            mod_prom = float(band_spec[peak_idx] / median)
        else:
            mod_hz, mod_prom = 0.0, 0.0

        env_db = 20.0 * np.log10(np.asarray(self._frame_rms) + 1e-12)
        p10, p90 = np.percentile(env_db, [10, 90])
        depth_db = float(p90 - p10)

        tonal = [f0 for f0, _prom in self._frames]
        tone_frac = float(np.mean([f0 is not None for f0 in tonal]))
        hold_pairs = pair_total = 0
        for prev_f0, now_f0 in zip(tonal, tonal[1:]):
            if prev_f0 is None or now_f0 is None:
                continue
            pair_total += 1
            if abs(now_f0 - prev_f0) / prev_f0 < 0.005:
                hold_pairs += 1
        hold_frac = hold_pairs / pair_total if pair_total else 0.0

        return {
            "depth_db": depth_db,
            "hold_frac": hold_frac,
            "mod_hz": mod_hz,
            "mod_prom": mod_prom,
            "tone_frac": tone_frac,
        }


def looks_like_engine_rev(metrics, mod_prom_min=6.0, mod_hz_min=8.0,
                          fast_mod_hz_min=10.0, fast_mod_prom_min=4.5,
                          tone_frac_min=0.80, hold_frac_min=0.65,
                          depth_immunity_db=12.0):
    """Engine-signature veto over ModulationTracker metrics — the operator's
    "that RPM is way too high to be music" insight, made measurable.

    Engines are periodic at rates music cannot reach: firing/lope/blade-pass
    amplitude modulation lands at 8-45 Hz, while music's beat energy tops out
    near 2.4 Hz with harmonics to ~9.6 Hz (16th notes at 144 BPM). Idling
    engines additionally HOLD a continuous low-frequency fundamental that
    bass lines (discrete note steps) do not. Three sub-rules, any of which
    vetoes, all gated behind a D13 depth immunity — real music through walls
    keeps >= 12 dB of envelope contrast, machines measure nearly flat:

      E: strong modulation peak at >= 8 Hz (prominence >= 6)
      G: persistent held LF tone (tone_frac >= 0.80, hold_frac >= 0.65 —
         sustained bass NOTES hold up to ~0.6; a continuous idle fundamental
         holds ~0.72, and 0.50 vetoed the locked 90 dB music regression clip)
      I: any modulation peak at >= 10 Hz with prominence >= 4.5 — above
         music's beat-harmonic ceiling even weak periodicity is mechanical

    Calibrated 2026-10-29 on the 238-clip labeled corpus (rule "J"):
    neutralizes 42/53 machine clips that score music-like while killing 5 of
    121 music-labeled clips, of which the genuinely-music losses are the
    shallow-depth hard case (the rest are mixed truck/boat clips the keyword
    labeler had called music). Ledger: .TMP_engine_rev_results.txt /
    DECISIONS D16.

    Returns False while metrics is None (tracker warming up) — the veto
    fails toward recording, never toward suppression.
    """
    if metrics is None:
        return False
    if metrics["depth_db"] >= depth_immunity_db:
        return False
    if metrics["mod_prom"] >= mod_prom_min and metrics["mod_hz"] >= mod_hz_min:
        return True
    if metrics["tone_frac"] >= tone_frac_min and metrics["hold_frac"] >= hold_frac_min:
        return True
    if metrics["mod_hz"] >= fast_mod_hz_min and metrics["mod_prom"] >= fast_mod_prom_min:
        return True
    return False


def classify_sound(mscore, features, detection_cfg, rev_metrics=None):
    """Classify a non-excluded block: music_like / engine_noise / unknown.

    Single source of truth shared by engine._classify_sound and
    reclassify.analyze_clip (previously duplicated inline in both, which is
    exactly how the two paths drift apart). Three engine checks run before
    the music threshold:

      1. Dominant midband (> engine_midband_veto, default 0.40) — engines
         concentrate energy in 180-1200 Hz; music's "smiley EQ" scoops mids.
      2. Steady bass without harmonics — flat intra-block envelope
         (cv < 0.10) with weak harmonic structure is mechanical rumble.
      3. Engine-rev periodicity veto (looks_like_engine_rev) — catches the
         REVVING truck that defeats 1 and 2: through-wall rev rumble is
         bass-dominant (midband stays low) with a dynamic envelope (cv high)
         and real firing harmonics. Gated by detection_cfg
         engine_rev_veto (default true) and the tracker having 8 s of audio.
    """
    min_music = float(detection_cfg["min_music_like_score"])
    midband_veto = float(detection_cfg.get("engine_midband_veto", 0.40))

    if features:
        if features.get("midband_ratio", 0) > midband_veto:
            return "engine_noise"

        if (features.get("lowband_ratio", 0) > 0.10 and
                features.get("envelope_cv", 0.5) < 0.10 and
                features.get("harmonic_ratio", 0.5) < 0.40):
            return "engine_noise"

    if mscore >= min_music:
        # The rev veto gates only would-be music_like blocks: its thresholds
        # were calibrated exclusively on music-scoring windows (2026-10-29
        # corpus sweep), and relabeling sub-threshold blocks would churn
        # journals (a locked birdsong regression clip flipped 'birdsong' →
        # 'birdsong+' when the veto was allowed to touch non-music blocks).
        if detection_cfg.get("engine_rev_veto", True) and looks_like_engine_rev(
                rev_metrics,
                depth_immunity_db=float(
                    detection_cfg.get("engine_rev_depth_immunity_db", 12.0))):
            return "engine_noise"
        return "music_like"
    return "unknown"


def identify_filter(features, db_history, db_now, prev_db, detection_cfg,
                    feature_history=None, rev_metrics=None):
    """Run all exclusion filters in priority order and return the first match.

    This is the single entry point the engine uses for filter identification.
    The engine passes the raw detection config dict; each filter reads its own
    keys with its own defaults. Adding a new filter requires no engine changes.

    This function is stateless — holdover (sticky classification through gaps)
    is handled separately by apply_filter_holdover(), which both the engine
    and the reclassify tool call after getting the raw result.

    Args:
        features: spectrum_features() output dict
        db_history: recent dB readings list (for stability/variance checks)
        db_now: current block dBA
        prev_db: previous block dBA
        detection_cfg: the detection section of noise_warden.yaml config
        feature_history: list of recent spectrum_features() dicts (for temporal
            pattern analysis like chorus birdsong detection). Optional; only
            used by the birdsong filter's Path D.
        rev_metrics: ModulationTracker.metrics() output (or None). Only used
            to keep engine-signature audio out of the music-family
            amplified_bass filter — see below.

    Returns:
        Filter name string (e.g. "thunder", "birdsong") or None if no filter matches.
    """
    for name, check_fn in FILTER_CHAIN:
        if check_fn(features, db_history, db_now, prev_db, detection_cfg,
                     feature_history=feature_history):
            # Engine-signature veto on the music-family filter: amplified_bass
            # gates at music score 0.45, so a revving truck scoring 0.45-0.62
            # claims it long before classify_sound ever runs, and in
            # music-focus mode those blocks count as music seconds for the
            # keep-shield (how the Oct 23/28 truck sagas survived as
            # "amplified_bass (multiple)"). A vetoed block falls through to
            # the mechanical filters (diesel, flyover, ...) instead.
            if (name == "amplified_bass"
                    and detection_cfg.get("engine_rev_veto", True)
                    and looks_like_engine_rev(
                        rev_metrics,
                        depth_immunity_db=float(detection_cfg.get(
                            "engine_rev_depth_immunity_db", 12.0)))):
                continue
            return name

    return None

def apply_filter_holdover(raw_filter, prev_filter, prev_run, gap, detection_cfg):
    """Apply holdover logic to a raw filter result from identify_filter().

    When a filter has been established for several consecutive blocks
    (prev_run >= holdover_min_run), it persists through brief gaps of up to
    holdover_max_gap unmatched blocks. During active holdover, transient
    matches from different filters (e.g., an impulse during a sustained mower)
    are also suppressed — only the established filter or a gap expiry can
    break the holdover.

    Priority override: if the raw filter is *higher priority* (earlier in
    FILTER_CHAIN) than the established holdover, it breaks through immediately.
    Each filter already enforces its own internal consistency (min_history,
    confidence thresholds, etc.), so a higher-priority match during holdover
    is genuine, not transient noise. For example, thunder Path B during a
    mower holdover: thunder's min_history is already satisfied before it fires,
    so it should not be suppressed by a less-specific mower holdover.

    Args:
        raw_filter: the result from identify_filter() (string or None)
        prev_filter: the most recent established filter (string or None)
        prev_run: how many consecutive blocks prev_filter actually matched
        gap: how many blocks since the last real match (during holdover)
        detection_cfg: config dict (reads holdover_min_run, holdover_max_gap)

    Returns:
        (effective_filter, new_prev_filter, new_prev_run, new_gap) tuple.
        Caller should store new_prev_filter/new_prev_run/new_gap for the
        next block's call.
    """
    min_run = int(detection_cfg.get("holdover_min_run", 5))
    max_gap = int(detection_cfg.get("holdover_max_gap", 10))

    holdover_active = (
        prev_filter is not None
        and prev_run >= min_run
        and gap < max_gap
    )

    # Case 1: raw match equals established filter — extend the run
    if raw_filter is not None and raw_filter == prev_filter:
        return (raw_filter, prev_filter, prev_run + 1, 0)

    # Case 2: holdover is active — persist through gaps and transient blips
    # (e.g., a brief impulse during a sustained mower run).
    # HOWEVER, filters listed in holdover_priority_breakers can break through
    # an active holdover. These are filters with strong internal consistency
    # guarantees (e.g., thunder Path B requires min_history blocks of sustained
    # matching, flyover requires 4+ blocks) that should not be suppressed by
    # an unrelated holdover. The breaker's own internal checks serve as the
    # quality gate. Two constraints apply:
    #   1. If the current holdover filter is ALSO a breaker AND higher-priority,
    #      it wins — this prevents flyover from breaking thunder's holdover
    #      while still allowing flyover to break wind's holdover.
    #   2. A breaker never "breaks" its own holdover (same filter = extend run).
    if holdover_active:
        if raw_filter is not None:
            breakers_str = detection_cfg.get(
                "holdover_priority_breakers", "thunder",
            )
            breakers = {
                b.strip() for b in str(breakers_str).split(",") if b.strip()
            }
            if raw_filter in breakers and raw_filter != prev_filter:
                # If the holdover filter is also a breaker, only let the
                # higher-priority (lower index) breaker win.
                if prev_filter in breakers:
                    raw_pri = FILTER_PRIORITY.get(raw_filter, len(FILTER_CHAIN))
                    prev_pri = FILTER_PRIORITY.get(prev_filter, len(FILTER_CHAIN))
                    if raw_pri < prev_pri:
                        return (raw_filter, raw_filter, 1, 0)
                else:
                    # Holdover filter is NOT a breaker — the incoming breaker
                    # wins regardless of priority (flyover breaks wind, etc.)
                    return (raw_filter, raw_filter, 1, 0)

        return (prev_filter, prev_filter, prev_run, gap + 1)

    # Case 3: new filter (no holdover to override it) — start tracking
    if raw_filter is not None:
        return (raw_filter, raw_filter, 1, 0)

    # Case 4: nothing matches, no holdover
    return (None, None, 0, 0)

# Default detection latency (min_history) for each filter. Path A thunder and
# impulse are instant detectors (0), but thunder Path B requires min_history
# so its latency is set to 6 for journal backdating.
_FILTER_DEFAULT_LATENCY = {
    "birdsong": 8,
    "conversation": 10,
    "diesel": 8,
    "flyover": 4,
    "mower": 6,
    "rain": 6,
    "thunder": 6,
    "weedwhacker": 6,
    "wind": 6,
}

# Config keys that override the default min_history for certain filters.
_FILTER_LATENCY_CONFIG_KEYS = {
    "birdsong": "birdsong_min_history",
    "conversation": "conversation_min_history",
    "diesel": "diesel_min_history",
    "flyover": "flyover_min_history",
    "thunder": "thunder_rumble_min_history",
    "wind": "wind_min_history",
}

def get_filter_detection_latency(filter_name, detection_cfg):
    """Return the detection latency (in blocks) for a given filter.

    Sustained-pattern filters require min_history blocks of data before they
    can make a confident identification. When a filter first triggers, the
    sound pattern was present for at least this many blocks prior. Used to
    backdate journal entries so the timeline reflects when the source actually
    started, not when the system had enough data to confirm it.

    Args:
        filter_name: name from FILTER_CHAIN (e.g. "mower", "birdsong")
        detection_cfg: the detection section of noise_warden.yaml

    Returns:
        Integer number of blocks (0 for instant detectors like impulse/thunder,
        or for unrecognized names like music/music_like/unknown).
    """
    config_key = _FILTER_LATENCY_CONFIG_KEYS.get(filter_name)
    if config_key and config_key in detection_cfg:
        return int(detection_cfg[config_key])
    return _FILTER_DEFAULT_LATENCY.get(filter_name, 0)
