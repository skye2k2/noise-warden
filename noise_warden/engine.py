from __future__ import annotations
import os, shutil, threading, time, tempfile
from datetime import datetime, timezone, timedelta
import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import AudioCapture
from .dsp import (
    rms_dbfs, dba_estimate, spectrum_features, beat_confidence_from_history, music_like_score,
    is_impulse, looks_like_thunder, looks_like_rain, looks_like_mower
)
from .ordinance import applicable_threshold, is_night
from .response import RelayController, PlaylistPlayer
from .ha import HAClient


def _get_system_timezone():
    """Detect the system's IANA timezone name using platform-appropriate methods.
    Tries timedatectl (Linux/systemd) → /etc/timezone (Debian) → /etc/localtime
    symlink (macOS/Linux). Returns None if all strategies fail."""
    import subprocess

    # Strategy 1: timedatectl (Linux with systemd)
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            capture_output=True, text=True, timeout=5
        )
        tz = result.stdout.strip()
        if tz:
            return tz
    except (FileNotFoundError, OSError):
        pass

    # Strategy 2: /etc/timezone file (Debian/Ubuntu)
    try:
        with open("/etc/timezone") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except (FileNotFoundError, OSError):
        pass

    # Strategy 3: /etc/localtime symlink (macOS, some Linux)
    try:
        link = os.readlink("/etc/localtime")
        # e.g. /var/db/timezone/zoneinfo/America/Denver (macOS)
        #   or /usr/share/zoneinfo/America/Denver (Linux)
        marker = "zoneinfo/"
        idx = link.find(marker)
        if idx >= 0:
            tz = link[idx + len(marker):]
            if tz:
                return tz
    except (FileNotFoundError, OSError):
        pass

    return None


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
        self.relay = RelayController(
            int(r["relay_gpio_pin"]),
            active_high=bool(r.get("relay_active_high", True)),
            amp_power_on_delay_sec=float(r.get("amp_power_on_delay_sec", 0.0)),
        )
        self.player = PlaylistPlayer(r["player_command"], r["playlist_dir"])
        self.ha = HAClient(cfg)
        self.running = False
        self.thread = None
        self.db_history = []
        self.active = None
        self._lock = threading.Lock()
        self._last_mqtt_publish = 0.0
        self._mqtt_interval = 5.0  # Seconds between MQTT state publishes
        self._disk_warned = False   # Avoids spamming quota warnings every loop
        # Self-noise suppression: when the system is playing a response through
        # the relay/amp, we must not register our own playback as a noise incident.
        # _response_end_ts tracks when the last response stopped so we can apply
        # a cooldown window (response_cooldown_sec) before resuming detection.
        self._responding = False
        self._response_end_ts = 0.0
        self._response_cooldown_sec = float(r.get("response_cooldown_sec", 5.0))
        # Force-incident flags: set from web UI thread, consumed by engine loop
        self._force_start_requested = False
        self._force_end_requested = False

    def _check_disk_quota(self):
        """Check available disk space in snippets directory and warn if below threshold.
        Quota threshold defaults to 500 MB; configurable via audio.disk_quota_warn_mb.
        At 50 MB free, recording is proactively disabled to prevent silent write failures."""
        snippets_dir = os.path.join(self.cfg["app"]["shared_dir"], "snippets")
        os.makedirs(snippets_dir, exist_ok=True)

        warn_mb = float(self.cfg["audio"].get("disk_quota_warn_mb", 500))
        critical_mb = 50  # Hard floor — stop recording before writes start failing
        try:
            usage = shutil.disk_usage(snippets_dir)
            free_mb = usage.free / (1024 * 1024)
            self.state.set(disk_free_mb=round(free_mb, 1))

            if free_mb < critical_mb:
                # Critical: proactively stop recording on the active incident
                if not self._disk_warned:
                    print(f"[engine] CRITICAL: Disk nearly full — {free_mb:.0f} MB free. Recording disabled.")
                    self._disk_warned = True
                self.state.set(disk_warning=f"CRITICAL: {free_mb:.0f} MB free — recording disabled")
                if self.active and self.active.get("recording"):
                    self.active["recording"] = False
            elif free_mb < warn_mb:
                if not self._disk_warned:
                    print(f"[engine] WARNING: Disk space low — {free_mb:.0f} MB free (threshold: {warn_mb:.0f} MB)")
                    self._disk_warned = True
                self.state.set(disk_warning=f"Low disk: {free_mb:.0f} MB free")
            else:
                if self._disk_warned:
                    print(f"[engine] Disk space recovered — {free_mb:.0f} MB free")
                self._disk_warned = False
                self.state.set(disk_warning=None)
        except OSError as exc:
            print(f"[engine] Disk quota check failed: {exc}")

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.running = True
        # Restore armed state from config so pauses survive server restarts
        armed = bool(self.cfg["detection"].get("armed", True))
        self.state.set(armed=armed)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self._stop_response()
        self.relay.cleanup()
        if self.active:
            self._finalize_incident(force=True)
        self.state.set(running=False, mode="stopped")

    def _start_response(self):
        """Activate the relay and start audio playback. Marks the system as
        responding so self-noise suppression knows to skip detection."""
        self.relay.on()
        self.player.start()
        self._responding = True
        self.state.set(responding=True)
        print("[engine] Response activated — self-noise suppression engaged")

    def _stop_response(self):
        """Deactivate relay and stop playback. Starts the cooldown timer so
        residual speaker/amp noise doesn't immediately trigger a new incident."""
        if self._responding:
            print("[engine] Response deactivated — cooldown period starting")
        self.player.stop()
        self.relay.off()
        self._responding = False
        self._response_end_ts = time.time()
        self.state.set(responding=False)

    def _in_response_cooldown(self):
        """Return True if the system is actively responding OR still within
        the post-response cooldown window. During this period, detected noise
        is our own playback (or its echo/reverb tail) and should be ignored."""
        if self._responding:
            return True
        if self._response_cooldown_sec <= 0:
            return False
        return (time.time() - self._response_end_ts) < self._response_cooldown_sec

    def set_armed(self, armed: bool):
        self.state.set(armed=armed)
        # Finalize any active incident immediately when pausing, so the duration
        # reflects actual monitoring time rather than wall-clock time through the pause.
        if not armed and self.active:
            print("[engine] Pausing — finalizing active incident")
            self._finalize_incident(force=True)

    def force_incident(self):
        """Force-start a test incident from the web UI. The engine loop will append
        audio blocks to it on subsequent iterations. Thread-safe: sets a flag that
        the engine loop picks up on its next block read."""
        self._force_start_requested = True

    def end_forced_incident(self):
        """Request the engine loop to finalize any active incident (forced or real).
        Thread-safe: sets a flag consumed by the engine loop."""
        self._force_end_requested = True

    def _begin_incident(self, db_now, threshold, mscore, bconf, classification, mode):
        now = datetime.now().astimezone()
        ts = now.replace(microsecond=0).isoformat()
        row = {
            "start_ts": ts,
            "start_db": round(db_now, 1),
            "peak_db": round(db_now, 1),
            "avg_db": round(db_now, 1),
            "threshold_db": threshold,
            "music_like_score": round(mscore, 3),
            "beat_confidence": round(bconf, 3),
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
            "start": now,
            "dbs": [db_now],
            "classification": classification,
            "period": "night" if is_night(datetime.now(), self.cfg["detection"]["night_start_hour"], self.cfg["detection"]["night_end_hour"]) else "day",
            "responded": False,
            "last_above": now,
            "tmp_wav": None,
            "recording": bool(self.cfg["audio"].get("recording_enabled", True)),
        }

        if self.active["recording"]:
            snippets_dir = os.path.join(self.cfg["app"]["shared_dir"], "snippets")
            os.makedirs(snippets_dir, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(prefix=f"incident_{iid}_", suffix=".wav", dir=snippets_dir, delete=False)
            tmp.close()
            self.active["tmp_wav"] = tmp.name

            # Open the WAV file once and keep the handle for the incident's lifetime.
            # Previous approach opened/seeked/closed on every 1-second block, which
            # caused write buffering issues on Pi SD cards (choppy audio).
            wf = sf.SoundFile(tmp.name, mode="w", samplerate=self.capture.sr, channels=1, subtype="PCM_16")
            self.active["wav_handle"] = wf

            pre = self.capture.get_preroll(float(self.cfg["audio"]["snippet_pre_seconds"]))
            if pre:
                for block in pre:
                    wf.write(block)
                wf.flush()

        self.state.set(active_incident_id=iid, mode="incident_active")

    def _append_audio(self, block):
        if not self.active or not self.active.get("recording") or not self.active.get("tmp_wav"):
            return
        wf = self.active.get("wav_handle")
        if wf is None or wf.closed:
            return
        try:
            wf.write(block)
            wf.flush()
        except (OSError, RuntimeError) as e:
            # Catches both OS-level I/O errors (disk full) and soundfile's LibsndfileError
            # (which inherits from RuntimeError). Stop recording for this incident but
            # keep the dB-level monitoring running. The partial WAV is preserved.
            print(f"[engine] Audio write failed (disk full?): {e}")
            self._close_wav_handle()
            self.active["recording"] = False
            self.state.set(disk_warning=f"Recording stopped: {e}")

    def _close_wav_handle(self):
        """Close the persistent SoundFile handle if open. Safe to call multiple times."""
        if not self.active:
            return
        wf = self.active.get("wav_handle")
        if wf is not None and not wf.closed:
            try:
                wf.flush()
                wf.close()
            except (OSError, RuntimeError) as e:
                print(f"[engine] WAV handle close failed: {e}")
        self.active["wav_handle"] = None

    def _looks_like_driveby(self, dbs, duration_sec):
        """Determine if an incident matches a drive-by pattern: short duration with a
        fade-out in the tail portion. A drive-by typically rises to a peak then
        decays (near-monotonically) as the vehicle passes.

        Returns True if: duration is under the configured max AND the tail portion
        of dB readings shows at most 1 uptick (predominantly decreasing)."""
        max_dur = float(self.cfg["detection"].get("driveby_max_duration_sec", 30))
        tail_frac = float(self.cfg["detection"].get("driveby_fade_tail_fraction", 0.5))

        if duration_sec > max_dur:
            return False

        if len(dbs) < 3:
            return False

        # Check the tail portion for a fade-out pattern
        tail_start = max(1, int(len(dbs) * (1.0 - tail_frac)))
        tail = dbs[tail_start:]

        if len(tail) < 2:
            return False

        # Count upticks (dB increases) in the tail — a true fade-out should have
        # almost none. We allow 1 for jitter tolerance.
        increases = 0
        for i in range(1, len(tail)):
            if tail[i] > tail[i - 1] + 1.0:  # 1 dB tolerance for noise jitter
                increases += 1

        # Allow at most 1 uptick in the tail — real fade-outs are noisy but generally downward
        return increases <= 1

    def _finalize_incident(self, force=False):
        if not self.active:
            return

        # Close the WAV handle before anything else — ensures all audio data
        # is flushed to disk before we reference the file path in the DB.
        self._close_wav_handle()

        end = datetime.now().astimezone()
        dur = round((end - self.active["start"]).total_seconds())
        if self.active["recording"] and self.active.get("tmp_wav"):
            # pad post-roll with silence window placeholder by simply leaving last blocks already captured
            snippet_path = self.active["tmp_wav"]
        else:
            snippet_path = None

        # Compute average dB with exponential weighting so later (sustained) readings
        # carry more weight than the initial onset ramp. For short incidents (<10 blocks),
        # the weighting barely differs from a flat mean.
        dbs = self.active["dbs"]
        if dbs:
            n = len(dbs)
            # Decay factor: earlier readings get exponentially less weight
            decay = 0.95
            weights = np.array([decay ** (n - 1 - i) for i in range(n)])
            weights /= weights.sum()
            avg_db = float(np.dot(weights, dbs))
        else:
            avg_db = 0.0

        self.storage.finalize_incident(
            self.active["id"], end.replace(microsecond=0).isoformat(), dur,
            round(max(self.active["dbs"]), 1) if self.active["dbs"] else 0.0,
            round(avg_db, 1),
            snippet_path
        )
        self.ha.publish_event({"type": "incident_end", "id": self.active["id"], "duration_sec": dur})

        # Drive-by auto-dismiss: short incidents with a fade-out tail are likely passing
        # vehicles, not sustained nuisance noise. Soft-delete and quarantine the snippet
        # (moved to autodismissed/ for manual review — never permanently deleted by automation).
        incident_id = self.active["id"]
        if not force and self._looks_like_driveby(dbs, dur):
            if snippet_path and os.path.exists(snippet_path):
                try:
                    quarantine_dir = os.path.join(os.path.dirname(snippet_path), "autodismissed")
                    os.makedirs(quarantine_dir, exist_ok=True)
                    quarantine_path = os.path.join(quarantine_dir, os.path.basename(snippet_path))
                    shutil.move(snippet_path, quarantine_path)
                except OSError as exc:
                    print(f"[engine] Failed to quarantine drive-by snippet {snippet_path}: {exc}")
            self.storage.soft_delete_incident(incident_id)
            print(f"[engine] Auto-dismissed incident {incident_id} as drive-by ({dur:.1f}s, {len(dbs)} samples)")

        self.active = None
        self._stop_response()
        self.state.set(active_incident_id=None, mode="idle")

    def _check_period_split(self, db_now, threshold, mscore, bconf, classification, block):
        """Split an active incident when it crosses a day/night boundary.

        Each segment needs its own threshold_db and period so the timeline detail
        view shows accurate ordinance-comparison data. If the noise still exceeds
        the *new* period's threshold, a fresh incident is started immediately to
        maintain recording continuity. If not, the old incident simply finalizes
        and normal detection will restart if the noise rises again.

        Returns True if a split occurred (caller should skip normal append logic
        for this iteration — the current reading is already captured)."""
        if not self.active:
            return False

        cfg_det = self.cfg["detection"]
        current_night = is_night(
            datetime.now(), cfg_det["night_start_hour"], cfg_det["night_end_hour"]
        )
        current_period = "night" if current_night else "day"

        if current_period == self.active.get("period"):
            return False

        old_id = self.active["id"]
        old_period = self.active.get("period", "unknown")
        print(
            f"[engine] Day/night boundary crossed ({old_period} → {current_period}). "
            f"Finalizing incident {old_id}."
        )

        self._finalize_incident()

        # Only begin a new incident if noise still exceeds the new period's threshold.
        # Day→night: threshold drops (55 vs 65), so noise almost certainly still violates.
        # Night→day: threshold rises, so on-going noise may no longer qualify.
        if db_now >= threshold:
            mode = "record_only" if current_night else "respond"
            self._begin_incident(db_now, threshold, mscore, bconf, classification, mode)
            self._append_audio(block)

            if mode == "respond" and self.cfg["response"].get("enable_daytime_response", False):
                self._start_response()
                self.active["responded"] = True

            print(
                f"[engine] Continued as new incident {self.active['id']} "
                f"({current_period}, threshold={threshold})"
            )
        else:
            print(
                f"[engine] Noise ({db_now:.1f} dB) below new {current_period} "
                f"threshold ({threshold}). No new incident started."
            )

        return True

    def _check_duration_split(self, db_now, threshold, mscore, bconf, classification, block):
        """Split an active incident that has exceeded max_incident_record_hours.

        Very long incidents accumulate unbounded dB readings in memory and produce
        unwieldy WAV files. Splitting at a configurable hour boundary gives each
        segment a manageable size while maintaining recording continuity. The
        timeline still shows consecutive blocks for the same noise event.

        Returns True if a split occurred (caller should skip normal append logic
        for this iteration — the current reading is already captured)."""
        if not self.active:
            return False

        max_hours = float(self.cfg["audio"].get("max_incident_record_hours", 6))
        if max_hours <= 0:
            return False

        elapsed_sec = (datetime.now().astimezone() - self.active["start"]).total_seconds()
        max_sec = max_hours * 3600

        if elapsed_sec < max_sec:
            return False

        old_id = self.active["id"]
        hours_str = f"{elapsed_sec / 3600:.1f}"
        print(
            f"[engine] Incident {old_id} reached max duration ({hours_str}h >= {max_hours}h). "
            f"Splitting."
        )

        self._finalize_incident()

        # Noise is still ongoing — begin a fresh segment immediately
        if db_now >= threshold:
            cfg_det = self.cfg["detection"]
            current_night = is_night(
                datetime.now(), cfg_det["night_start_hour"], cfg_det["night_end_hour"]
            )
            mode = "record_only" if current_night else "respond"
            self._begin_incident(db_now, threshold, mscore, bconf, classification, mode)
            self._append_audio(block)

            if mode == "respond" and self.cfg["response"].get("enable_daytime_response", False):
                self._start_response()
                self.active["responded"] = True

            print(f"[engine] Continued as new incident {self.active['id']}")

        return True

    def run(self):
        self.state.set(running=True, mode="idle")

        # Validate audio device at startup — catch misconfiguration early
        ok, msg = self.capture.validate_device()
        if ok:
            print(f"[engine] Audio device validated: {msg}")
        else:
            print(f"[engine] WARNING: Audio device validation failed: {msg}")
            self.state.set(mic_ok=False, last_error=f"Device validation: {msg}")

        # Validate system timezone matches configured ordinance timezone.
        # Day/night threshold selection uses datetime.now() (system local time), so if the
        # Pi's timezone doesn't match the ordinance jurisdiction, thresholds apply at wrong hours.
        expected_tz = self.cfg["detection"].get("expected_timezone")
        if expected_tz:
            system_tz = _get_system_timezone()
            if system_tz is None:
                print("[engine] Timezone check skipped: could not determine system timezone")
            elif system_tz != expected_tz:
                msg = f"System timezone [{system_tz}] does not match expected [{expected_tz}]. Day/night thresholds may be wrong!"
                print(f"[engine] WARNING: {msg}")
                self.state.set(last_error=msg)
            else:
                print(f"[engine] Timezone validated: {system_tz}")

        # Repair any incidents left open by a previous crash
        try:
            repaired = self.storage.repair_stale_incidents()
            if repaired:
                print(f"[engine] Startup repaired {repaired} stale incident(s) from previous crash")
        except Exception as e:
            print(f"[engine] Stale incident repair error: {e}")

        # Run snippet cleanup at engine startup
        retention_days = int(self.cfg["audio"].get("retention_days", 30))
        snippets_dir = os.path.join(self.cfg["app"]["shared_dir"], "snippets")
        try:
            removed = self.storage.cleanup_old_snippets(retention_days, snippets_dir)
            if removed:
                print(f"[engine] Startup cleanup removed {removed} expired snippet(s)")
        except Exception as e:
            print(f"[engine] Startup cleanup error: {e}")

        self._check_disk_quota()

        # Periodic DB vacuum to reclaim space from soft-deleted rows
        try:
            self.storage.vacuum()
            print("[engine] Startup DB vacuum complete")
        except Exception as e:
            print(f"[engine] DB vacuum error: {e}")

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

                # Noise floor gate: if the computed dBA is below the configured
                # floor (default 50 dB), the signal is ambient white noise and
                # not worth analyzing. Skip the expensive DSP pipeline (spectrum
                # features, beat confidence, music classification, exclusion
                # filters) and go straight to gap-timeout / finalize checks.
                noise_floor_db = float(self.cfg["detection"].get("noise_floor_db", 50.0))
                _, threshold = applicable_threshold(self.cfg, datetime.now())
                self.state.set(current_db=round(db_now, 2), current_threshold_db=threshold)

                # Force-incident handling: web UI can request start/stop of a test incident.
                # Checked early so it takes priority over normal detection logic.
                if self._force_end_requested:
                    self._force_end_requested = False
                    if self.active:
                        print("[engine] Ending forced/active incident by user request")
                        self._finalize_incident(force=True)
                    continue

                if self._force_start_requested:
                    self._force_start_requested = False
                    if not self.active:
                        print(f"[engine] Force-starting test incident at {db_now:.1f} dB")
                        mode = "record_only"
                        self._begin_incident(db_now, threshold, 0.0, 0.0, "forced_test", mode)
                    # Append this block to the forced incident
                    if self.active:
                        self.active["dbs"].append(db_now)
                        self._append_audio(block)
                    continue

                if db_now < noise_floor_db:
                    # Still append to an active incident's recording (captures the
                    # tail-off), and check gap-timeout for finalization.
                    if self.active:
                        self.active["dbs"].append(db_now)
                        self._append_audio(block)
                        gap = (datetime.now().astimezone() - self.active["last_above"]).total_seconds()
                        if gap >= float(self.cfg["detection"]["song_gap_merge_sec"]):
                            self._finalize_incident()
                    continue

                features = spectrum_features(block, self.capture.sr)
                bconf = beat_confidence_from_history(self.db_history)
                mscore = music_like_score(features)

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

                # If an active incident crosses a day/night boundary, split it so each
                # segment displays the correct period-specific threshold in the timeline.
                if self.active and self._check_period_split(
                    db_now, threshold, mscore, bconf, classify, block
                ):
                    continue

                # Cap incident duration to avoid unbounded WAV files and memory usage.
                if self.active and self._check_duration_split(
                    db_now, threshold, mscore, bconf, classify, block
                ):
                    continue

                # Self-noise suppression: when we are playing a response (or within the
                # post-response cooldown window), any detected noise is likely our own
                # playback bouncing off surfaces. Skip incident creation/extension to avoid
                # registering our own retaliation as a noise violation.
                # HOWEVER, comma, we still capture audio blocks to the active incident's
                # WAV if one is already in progress — just don't start NEW incidents.
                if self._in_response_cooldown() and not self.active:
                    continue

                if db_now >= threshold and not impulse and not thunder and not rain and not mower:
                    if classify == "music_like" or self.cfg["detection"]["mode"] != "continuous_music_focus":
                        if not self.active:
                            mode = "record_only" if is_night(datetime.now(), self.cfg["detection"]["night_start_hour"], self.cfg["detection"]["night_end_hour"]) else "respond"
                            self._begin_incident(db_now, threshold, mscore, bconf, classify, mode)

                            if mode == "respond" and self.cfg["response"].get("enable_daytime_response", False):
                                self._start_response()
                                self.active["responded"] = True
                        else:
                            self.active["dbs"].append(db_now)
                            self.active["last_above"] = datetime.now().astimezone()
                            self._append_audio(block)
                    else:
                        if self.active:
                            self.active["dbs"].append(db_now)
                            self._append_audio(block)
                else:
                    if self.active:
                        self.active["dbs"].append(db_now)
                        self._append_audio(block)
                        gap = (datetime.now().astimezone() - self.active["last_above"]).total_seconds()
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
                        removed = self.storage.cleanup_old_snippets(retention_days, snippets_dir)
                        if removed:
                            print(f"[engine] Periodic cleanup removed {removed} expired snippet(s)")
                    except Exception as e:
                        print(f"[engine] Periodic cleanup error: {e}")
                    self._check_disk_quota()
                    # Periodic device validation — catch slow drift or silent mic swap
                    ok, msg = self.capture.validate_device()
                    if not ok:
                        print(f"[engine] WARNING: Audio device changed since startup: {msg}")
                        self.state.set(mic_ok=False, last_error=f"Device drift: {msg}")
                    last_cleanup = time.time()

            except (sd.PortAudioError, OSError) as e:
                # Audio I/O errors (USB disconnect, ALSA xrun, disk I/O) are often transient.
                # Reinitialize the capture device and retry rather than spinning on a dead handle.
                error_msg = str(e)
                print(f"[engine] Audio I/O error (attempting reconnection): {error_msg}")
                self.state.set(mic_ok=False, last_error=error_msg, mode="error")
                try:
                    a = self.cfg["audio"]
                    self.capture = AudioCapture(
                        sample_rate=int(a["sample_rate"]),
                        block_seconds=float(a["block_seconds"]),
                        channels=int(a.get("input_channels", 1)),
                        device=a.get("input_device")
                    )
                    print("[engine] Audio device reinitialized successfully")
                except Exception as reinit_err:
                    print(f"[engine] Audio reinit failed: {reinit_err}")
                time.sleep(2.0)  # Back off to avoid hammering a disconnected device

            except Exception as e:
                # Unexpected errors — log but don't crash the loop
                self.state.set(mic_ok=False, last_error=str(e), mode="error")
                print(f"[engine] Unexpected error in audio loop: {e}")
                time.sleep(1.0)
