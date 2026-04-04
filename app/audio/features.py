from __future__ import annotations
import numpy as np
from scipy.signal import get_window
from app.models import AudioFeatures


class FeatureExtractor:
    def __init__(self, fs: int, bass_low: float, bass_high: float, mower_low: float, mower_high: float):
        self.fs = fs
        self.prev_mag = None
        self.slow_db = None
        self.fast_db = None
        self.bass_low = bass_low
        self.bass_high = bass_high
        self.mower_low = mower_low
        self.mower_high = mower_high

    def _db(self, x: np.ndarray) -> float:
        rms = np.sqrt(np.mean(np.square(x)) + 1e-12)
        return 20.0 * np.log10(rms + 1e-12)

    def _ema(self, prev: float | None, value: float, alpha: float) -> float:
        return value if prev is None else (alpha * value + (1 - alpha) * prev)

    def extract(self, x: np.ndarray) -> AudioFeatures:
        # level
        rms_db = self._db(x)
        self.slow_db = self._ema(self.slow_db, rms_db, 0.15)   # rough SLOW-ish
        self.fast_db = self._ema(self.fast_db, rms_db, 0.45)   # rough FAST-ish

        # spectrum
        n = len(x)
        w = get_window("hann", n, fftbins=True)
        X = np.fft.rfft(x * w)
        mag = np.abs(X) + 1e-12
        freqs = np.fft.rfftfreq(n, d=1.0 / self.fs)

        norm = mag / np.sum(mag)
        centroid = float(np.sum(freqs * norm))
        bandwidth = float(np.sqrt(np.sum(((freqs - centroid) ** 2) * norm)))

        gm = np.exp(np.mean(np.log(mag)))
        am = np.mean(mag)
        flatness = float(gm / (am + 1e-12))

        if self.prev_mag is None:
            flux = 0.0
        else:
            flux = float(np.mean(np.maximum(mag - self.prev_mag, 0.0)))
        self.prev_mag = mag

        bass_mask = (freqs >= self.bass_low) & (freqs <= self.bass_high)
        mower_mask = (freqs >= self.mower_low) & (freqs <= self.mower_high)
        total_energy = float(np.sum(mag))
        bass_energy = float(np.sum(mag[bass_mask]))
        mower_energy = float(np.sum(mag[mower_mask]))
        bass_ratio = bass_energy / (total_energy + 1e-12)
        tonal_ratio = mower_energy / (total_energy + 1e-12)

        return AudioFeatures(
            rms_db=rms_db,
            slow_db=self.slow_db,
            fast_db=self.fast_db,
            spectral_centroid_hz=centroid,
            spectral_bandwidth_hz=bandwidth,
            spectral_flatness=flatness,
            spectral_flux=flux,
            bass_energy_ratio=bass_ratio,
            tonal_ratio=tonal_ratio,
        )
