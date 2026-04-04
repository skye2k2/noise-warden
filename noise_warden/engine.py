from __future__ import annotations
import os, threading, time, tempfile
from datetime import datetime, timezone, timedelta
import numpy as np
import soundfile as sf

from .audio import AudioCapture
from .dsp import (
    rms_dbfs, dba_estimate, spectrum_features, beat_confidence_from_history, music_like_score,
    is_impulse, looks_like_thunder, looks_like_rain, looks_like_mower
)
from .ordinance import applicable_threshold, is_night
from .response import RelayController, PlaylistPlayer
from .ha import HAClient

class Engine:
    def __init__(self, cfg, storage, state):
        self.cfg = cfg
        self.storage = storage
        self.state = state
        a = cfg["audio"]
        r = cfg["response"]
        self.capture = AudioCapture(
            sample_rate=int(a["sample_rate"]),
            block_seconds=float(a["block_seconds"]),
            channels=int(a.get("input_channels", 1)),
            device=a.get("input_device")
        )
        self.relay = RelayController(int(r["relay_gpio_pin"]))
        self.player = PlaylistPlayer(r["player_command"], r["playlist_dir"])
        self.ha = HAClient(cfg)
        self.running = False
        self.thread = None
        self.db_history = []
        self.active = None
        self._lock = threading.Lock()
        self._last_mqtt_publish = 0.0
        self._mqtt_interval = 5.0  # Seconds between MQTT state publishes

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.running = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self.player.stop()
        self.relay.off()
        if self.active:
            self._finalize_incident(force=True)
        self.state.set(running=False, mode="stopped")

    def set_armed(self, armed: bool):
        self.state.set(armed=armed)

    def _begin_incident(self, db_now, threshold, mscore, bconf, classification, mode):
        ts = datetime.now(timezone.utc).isoformat()
        row = {
            "start_ts": ts,
            "start_db": db_now,
            "peak_db": db_now,
            "avg_db": db_now,
            "threshold_db": threshold,
            "music_like_score": mscore,
            "beat_confidence": bconf,
            "classification": classification,
            "mode": mode,
            "responded": 0,
            "merge_count": 0,
            "snippet_path": None,
            "notes": ""
        }
        iid = self.storage.create_incident(row)

        self.active = {
            "id": iid,
            "start": datetime.now(timezone.utc),
            "dbs": [db_now],
            "classification": classification,
            "responded": False,
            "last_above": datetime.now(timezone.utc),
            "tmp_wav": None,
            "recording": bool(self.cfg["audio"].get("recording_enabled", True)),
        }

        if self.active["recording"]:
            snippets_dir = os.path.join(self.cfg["app"]["shared_dir"], "snippets")
            os.makedirs(snippets_dir, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(prefix=f"incident_{iid}_", suffix=".wav", dir=snippets_dir, delete=False)
            tmp.close()
            self.active["tmp_wav"] = tmp.name
            pre = self.capture.get_preroll(float(self.cfg["audio"]["snippet_pre_seconds"]))
            if pre:
                with sf.SoundFile(tmp.name, mode="w", samplerate=self.capture.sr, channels=1, subtype="PCM_16") as wf:
                    for block in pre:
                        wf.write(block)

        self.state.set(active_incident_id=iid, mode="incident_active")

    def _append_audio(self, block):
        if not self.active or not self.active.get("recording") or not self.active.get("tmp_wav"):
            return
        with sf.SoundFile(self.active["tmp_wav"], mode="r+") as wf:
            wf.seek(0, whence=2)
            wf.write(block)

    def _finalize_incident(self, force=False):
        if not self.active:
            return
        end = datetime.now(timezone.utc)
        dur = (end - self.active["start"]).total_seconds()
        if self.active["recording"] and self.active.get("tmp_wav"):
            # pad post-roll with silence window placeholder by simply leaving last blocks already captured
            snippet_path = self.active["tmp_wav"]
        else:
            snippet_path = None
        self.storage.finalize_incident(
            self.active["id"], end.isoformat(), dur,
            max(self.active["dbs"]) if self.active["dbs"] else 0.0,
            float(np.mean(self.active["dbs"])) if self.active["dbs"] else 0.0,
            snippet_path
        )
        self.ha.publish_event({"type": "incident_end", "id": self.active["id"], "duration_sec": dur})
        self.active = None
        self.relay.off()
        self.player.stop()
        self.state.set(active_incident_id=None, mode="idle")

    def run(self):
        self.state.set(running=True, mode="idle")

        # Run snippet cleanup at engine startup
        snippets_dir = os.path.join(self.cfg["app"]["shared_dir"], "snippets")
        retention_days = int(self.cfg["audio"].get("retention_days", 30))
        try:
            removed = self.storage.cleanup_old_snippets(snippets_dir, retention_days)
            if removed:
                print(f"[engine] Startup cleanup removed {removed} expired snippet(s)")
        except Exception as e:
            print(f"[engine] Startup cleanup error: {e}")

        last_cleanup = time.time()
        CLEANUP_INTERVAL = 86400  # Re-run cleanup once per day

        while self.running:
            try:
                if not self.state.snapshot()["armed"]:
                    time.sleep(0.25)
                    continue

                block = self.capture.read_block()
                self.state.set(mic_ok=True)

                dbfs = rms_dbfs(block)
                db_now = dba_estimate(dbfs, float(self.cfg["detection"]["calibration_offset_db"]))
                self.db_history.append(db_now)
                self.db_history = self.db_history[-240:]

                features = spectrum_features(block, self.capture.sr)
                bconf = beat_confidence_from_history(self.db_history)
                mscore = music_like_score(features)

                rule_name, threshold = applicable_threshold(self.cfg, datetime.now())
                self.state.set(current_db=round(db_now, 2), current_threshold_db=threshold)

                prev = self.db_history[-2] if len(self.db_history) > 1 else db_now
                impulse = is_impulse(db_now, prev, float(self.cfg["detection"]["impulse_peak_delta_db"]))
                thunder = looks_like_thunder(features, db_now, prev, float(self.cfg["detection"]["thunder_peak_delta_db"]))
                rain = looks_like_rain(features, self.db_history, float(self.cfg["detection"]["rain_flatness_threshold"]), float(self.cfg["detection"]["rain_low_variance_db"]))
                mower = looks_like_mower(
                    features, self.db_history,
                    float(self.cfg["detection"]["mower_flatness_threshold"]),
                    float(self.cfg["detection"]["mower_centroid_min_hz"]),
                    float(self.cfg["detection"]["mower_centroid_max_hz"])
                )

                classify = "other"
                if mscore >= float(self.cfg["detection"]["min_music_like_score"]):
                    classify = "music_like"

                if db_now >= threshold and not impulse and not thunder and not rain and not mower:
                    if classify == "music_like" or self.cfg["detection"]["mode"] != "continuous_music_focus":
                        if not self.active:
                            mode = "record_only" if is_night(datetime.now(), self.cfg["detection"]["night_start_hour"], self.cfg["detection"]["night_end_hour"]) else "respond"
                            self._begin_incident(db_now, threshold, mscore, bconf, classify, mode)

                            if mode == "respond" and self.cfg["response"].get("enable_daytime_response", False):
                                self.relay.on()
                                self.player.start()
                                self.active["responded"] = True
                        else:
                            self.active["dbs"].append(db_now)
                            self.active["last_above"] = datetime.now(timezone.utc)
                            self._append_audio(block)
                    else:
                        if self.active:
                            self.active["dbs"].append(db_now)
                            self._append_audio(block)
                else:
                    if self.active:
                        self.active["dbs"].append(db_now)
                        self._append_audio(block)
                        gap = (datetime.now(timezone.utc) - self.active["last_above"]).total_seconds()
                        if gap >= float(self.cfg["detection"]["song_gap_merge_sec"]):
                            self._finalize_incident()

                # Throttle MQTT to avoid flooding the broker (~120 msgs/min → ~12 msgs/min)
                now_ts = time.time()
                if now_ts - self._last_mqtt_publish >= self._mqtt_interval:
                    self.ha.publish_state(self.state.snapshot())
                    self._last_mqtt_publish = now_ts

                # Periodic snippet cleanup (once per day)
                if time.time() - last_cleanup >= CLEANUP_INTERVAL:
                    try:
                        removed = self.storage.cleanup_old_snippets(snippets_dir, retention_days)
                        if removed:
                            print(f"[engine] Periodic cleanup removed {removed} expired snippet(s)")
                    except Exception as e:
                        print(f"[engine] Periodic cleanup error: {e}")
                    last_cleanup = time.time()

            except Exception as e:
                self.state.set(mic_ok=False, last_error=str(e), mode="error")
                time.sleep(1.0)
