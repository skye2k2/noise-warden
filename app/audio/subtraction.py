def reference_adaptive_subtract(primary, reference):
    if len(primary) != len(reference) or len(primary) == 0: return primary
    denom = float((reference * reference).sum()) + 1e-9
    gain = float((primary * reference).sum() / denom)
    return primary - gain * reference
def dual_mic_differential(primary, secondary):
    if len(primary) != len(secondary): return primary
    return primary - 0.7 * secondary
