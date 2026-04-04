from pathlib import Path
from datetime import datetime
import threading, time
import numpy as np, soundfile as sf
from app.core.config import Config
from app.core.timeutil import is_night_mode
from app.core.logging_db import IncidentStore
from app.audio.capture import AudioInput
from app.audio.metrics import rms_dbfs
from app.audio.exclusion import ExclusionEngine
from app.audio.ringbuffer import AudioRingBuffer
from app.audio.subtraction import reference_adaptive_subtract, dual_mic_differential
from app.hardware.relay import RelayController
from app.hardware.player import PlaylistPlayer
from app.engine.state import RuntimeState

class NoiseWardenController:
    def __init__(self):
        self.cfg = Config.load(); self.store = IncidentStore(); self.state = RuntimeState()
        self.thread = None; self.stop_flag = False
        sr = int(self.cfg.get("audio","sample_rate")); block = int(sr * float(self.cfg.get("audio","block_seconds")))
        self.sr = sr; self.block = block
        self.primary = AudioInput(self.cfg.get("audio","primary_device"), int(self.cfg.get("audio","channels_primary", default=1)), sr, block)
        self.secondary = AudioInput(self.cfg.get("audio","secondary_device"), int(self.cfg.get("audio","channels_secondary", default=1)), sr, block) if self.cfg.get("audio","enable_secondary_mic", default=False) else None
        self.reference = AudioInput(self.cfg.get("audio","reference_device"), int(self.cfg.get("audio","channels_reference", default=1)), sr, block) if self.cfg.get("audio","enable_reference_input", default=False) else None
        self.ring = AudioRingBuffer(int(self.cfg.get("audio","snippet_pre_seconds", default=15)), sr)
        self.event_frames = []
        self.exclusions = ExclusionEngine(self.cfg.get("filters", default={}), sr)
        self.relay = RelayController(int(self.cfg.get("gpio","relay_pin", default=17)), bool(self.cfg.get("gpio","relay_active_high", default=True)), bool(self.cfg.get("gpio","enable_relay", default=True)))
        self.player = PlaylistPlayer(self.cfg.get("response","playlist_dir", default="./media/playlist"), self.cfg.get("response","player_command", default="ffplay -nodisp -autoexit -loglevel quiet {file}"))

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_flag = False
        self.primary.start()
        if self.secondary: self.secondary.start()
        if self.reference: self.reference.start()
        self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()

    def manual_arm(self, armed: bool):
        self.state.armed = armed
        if not armed and self.state.active: self._end_incident(datetime.now())

    def _start_incident(self, now, db, mode, classification):
        self.state.active = True; self.state.current_mode = "record_only" if mode == "night_record_only" else "responding"
        self.state.current_incident_start = now; self.state.peak_db = db; self.state.sum_db = db; self.state.count_db = 1; self.state.pending_gap_since = None
        self.event_frames = [self.ring.get()]
        iid = self.store.start_incident(now, db, mode, classification); self.state.current_incident_id = iid
        if mode != "night_record_only" and self.cfg.get("response","enable_daytime_response", default=False):
            self.relay.on(); time.sleep(float(self.cfg.get("response","amp_power_on_delay_sec", default=0.75))); self.player.start()

    def _save_snippet(self, end_ts):
        if not self.event_frames: return None
        audio = np.concatenate(self.event_frames) if len(self.event_frames) > 1 else self.event_frames[0]
        out = Path("data/snippets"); out.mkdir(parents=True, exist_ok=True)
        fname = out / f"incident_{end_ts.strftime('%Y%m%d_%H%M%S')}.wav"
        sf.write(str(fname), audio, self.sr); return str(fname)

    def _end_incident(self, now):
        if not self.state.active or self.state.current_incident_id is None: return
        snippet = self._save_snippet(now)
        self.store.end_incident(self.state.current_incident_id, now, snippet)
        self.state.active = False; self.state.current_mode = "standby"; self.state.current_incident_id = None; self.state.current_incident_start = None; self.state.pending_gap_since = None
        self.event_frames = []; self.player.stop(); self.relay.off()

    def _run(self):
        rules = self.cfg.get("rules", default={})
        threshold_day = float(rules.get("residential_day_threshold_db", 60.0)); threshold_night = float(rules.get("residential_night_threshold_db", 55.0))
        eval_interval = int(rules.get("evaluation_interval_sec", 10)); release_sec = int(rules.get("release_below_threshold_sec", 20))
        gap_merge = int(rules.get("song_gap_merge_sec", 10)); min_dur = int(rules.get("min_event_duration_sec", 15)); hysteresis = float(rules.get("hysteresis_db", 3.0))
        above_history = []
        while not self.stop_flag:
            now = datetime.now()
            try: frame = self.primary.read(timeout=2.0)
            except Exception: time.sleep(0.1); continue
            if self.reference:
                try: frame = reference_adaptive_subtract(frame, self.reference.read(timeout=0.1))
                except Exception: pass
            if self.secondary:
                try: frame = dual_mic_differential(frame, self.secondary.read(timeout=0.1))
                except Exception: pass
            self.ring.push(frame)
            db = rms_dbfs(frame) + 100.0
            self.state.last_db = round(db, 2); self.state.last_update = now.isoformat()
            night = is_night_mode(now, rules.get("night_start","22:00"), rules.get("night_end","07:00"))
            threshold = threshold_night if night else threshold_day
            exclusion = self.exclusions.decide(frame, db); self.state.last_classification = exclusion.label
            above = (db >= threshold)
            above_history.append(above and not exclusion.excluded)
            if len(above_history) > eval_interval: above_history.pop(0)
            sustained = sum(1 for x in above_history if x) >= max(1, int(eval_interval * 0.7))
            if not self.state.armed:
                if self.state.active: self._end_incident(now)
                continue
            if not self.state.active:
                if sustained:
                    self._start_incident(now, db, "night_record_only" if night else "day_response", exclusion.label)
            else:
                self.event_frames.append(frame.copy())
                self.state.peak_db = max(self.state.peak_db, db); self.state.sum_db += db; self.state.count_db += 1
                self.store.update_incident(self.state.current_incident_id, self.state.peak_db, self.state.sum_db / max(1, self.state.count_db))
                if db < (threshold - hysteresis) or exclusion.excluded:
                    if self.state.pending_gap_since is None: self.state.pending_gap_since = now
                    gap = (now - self.state.pending_gap_since).total_seconds()
                    elapsed = (now - self.state.current_incident_start).total_seconds() if self.state.current_incident_start else 0
                    if gap >= max(release_sec, gap_merge) and elapsed >= min_dur: self._end_incident(now)
                else:
                    self.state.pending_gap_since = None

    def get_status(self):
        rules = self.cfg.get("rules", default={})
        return {
            "armed": self.state.armed, "active": self.state.active, "mode": self.state.current_mode,
            "last_db": self.state.last_db, "last_classification": self.state.last_classification, "last_update": self.state.last_update,
            "ha_status": self.state.ha_status,
            "thresholds": {"day": rules.get("residential_day_threshold_db"), "night": rules.get("residential_night_threshold_db"),
                           "eval_interval_sec": rules.get("evaluation_interval_sec"), "release_below_threshold_sec": rules.get("release_below_threshold_sec"),
                           "song_gap_merge_sec": rules.get("song_gap_merge_sec")}
        }
