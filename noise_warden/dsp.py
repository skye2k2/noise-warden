from __future__ import annotations
import numpy as np

def rms_dbfs(arr: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(arr))) + 1e-12)
    return 20.0 * np.log10(rms + 1e-12)

def dba_estimate(dbfs: float, calibration_offset_db: float) -> float:
    return dbfs + calibration_offset_db

def spectrum_features(arr: np.ndarray, sr: int):
    win = np.hanning(len(arr))
    x = arr * win
    fft = np.fft.rfft(x)
    mag = np.abs(fft) + 1e-12
    freqs = np.fft.rfftfreq(len(x), d=1.0/sr)

    centroid = float(np.sum(freqs * mag) / np.sum(mag))
    geo = np.exp(np.mean(np.log(mag)))
    arith = np.mean(mag)
    flatness = float(geo / (arith + 1e-12))

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
    # Simple autocorrelation-inspired heuristic on dB deltas (still heuristic, but less silly than volatility-only)
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
    # Documented heuristic: strong low-band energy + not-too-flat spectrum
    low = max(0.0, min(1.0, features["lowband_ratio"] * 1.6))
    tonal_window = max(0.0, 1.0 - abs(features["flatness"] - 0.35) / 0.35)
    return max(0.0, min(1.0, 0.6 * low + 0.4 * tonal_window))

def is_impulse(db_now, db_prev, delta_threshold):
    return (db_now - db_prev) >= delta_threshold

def looks_like_thunder(features, db_now, db_prev, delta_threshold):
    return (db_now - db_prev) >= delta_threshold and features["lowband_ratio"] > 0.55 and features["flatness"] > 0.45

def looks_like_rain(features, recent_db, flatness_threshold, variance_db):
    if len(recent_db) < 6:
        return False
    arr = np.array(recent_db[-12:], dtype=float)
    return features["flatness"] >= flatness_threshold and float(np.std(arr)) <= variance_db

def looks_like_mower(features, recent_db, flatness_threshold, cmin, cmax):
    if len(recent_db) < 6:
        return False
    env_std = float(np.std(np.array(recent_db[-12:], dtype=float)))
    return (
        features["flatness"] >= flatness_threshold and
        cmin <= features["centroid_hz"] <= cmax and
        env_std <= 3.5
    )
