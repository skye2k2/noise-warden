import threading, time
from datetime import datetime
from pathlib import Path
import numpy as np

from app.audio import AudioProcessor
from app.classifier import DeterministicClassifier
from app.db import IncidentDB
from app.ordinance import OrdinanceRules
from app.playback import PlaybackController
from app.integrations import HomeAssistantMonitor, MQTTPublisher
from app.experimental import AdaptiveSubtraction, DualMicRejector

class NoiseWardenEngine:
    def __init__(self, cfg: dict, runtime):
        self.cfg = cfg
        self.runtime = runtime
        self.running = False
        self.thread = None

        Path(cfg["storage"]["snippet_dir"]).mkdir(parents=True, exist_ok=True)
        Path(cfg["storage"]["export_dir"]).mkdir(parents=True, exist_ok=True)

        self.runtime.armed = cfg["mode"]["armed"]
        self.runtime.emergency_kill = cfg["mode"]["emergency_kill"]
        self.runtime.db = IncidentDB(cfg["storage"]["db_path"], cfg["storage"]["export_dir"])

        self.audio = AudioProcessor(cfg)
        self.classifier = DeterministicClassifier(cfg)
        self.ordinance = OrdinanceRules(cfg)
        self.playback = PlaybackController(cfg, runtime)
        self.ha = HomeAssistantMonitor(cfg, runtime)
        self.mqtt = MQTTPublisher(cfg)
        self.adaptive = AdaptiveSubtraction(cfg["experimental"]["adaptive_subtraction"]["learning_rate"])
        self.dual = DualMicRejector(cfg["experimental"]["dual_mic_rejection"]["directionality_bias"])

        self.active_incident_id = None
        self.incident_started_at = None
        self.incident_peak = 0.0
        self.last_below_time = None
        self.last_playback_stop = 0.0

    def start(self):
        if self.running: return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.force_stop_playback()
        if self.thread: self.thread.join(timeout=2)

    def force_stop_playback(self):
        self.playback.stop()
        self.last_playback_stop = time.time()

    def test_playback(self):
        self.playback.test()

    def thresholds_payload(self):
        now = datetime.now()
        active = self.ordinance.thresholds_for_now(now)
        return {
            "city": self.cfg["ordinance"]["city"],
            "current_period": "night" if active["is_night"] else "day",
            "ordinance": self.cfg["ordinance"]["residential"],
            "active_limits": active,
            "system": {
                "trigger_margin_db": self.cfg["thresholds"]["trigger_margin_db"],
                "release_margin_db": self.cfg["thresholds"]["release_margin_db"],
                "release_below_seconds": self.cfg["thresholds"]["release_below_seconds"],
                "hold_min_seconds": self.cfg["thresholds"]["hold_min_seconds"],
                "merge_gap_seconds": self.cfg["classification"]["merge_gap_seconds"],
                "night_record_only": self.cfg["mode"]["night_record_only"],
            }
        }

    def _fake_audio_frame(self):
        n = int(self.cfg["audio"]["sample_rate"] * self.cfg["audio"]["frame_ms"] / 1000)
        return (np.random.randn(n) * 0.002).astype(np.float32)

    def _loop(self):
        while self.running:
            try:
                self.ha.poll()
                now = datetime.now()
                active_limits = self.ordinance.thresholds_for_now(now)
                self.runtime.record_only_now = bool(
                    self.cfg["mode"]["record_only_override"] or
                    (self.cfg["mode"]["night_record_only"] and active_limits["is_night"])
                )

                primary = self._fake_audio_frame()
                secondary = self._fake_audio_frame() if self.cfg["audio"]["use_secondary_mic"] else None
                reference = self._fake_audio_frame() if self.cfg["audio"]["use_reference_input"] else None

                if self.cfg["experimental"]["adaptive_subtraction"]["enabled"]:
                    primary = self.adaptive.process(primary, reference)
                if self.cfg["experimental"]["dual_mic_rejection"]["enabled"]:
                    primary = self.dual.process(primary, secondary)

                metrics = self.audio.analyze_frame(primary)
                self.runtime.current_db_slow = metrics["db_slow"]
                self.runtime.current_db_fast = metrics["db_fast"]

                if self.runtime.playback_active and self.cfg["classification"]["ignore_during_playback"]:
                    self.runtime.classification = "suppressed_self_playback"
                    time.sleep(self.cfg["audio"]["frame_ms"] / 1000); continue

                if time.time() - self.last_playback_stop < self.cfg["playback"]["post_playback_suppress_seconds"]:
                    self.runtime.classification = "cooldown_after_playback"
                    time.sleep(self.cfg["audio"]["frame_ms"] / 1000); continue

                verdict = self.classifier.classify(metrics, active_limits)
                self.runtime.classification = verdict["label"]

                trigger_level = active_limits["continuous"] + self.cfg["thresholds"]["trigger_margin_db"]
                release_level = max(0.0, active_limits["continuous"] - self.cfg["thresholds"]["release_margin_db"])
                should_trigger = verdict["trigger"] and metrics["db_slow"] >= trigger_level and self.runtime.armed and not self.runtime.emergency_kill

                if should_trigger:
                    self.last_below_time = None
                    self.incident_peak = max(self.incident_peak, metrics["db_slow"])
                    if not self.runtime.incident_active:
                        self._start_incident(metrics, verdict, now)
                else:
                    if self.runtime.incident_active:
                        if metrics["db_slow"] < release_level:
                            if self.last_below_time is None:
                                self.last_below_time = time.time()
                            gap = time.time() - self.last_below_time
                            held = time.time() - self.incident_started_at.timestamp()
                            if gap >= self.cfg["classification"]["merge_gap_seconds"] and held >= self.cfg["thresholds"]["hold_min_seconds"]:
                                self._stop_incident(now)
                        else:
                            self.last_below_time = None

                self.mqtt.publish_status(self.runtime.status_payload())
                time.sleep(self.cfg["audio"]["frame_ms"] / 1000)

            except Exception as e:
                self.runtime.last_error = str(e)
                time.sleep(1)

    def _start_incident(self, metrics, verdict, now):
        self.runtime.incident_active = True
        self.incident_started_at = now
        self.incident_peak = metrics["db_slow"]
        snippet = self.audio.save_snippet(
            post_audio=np.zeros(int(self.cfg["audio"]["sample_rate"] * min(5, self.cfg["storage"]["max_snippet_seconds"])), dtype=np.float32),
            snippet_dir=self.cfg["storage"]["snippet_dir"],
        )
        action_taken = False
        if not self.runtime.record_only_now:
            self.playback.start()
            action_taken = self.runtime.playback_active
        self.active_incident_id = self.runtime.db.create_incident(
            started_at=now.isoformat(),
            peak_db=metrics["db_slow"],
            initial_db=metrics["db_slow"],
            classification=verdict["label"],
            action_taken=action_taken,
            record_only=self.runtime.record_only_now,
            snippet_path=snippet,
        )

    def _stop_incident(self, now):
        self.runtime.incident_active = False
        if self.runtime.playback_active:
            self.playback.stop()
            self.last_playback_stop = time.time()
        duration = (now - self.incident_started_at).total_seconds()
        self.runtime.db.close_incident(
            self.active_incident_id,
            ended_at=now.isoformat(),
            peak_db=self.incident_peak,
            duration_seconds=duration,
        )
        self.active_incident_id = None
        self.incident_started_at = None
        self.incident_peak = 0.0
        self.last_below_time = None
