"""Pure signal-processing functions for noise classification.

Pipeline overview (called per audio block in the engine's main loop):
  1. rms_dbfs        → raw amplitude in dBFS
  2. dba_estimate    → calibrated dBA via mic offset
  3. spectrum_features → band-energy ratios, spectral centroid, flatness
  4. beat_confidence  → autocorrelation-based rhythmicity score
  5. music_like_score → composite heuristic combining bass energy + tonality
  6. Exclusion filters (impulse, thunder, rain, mower, birdsong)

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

def spectrum_features(arr: np.ndarray, sr: int):
    """Extract spectral features from a single audio block.

    Returns a dict with:
      centroid_hz    — amplitude-weighted mean frequency (brightness indicator)
      flatness       — geometric/arithmetic mean ratio of magnitudes (0=tonal, 1=noise)
      lowband_ratio  — energy fraction in 30–180 Hz (bass/kick fundamentals)
      midband_ratio  — energy fraction in 180–1200 Hz (voice, guitar body, melody)
      highband_ratio — energy fraction above 1200 Hz (sibilance, cymbals, birdsong)

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
    return {
        "centroid_hz": centroid,
        "flatness": flatness,
        "lowband_ratio": float(low / total),
        "midband_ratio": float(mid / total),
        "highband_ratio": float(high / total),
    }

def beat_confidence_from_history(db_history):
    """Estimate rhythmic regularity via autocorrelation of recent dB readings.

    This is a heuristic, not a proper BPM detector. It looks for repeating
    amplitude patterns over short lag intervals — an oscillating loud/quiet
    pattern (like a beat) produces high correlation at the beat's lag.

    Constants:
      8   — minimum readings to analyze. Below this there isn't enough data
            for meaningful correlation. At 1 block/sec, this is 8 seconds.
      24  — analysis window (last 24 blocks). Long enough to capture ~6 bars
            of a typical 120 BPM track (beat every 2 blocks at 1 block/sec),
            short enough to stay responsive to changing audio.
      2–8 — lag range in blocks. At 1 block/sec:
            lag 2 = 120 BPM (2 blocks between beats)
            lag 8 = 30 BPM (very slow, catches half-time patterns)
            Lags below 2 are too noisy; above 8 rarely appear as beats.
      (+1)/2 normalization — raw autocorrelation is in [-1, +1]. This
            maps it to [0, 1] where 0 = anti-correlated, 0.5 = no pattern,
            1.0 = perfect repetition.
    """
    if len(db_history) < 8:
        return 0.0
    x = np.array(db_history[-24:], dtype=float)
    x = x - np.mean(x)
    if np.allclose(x, 0):
        return 0.0
    best = 0.0
    for lag in range(2, min(8, len(x)-1)):
        a = x[:-lag]
        b = x[lag:]
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
        corr = float(np.dot(a, b) / denom)
        best = max(best, corr)
    return max(0.0, min(1.0, (best + 1.0) / 2.0))

def music_like_score(features: dict):
    """Composite heuristic: how much does this sound resemble music?

    Combines two signals:
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

    Weighting: 0.6 * bass + 0.4 * tonal. Bass is weighted higher because
    it's the more reliable indicator (most non-music sounds lack sustained
    low-frequency energy). Tonal helps disambiguate bass-heavy non-music
    (truck idle at 0.60 flatness) from actual music.

    Final score is clamped to [0, 1].
    """
    # 1.6x boost maps typical music lowband (0.30–0.50) into the 0.48–0.80 range
    low = max(0.0, min(1.0, features["lowband_ratio"] * 1.6))
    # Triangle peaking at flatness=0.35, zero at 0.0 and 0.70
    tonal_window = max(0.0, 1.0 - abs(features["flatness"] - 0.35) / 0.35)
    return max(0.0, min(1.0, 0.6 * low + 0.4 * tonal_window))

