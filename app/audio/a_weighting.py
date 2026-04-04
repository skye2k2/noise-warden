from __future__ import annotations
import numpy as np
from scipy import signal


def design_a_weighting(fs: int):
    # Digital A-weighting approximation via bilinear transform
    f1 = 20.598997
    f2 = 107.65265
    f3 = 737.86223
    f4 = 12194.217
    A1000 = 1.9997

    nums = [(2 * np.pi * f4) ** 2 * (10 ** (A1000 / 20)), 0, 0, 0, 0]
    dens = np.polymul([1, 4 * np.pi * f4, (2 * np.pi * f4) ** 2], [1, 4 * np.pi * f1, (2 * np.pi * f1) ** 2])
    dens = np.polymul(np.polymul(dens, [1, 2 * np.pi * f3]), [1, 2 * np.pi * f2])

    b, a = signal.bilinear(nums, dens, fs)
    return b, a


def apply_a_weighting(x: np.ndarray, fs: int, zi=None):
    b, a = design_a_weighting(fs)
    if zi is None:
        y = signal.lfilter(b, a, x)
        return y, None
    y, zf = signal.lfilter(b, a, x, zi=zi)
    return y, zf
