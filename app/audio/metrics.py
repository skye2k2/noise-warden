import numpy as np
EPS = 1e-12
def rms_dbfs(x):
    x = np.asarray(x, dtype=np.float32)
    rms = np.sqrt(np.mean(np.square(x)) + EPS)
    return 20.0 * np.log10(rms + EPS)
def spectral_features(x, sr):
    x = np.asarray(x, dtype=np.float32)
    if len(x) == 0: return {"centroid":0.0,"flatness":1.0,"low_ratio":0.0,"spread":0.0}
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)))) + EPS
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    mag = X / np.sum(X)
    centroid = float(np.sum(freqs * mag))
    gm = float(np.exp(np.mean(np.log(X)))); am = float(np.mean(X))
    flatness = gm / (am + EPS)
    low = np.sum(X[(freqs >= 20) & (freqs <= 120)]); total = np.sum(X)
    low_ratio = float(low / (total + EPS))
    spread = float(np.std(mag))
    return {"centroid":centroid,"flatness":flatness,"low_ratio":low_ratio,"spread":spread}