def is_impulse(db_now, db_prev, delta_threshold):
    """Single-block transient: dB jump from previous block exceeds threshold.

    The threshold is a config parameter (default 14 dB). This fires for any
    sudden spike — gunshot, door slam, firework, or the leading edge of
    thunder. Thunder is checked first in the filter priority chain to avoid
    labeling it as a generic impulse.
    """
    return (db_now - db_prev) >= delta_threshold

def looks_like_thunder(features, db_now, db_prev, delta_threshold,
                       lowband_min=0.55, flatness_min=0.45):
    """Impulse with dominant low-frequency, broadband character.

    Thunder is distinguished from other impulses by two additional checks:
      lowband_min (default 0.55) — thunder energy is concentrated below
        180 Hz. A ratio above 0.55 means more than half the energy is in
        the bass band, which is characteristic of thunder rumble. Fireworks
        and door slams have more midrange content.
      flatness_min (default 0.45) — thunder is spectrally broad (rumble
        across many frequencies), not tonal. 0.45 is well above music
        (0.25–0.40) but below rain (0.72+). This rejects bass-heavy music
        bursts that might otherwise match on lowband alone.

    Args:
        features: spectrum_features() output dict
        db_now: current block dBA
        db_prev: previous block dBA
        delta_threshold: minimum dB jump (config: thunder_peak_delta_db, default 18)
        lowband_min: minimum lowband_ratio for thunder classification (default 0.55)
        flatness_min: minimum spectral flatness for thunder (default 0.45)
    """
    return ((db_now - db_prev) >= delta_threshold and
            features["lowband_ratio"] > lowband_min and
            features["flatness"] > flatness_min)

def looks_like_rain(features, recent_db, flatness_threshold, variance_db,
                    min_history=6, window=12):
    """Steady broadband noise with very low amplitude variation.

    Rain produces a flat spectrum (flatness typically 0.72+) with almost
    no amplitude fluctuation (std dev < 2.5 dB over several seconds).

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        flatness_threshold: minimum spectral flatness (config: rain_flatness_threshold)
        variance_db: maximum dB std dev (config: rain_low_variance_db)
        min_history: minimum readings before filter activates (default 6)
        window: number of recent readings to evaluate (default 12)
    """
    if len(recent_db) < min_history:
        return False
    arr = np.array(recent_db[-window:], dtype=float)
    return features["flatness"] >= flatness_threshold and float(np.std(arr)) <= variance_db

def looks_like_mower(features, recent_db, flatness_threshold, cmin, cmax,
                     env_std_max=4.5, min_history=6, window=12, min_db=70.0,
                     db_now=None):
    """Sustained mechanical drone in the 300–4000 Hz range.

    Mowers/blowers produce a mid-frequency drone with moderate flatness and
    fairly stable amplitude. Real-world calibration showed flatness as low as
    0.26 during steady operation and centroid up to 3450 Hz.

    A minimum dB floor rejects quiet sources (fans, HVAC) whose spectral shape
    mimics a mower. Real mowers at any meaningful distance produce 70+ dBA;
    computer fans and indoor equipment sit at 55–65 dBA.

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
    """
    if len(recent_db) < min_history:
        return False

    # Quiet sources (fans, HVAC) can mimic mower spectrally but are far too soft
    if db_now is not None and db_now < min_db:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))
    return (
        features["flatness"] >= flatness_threshold and
        cmin <= features["centroid_hz"] <= cmax and
        env_std <= env_std_max
    )

