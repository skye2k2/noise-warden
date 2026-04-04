from datetime import time
def parse_hhmm(s: str) -> time:
    h, m = s.split(":"); return time(int(h), int(m))
def is_night_mode(now, start: str, end: str) -> bool:
    st, en = parse_hhmm(start), parse_hhmm(end)
    t = now.time()
    if st < en: return st <= t < en
    return t >= st or t < en
