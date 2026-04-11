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

def _inter_block_beat_confidence(db_history):
    """Detect slow macro-level amplitude periodicity across multiple blocks.

    Autocorrelates the dB history at lags of 2–8 blocks. At 1 block/sec,
    this detects patterns repeating every 2–8 seconds (7.5–30 BPM range) —
    NOT actual musical beats, but structural dynamics like verse/chorus
    volume changes, DJ drops, or pulsing alarms of the particularly
    obnoxious variety.

    This is the slower complement to intra_block_beat_confidence(), which
    detects actual musical tempo within each audio block. The two are
    combined via max() in beat_confidence().

    Constants:
      8   — minimum readings to analyze. Below this there isn't enough data
            for meaningful correlation. At 1 block/sec, this is 8 seconds.
      24  — analysis window (last 24 blocks).
      2–8 — lag range in blocks. At 1 block/sec:
            lag 2 = 30 BPM (repeats every 2 seconds)
            lag 8 = 7.5 BPM (repeats every 8 seconds)
      Normalized autocorrelation is in [0, +1] because we take the max
            of positive correlations only. 0 = no pattern, 1.0 = perfect
            repetition.
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
    return max(0.0, min(1.0, best))


def intra_block_beat_confidence(block, sr):
    """Detect rhythmic beat patterns within a single audio block.

    Computes a short-time RMS energy envelope at 10ms hop, then
    autocorrelates at lags corresponding to common musical tempos
    (80–180 BPM). This catches the actual thump-thump-thump that
    the inter-block approach cannot see — at 1-second block resolution,
    a 120 BPM track has 2 beats per block, which averages to a flat
    dB reading and produces no inter-block periodicity.

    Real-world validation:
        bass music through walls: median 0.77, detects ~143 BPM
        rain:                     median 0.50 (no rhythm — baseline)
        birdsong chorus:          median 0.50 (no rhythm)
        diesel engine:            median 0.90 (engine firing cycle is
                                  genuinely periodic — correct)

    Constants:
      10ms hop — 100 envelope frames per second at any sample rate.
            Fine enough to resolve 180 BPM (beat every 333ms = 33 frames).
      80–180 BPM range — covers the vast majority of popular music.
            At 10ms hop: 80 BPM → lag 75, 180 BPM → lag 33.
      max_lag capped at n_frames/2 — autocorrelation is unreliable
            when lag approaches signal length.
      Output is in [0, 1] — raw normalized autocorrelation is already
            in [-1, +1], and we take the max of positive correlations only.
            0 = no pattern, 1.0 = perfect periodic amplitude at some tempo.

    Args:
        block: raw audio samples (numpy array, float32)
        sr: sample rate in Hz
    """
    hop = int(sr * 0.01)  # 10ms hop
    n_frames = len(block) // hop

    if n_frames < 20:
        return 0.0

    # RMS energy per frame — captures amplitude envelope
    envelope = np.array([
        np.sqrt(np.mean(block[i * hop:(i + 1) * hop] ** 2))
        for i in range(n_frames)
    ])

    env_mean = np.mean(envelope)
    if env_mean < 1e-8:
        return 0.0  # silence

    # Guard: if amplitude variation is negligible relative to mean energy,
    # there's no beat to detect. A pure tone or constant noise has tiny
    # float-precision jitter that, when autocorrelated and normalized,
    # produces spuriously high correlation. CV < 5% → steady signal.
    if np.std(envelope) / env_mean < 0.05:
        return 0.0

    envelope = envelope - env_mean

    # Lag range for 80–180 BPM at 10ms hop resolution
    min_lag = int(60.0 / 180.0 / 0.01)  # 33 frames = 180 BPM
    max_lag = min(n_frames // 2, int(60.0 / 80.0 / 0.01))  # 75 frames = 80 BPM

    if min_lag >= max_lag or max_lag >= n_frames:
        return 0.0

    best = 0.0
    for lag in range(min_lag, max_lag + 1):
        a = envelope[:-lag]
        b = envelope[lag:]
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
        corr = float(np.dot(a, b) / denom)
        best = max(best, corr)

    return max(0.0, min(1.0, best))


def beat_confidence(block, sr, db_history):
    """Beat confidence from intra-block rhythm analysis.

    Uses only the intra-block component: short-time RMS envelope
    autocorrelated at 80–180 BPM lags. This detects actual musical
    tempo within each audio block.

    The inter-block component (_inter_block_beat_confidence) was removed
    from this combined function because it measures dB-level *stability*,
    not rhythm. Any steady source (mower, rain, constant HVAC) produces
    high inter-block autocorrelation simply because the dB doesn't change
    much between blocks — inflating scores for non-musical sources:
        gas mower:   0.805 inter (steady dB, not musical)
        thunder-rain: 0.773 inter (steady rain dB)
        bass music:   0.056 inter (irrelevant — rhythm is intra-block)
    The inter-block function is retained for reference but no longer
    contributes to the combined score.

    The db_history parameter is accepted but unused, preserving the
    call signature for engine.py and reclassify.py callers.

    Args:
        block: raw audio samples for the current block (numpy array)
        sr: sample rate in Hz
        db_history: list of recent dB readings (unused; kept for API compat)
    """
    return intra_block_beat_confidence(block, sr)

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
                       lowband_min=0.55, flatness_min=0.45,
                       recent_db=None, rumble_centroid_max=1300,
                       rumble_flatness_max=0.15, rumble_midband_min=0.40,
                       rumble_min_db=95.0, rumble_min_history=6,
                       rumble_window=12):
    """Detect thunder via two paths: sharp crack or sustained rumble.

    Path A (original) — single-block impulse with dominant bass:
      Requires a sharp dB spike (≥ delta_threshold) with concentrated
      low-frequency energy (lowband > 0.55) and broadband character
      (flatness > 0.45). Catches close lightning strikes and sharp cracks.

    Path B (sustained rumble) — mellow storm thunder:
      Recorded thunderstorms (especially mellow/distant ones) don't produce
      the sharp 18+ dB spikes that Path A requires. Instead, they ramp up
      over 2–3 seconds with centroid plummeting below 1300 Hz, extremely
      concentrated energy (flatness < 0.15 — the opposite of Path A's
      broadband requirement), and dominant midband from the rumble body.
      The key differentiator from mower: mower flatness is always ≥ 0.25,
      while thunder rumble flatness is < 0.15. A minimum dB floor (95 dBA)
      and min_history requirement prevent quiet ambient sounds from matching.

    Args:
        features: spectrum_features() output dict
        db_now: current block dBA
        db_prev: previous block dBA
        delta_threshold: minimum dB jump for Path A (config: thunder_peak_delta_db)
        lowband_min: minimum lowband_ratio for Path A (default 0.55)
        flatness_min: minimum spectral flatness for Path A (default 0.45)
        recent_db: recent dB history for Path B stability check
        rumble_centroid_max: max centroid for Path B (default 1300 Hz)
        rumble_flatness_max: max flatness for Path B (default 0.15)
        rumble_midband_min: min midband ratio for Path B (default 0.40)
        rumble_min_db: minimum dBA for Path B (default 95.0)
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
                              beat_confidence=None, min_beat_confidence=0.0,
                              flatness_min=0.20):
    """Bass-heavy music — the neighborhood thump filter.

    Detects amplified music with boosted bass in two scenarios:

    Through walls/garage (strong bass, attenuated highs):
        lowband:  0.36–0.61 (median 0.43)
        centroid: 1833–3301 Hz
        flatness: 0.18–0.33
        mscore:   0.69–0.79 (median 0.73, all blocks ≥ 0.60)
        bconf:    0.04–0.86 (median 0.54)

    Open windows/doors (broader spectrum, less bass-dominant):
        lowband:  0.09–0.44 (median 0.19)
        centroid: 2131–4678 Hz
        flatness: 0.21–0.44 (median 0.28)
        mscore:   0.40–0.73 (median 0.50)
        bconf:    0.00–0.66 (median 0.15, truck overlay kills rhythm)

    HOWEVER, comma, this filter MUST run before rain and mower because those
    filters would steal bass-music blocks without the music score guard or
    this dedicated filter.

    The flatness floor (≥ 0.20) is critical for diesel separation — diesel
    engines have flatness median 0.151 (max 0.285), while bass music always
    exceeds 0.20 (min 0.207 across all recordings). This replaced beat
    confidence as the diesel guard after open-window recordings showed that
    overlapping noise sources (e.g., truck + music) destroy rhythm detection.

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
        beat_confidence: pre-computed beat confidence for the current block.
            If provided and min_beat_confidence > 0, must meet threshold.
            None skips the check (backward-compatible).
        min_beat_confidence: minimum beat confidence threshold (default 0.0).
            Disabled by default because overlapping noise sources (truck +
            music) destroy rhythm detection in open-window scenarios. The
            flatness floor handles diesel separation instead.
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

    # Beat confidence gate — music has rhythm. Disabled by default (threshold
    # 0.0) because truck/wind overlays destroy rhythm detection. The flatness
    # floor now handles diesel separation. Retained as optional strengthening.
    if (beat_confidence is not None and min_beat_confidence > 0 and
            beat_confidence < min_beat_confidence):
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

def _check_thunder(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
    return looks_like_thunder(
        features, db_now, prev_db,
        float(det.get("thunder_peak_delta_db", 18.0)),
        lowband_min=float(det.get("thunder_lowband_min", 0.55)),
        flatness_min=float(det.get("thunder_flatness_min", 0.45)),
        recent_db=db_history,
        rumble_centroid_max=int(det.get("thunder_rumble_centroid_max", 1300)),
        rumble_flatness_max=float(det.get("thunder_rumble_flatness_max", 0.15)),
        rumble_midband_min=float(det.get("thunder_rumble_midband_min", 0.40)),
        rumble_min_db=float(det.get("thunder_rumble_min_db", 95.0)),
        rumble_min_history=int(det.get("thunder_rumble_min_history", 6)),
        rumble_window=int(det.get("thunder_rumble_window", 12)),
    )

def _check_impulse(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
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

def _check_birdsong(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
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

def _check_amplified_bass(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
    return looks_like_amplified_bass(
        features, db_history,
        min_music_score=float(det.get("amplified_bass_min_music_score", 0.45)),
        lowband_min=float(det.get("amplified_bass_lowband_min", 0.16)),
        centroid_max=float(det.get("amplified_bass_centroid_max_hz", 4000)),
        env_std_max=float(det.get("amplified_bass_env_std_max", 3.0)),
        min_history=int(det.get("amplified_bass_min_history", 6)),
        beat_confidence=beat_confidence,
        min_beat_confidence=float(det.get("amplified_bass_min_beat_confidence", 0.0)),
        flatness_min=float(det.get("amplified_bass_flatness_min", 0.20)),
    )

def _check_rain(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
    return looks_like_rain(
        features, db_history,
        float(det.get("rain_flatness_threshold", 0.27)),
        float(det.get("rain_low_variance_db", 1.5)),
        lowband_min=float(det.get("rain_lowband_min", 0.07)),
        centroid_max=float(det.get("rain_centroid_max_hz", 5000)),
        max_music_score=float(det.get("rain_max_music_score", 0.70)),
    )

def _check_weedwhacker(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
    return looks_like_weedwhacker(
        features, db_history,
        centroid_min=float(det.get("weedwhacker_centroid_min_hz", 2000)),
        centroid_max=float(det.get("weedwhacker_centroid_max_hz", 6000)),
        flatness_min=float(det.get("weedwhacker_flatness_min", 0.50)),
        lowband_max=float(det.get("weedwhacker_lowband_max", 0.15)),
        env_std_max=float(det.get("weedwhacker_env_std_max", 5.0)),
    )

def _check_mower(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
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

def _check_diesel(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
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

def _check_conversation(features, db_history, db_now, prev_db, det, feature_history=None, beat_confidence=None):
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
# Amplified bass before rain/mower (bass music mimics their steady profiles;
#   the music score guard on rain/mower provides defense-in-depth, but this
#   dedicated filter gives the recording a proper classification label).
# Weedwhacker before mower (overlapping centroid ranges; weedwhacker is higher).
# Diesel after mower (lower centroid, different spectral shape).
# Conversation last (broadest catch, most overlap with other categories).
FILTER_CHAIN = [
    ("thunder", _check_thunder),
    ("impulse", _check_impulse),
    ("birdsong", _check_birdsong),
    ("amplified_bass", _check_amplified_bass),
    ("rain", _check_rain),
    ("weedwhacker", _check_weedwhacker),
    ("mower", _check_mower),
    ("diesel", _check_diesel),
    ("conversation", _check_conversation),
]

# Priority lookup derived from FILTER_CHAIN order. Lower index = higher priority.
# Used by apply_filter_holdover() to let higher-priority raw matches break
# through an active lower-priority holdover. For example, if thunder fires
# during a mower holdover, thunder wins because it's more specific and its own
# internal checks (Path B min_history, etc.) are already satisfied.
FILTER_PRIORITY = {name: idx for idx, (name, _) in enumerate(FILTER_CHAIN)}


def identify_filter(features, db_history, db_now, prev_db, detection_cfg,
                    feature_history=None, beat_confidence=None):
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
        beat_confidence: pre-computed beat confidence for the current block.
            Optional; only used by the amplified_bass filter to verify that
            bass-heavy signals actually have rhythmic content.

    Returns:
        Filter name string (e.g. "thunder", "birdsong") or None if no filter matches.
    """
    for name, check_fn in FILTER_CHAIN:
        if check_fn(features, db_history, db_now, prev_db, detection_cfg,
                     feature_history=feature_history,
                     beat_confidence=beat_confidence):
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
    # a lower-priority holdover. These are filters with strong internal
    # consistency guarantees (e.g., thunder Path B requires min_history blocks
    # of sustained matching) that should not be suppressed by a less-specific
    # holdover. The check also requires higher priority (earlier in
    # FILTER_CHAIN) to prevent, say, conversation from overriding mower.
    if holdover_active:
        if raw_filter is not None:
            breakers_str = detection_cfg.get(
                "holdover_priority_breakers", "thunder",
            )
            breakers = {
                b.strip() for b in str(breakers_str).split(",") if b.strip()
            }
            if raw_filter in breakers:
                raw_pri = FILTER_PRIORITY.get(raw_filter, len(FILTER_CHAIN))
                prev_pri = FILTER_PRIORITY.get(prev_filter, len(FILTER_CHAIN))
                if raw_pri < prev_pri:
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
    "mower": 6,
    "rain": 6,
    "thunder": 6,
    "weedwhacker": 6,
}

# Config keys that override the default min_history for certain filters.
_FILTER_LATENCY_CONFIG_KEYS = {
    "birdsong": "birdsong_min_history",
    "conversation": "conversation_min_history",
    "diesel": "diesel_min_history",
    "thunder": "thunder_rumble_min_history",
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