def looks_like_birdsong(features, recent_db, highband_min=0.70, lowband_max=0.15,
                        flatness_min=0.30, variance_max=3.0, min_history=8,
                        peak_highband_min=0.89, peak_centroid_min=2800.0,
                        peak_db_threshold=10.0, peak_variance_min=8.0,
                        purity_highband_min=0.95, purity_db_margin=15.0):
    """Detect birdsong: high-frequency energy with minimal bass.

    Uses three detection paths to handle different bird call patterns:

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

def looks_like_diesel(features, recent_db, centroid_max=400.0,
                      lowband_min=0.45, flatness_min=0.40,
                      flatness_max=0.65, env_std_max=3.0,
                      min_history=8, window=12):
    """Detect diesel engine idle / heavy truck rumble: sustained low-frequency drone.

    Diesel idle sounds like a steady bass rumble without the sharp onset of
    thunder (no dB spike). Key differences from similar categories:
      vs. thunder — diesel is sustained with low amplitude variance; thunder
        requires a sudden dB jump. Thunder is checked first in priority, so
        diesel only fires for non-impulsive sounds.
      vs. mower — diesel has a lower centroid (below 400 Hz) and heavier
        lowband energy (0.45+ vs mower's typical 0.30). Mowers peak in the
        300–3000 Hz midrange.
      vs. rain — diesel has lower flatness (0.40–0.65 vs rain's 0.72+) and
        more concentrated low-frequency energy.

    Args:
        features: spectrum_features() output dict
        recent_db: last N dB readings
        centroid_max: maximum centroid Hz (default 400 — diesel rumble is low)
        lowband_min: minimum lowband_ratio (default 0.45 — bass-dominant)
        flatness_min: minimum spectral flatness (default 0.40 — broadband rumble)
        flatness_max: maximum spectral flatness (default 0.65 — below rain territory)
        env_std_max: maximum dB std dev (default 3.0 — diesel idle is very steady)
        min_history: minimum readings before filter activates (default 8)
        window: number of recent readings to evaluate (default 12)
    """
    if len(recent_db) < min_history:
        return False

    env_std = float(np.std(np.array(recent_db[-window:], dtype=float)))

    return (
        features["centroid_hz"] <= centroid_max and
        features["lowband_ratio"] >= lowband_min and
        flatness_min <= features["flatness"] <= flatness_max and
        env_std <= env_std_max
    )

def looks_like_conversation(features, recent_db, centroid_min=500.0,
                            centroid_max=2500.0, lowband_max=0.35,
                            flatness_max=0.55, env_std_min=4.0,
                            env_std_max=8.0, db_range_max=15.0,
                            min_history=10, window=12):
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

    Caveats:
      - Single speakers are easier to detect than large groups (groups approach
        broadband noise and may trigger rain filter instead).
      - Music with vocals can overlap this range — the music_like_score check
        in the engine runs first and catches those.
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
    """
    if len(recent_db) < min_history:
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

def _check_thunder(features, db_history, db_now, prev_db, det):
    return looks_like_thunder(
        features, db_now, prev_db,
        float(det.get("thunder_peak_delta_db", 18.0)),
        lowband_min=float(det.get("thunder_lowband_min", 0.55)),
        flatness_min=float(det.get("thunder_flatness_min", 0.45)),
    )

def _check_impulse(features, db_history, db_now, prev_db, det):
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

def _check_birdsong(features, db_history, db_now, prev_db, det):
    return looks_like_birdsong(
        features, db_history,
        highband_min=float(det.get("birdsong_highband_min", 0.70)),
        lowband_max=float(det.get("birdsong_lowband_max", 0.15)),
        flatness_min=float(det.get("birdsong_flatness_min", 0.30)),
        variance_max=float(det.get("birdsong_amplitude_std_max", 3.0)),
        min_history=int(det.get("birdsong_min_history", 8)),
        peak_highband_min=float(det.get("birdsong_peak_highband_min", 0.89)),
        peak_centroid_min=float(det.get("birdsong_peak_centroid_min", 2800.0)),
        peak_db_threshold=float(det.get("birdsong_peak_db_threshold", 10.0)),
        peak_variance_min=float(det.get("birdsong_peak_variance_min", 8.0)),
        purity_highband_min=float(det.get("birdsong_purity_highband_min", 0.95)),
        purity_db_margin=float(det.get("birdsong_purity_db_margin", 15.0)),
    )

def _check_rain(features, db_history, db_now, prev_db, det):
    return looks_like_rain(
        features, db_history,
        float(det.get("rain_flatness_threshold", 0.72)),
        float(det.get("rain_low_variance_db", 2.5)),
    )

def _check_weedwhacker(features, db_history, db_now, prev_db, det):
    return looks_like_weedwhacker(
        features, db_history,
        centroid_min=float(det.get("weedwhacker_centroid_min_hz", 2000)),
        centroid_max=float(det.get("weedwhacker_centroid_max_hz", 6000)),
        flatness_min=float(det.get("weedwhacker_flatness_min", 0.50)),
        lowband_max=float(det.get("weedwhacker_lowband_max", 0.15)),
        env_std_max=float(det.get("weedwhacker_env_std_max", 5.0)),
    )

def _check_mower(features, db_history, db_now, prev_db, det):
    return looks_like_mower(
        features, db_history,
        float(det.get("mower_flatness_threshold", 0.25)),
        float(det.get("mower_centroid_min_hz", 300)),
        float(det.get("mower_centroid_max_hz", 4000)),
        env_std_max=float(det.get("mower_env_std_max", 4.5)),
        min_db=float(det.get("mower_min_db", 70.0)),
        db_now=db_now,
    )

def _check_diesel(features, db_history, db_now, prev_db, det):
    return looks_like_diesel(
        features, db_history,
        centroid_max=float(det.get("diesel_centroid_max_hz", 400)),
        lowband_min=float(det.get("diesel_lowband_min", 0.45)),
        flatness_min=float(det.get("diesel_flatness_min", 0.40)),
        flatness_max=float(det.get("diesel_flatness_max", 0.65)),
        env_std_max=float(det.get("diesel_env_std_max", 3.0)),
        min_history=int(det.get("diesel_min_history", 8)),
    )

def _check_conversation(features, db_history, db_now, prev_db, det):
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
    )


# Priority-ordered filter chain. More specific patterns first, broadest last.
# Thunder before impulse (thunder IS an impulse, but more descriptive).
# Birdsong before weedwhacker (birdsong is more specific high-freq pattern).
# Weedwhacker before mower (overlapping centroid ranges; weedwhacker is higher).
# Diesel after mower (lower centroid, different spectral shape).
# Conversation last (broadest catch, most overlap with other categories).
FILTER_CHAIN = [
    ("thunder", _check_thunder),
    ("impulse", _check_impulse),
    ("birdsong", _check_birdsong),
    ("rain", _check_rain),
    ("weedwhacker", _check_weedwhacker),
    ("mower", _check_mower),
    ("diesel", _check_diesel),
    ("conversation", _check_conversation),
]


def identify_filter(features, db_history, db_now, prev_db, detection_cfg):
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

    Returns:
        Filter name string (e.g. "thunder", "birdsong") or None if no filter matches.
    """
    for name, check_fn in FILTER_CHAIN:
        if check_fn(features, db_history, db_now, prev_db, detection_cfg):
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
    # (e.g., a brief impulse during a sustained mower run)
    if holdover_active:
        return (prev_filter, prev_filter, prev_run, gap + 1)

    # Case 3: new filter (no holdover to override it) — start tracking
    if raw_filter is not None:
        return (raw_filter, raw_filter, 1, 0)

    # Case 4: nothing matches, no holdover
    return (None, None, 0, 0)


# Default detection latency (min_history) for each filter. Filters without
# a min_history requirement (thunder, impulse) are instant detectors.
_FILTER_DEFAULT_LATENCY = {
    "birdsong": 8,
    "conversation": 10,
    "diesel": 8,
    "mower": 6,
    "rain": 6,
    "weedwhacker": 6,
}

# Config keys that override the default min_history for certain filters.
_FILTER_LATENCY_CONFIG_KEYS = {
    "birdsong": "birdsong_min_history",
    "conversation": "conversation_min_history",
    "diesel": "diesel_min_history",
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
