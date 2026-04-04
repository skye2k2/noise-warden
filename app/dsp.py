import numpy as np
def rms_dbfs(x):
    if len(x)==0: return -120.0
    rms = np.sqrt(np.mean(np.square(x)) + 1e-12)
    return 20.0 * np.log10(rms + 1e-12)
def calibrated_db(x, offset_db): return rms_dbfs(x) + offset_db
def spectral_features(x, sr):
    if len(x)==0: return {'flatness':1.0,'lowband_ratio':0.0,'centroid':0.0}
    w = np.hanning(len(x)); X = np.fft.rfft(x*w); mag = np.abs(X)+1e-12; freqs = np.fft.rfftfreq(len(x),1/sr)
    gm = np.exp(np.mean(np.log(mag))); am = np.mean(mag); flatness = float(gm/(am+1e-12))
    low = mag[(freqs>=20)&(freqs<=250)].sum(); total = mag.sum(); lowband_ratio = float(low/(total+1e-12))
    centroid = float((freqs*mag).sum()/(mag.sum()+1e-12))
    return {'flatness':flatness,'lowband_ratio':lowband_ratio,'centroid':centroid}
def music_like_score(x, sr):
    f = spectral_features(x, sr)
    return float(max(0.0, min(1.0, 0.6*max(0.0,min(1.0,f['lowband_ratio']*1.6)) + 0.4*max(0.0,min(1.0,1.0-abs(f['flatness']-0.35))))))
def beat_confidence(history):
    if len(history) < 6: return 0.0
    arr = np.array(history, dtype=float)
    return float(max(0.0, min(1.0, np.mean(np.abs(np.diff(arr))) / 8.0)))
def classify_noise(db_now, db_prev, features, cfg):
    delta = db_now - db_prev
    if cfg['enable_impulse_reject'] and delta >= cfg['impulse_peak_delta_db'] and features['flatness'] < 0.15: return 'impulse', True
    if cfg['enable_thunder_reject'] and delta >= cfg['thunder_peak_delta_db'] and features['lowband_ratio'] >= cfg['thunder_lowband_ratio_min']: return 'thunder_like', True
    if cfg['enable_rain_reject'] and features['flatness'] >= cfg['rain_flatness_threshold']: return 'rain_like', True
    if cfg['enable_mower_reject'] and features['flatness'] >= cfg['mower_flatness_threshold'] and features['centroid'] > 500: return 'mower_like', True
    return 'music_candidate', False
