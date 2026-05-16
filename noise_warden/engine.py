from __future__ import annotations
import json, os, signal, shutil, threading, time, tempfile
from datetime import datetime, timezone, timedelta
import numpy as np
import sounddevice as sd
import soundfile as sf

from .audio import AudioCapture
from .dsp import (
    apply_filter_holdover, rms_dbfs, dba_estimate, spectrum_features,
    beat_confidence, get_filter_detection_latency,
    identify_filter, music_like_score,
)
from .ordinance import applicable_threshold, is_night
from .reclassify import denoise_snippet, normalize_snippet
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
        # Plain list, not deque — DSP functions slice db_history (e.g. [-12:])
        # and deque doesn't support slicing. Converting to list at each DSP call
        # would cost more than the list re-slice here, so keep it simple.
        self.db_history = []
        self.feature_history = []  # Recent spectrum_features dicts for temporal analysis (Path D chorus)
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

        # Excluded incident tracking: when a filter (thunder, mower, birdsong, etc.)
        # catches a sound that's above threshold, we optionally log it as an excluded
        # incident so the operator can review what was filtered out. Only active in
        # continuous/intermittent mode (not continuous_music_focus).
        self._excluded_id = None
        self._excluded_filter = None
        self._excluded_peak_db = 0.0
        self._excluded_start = None

        # Filter holdover state: tracks the most recent filter result and how
        # many consecutive blocks it has been active. Used by apply_filter_holdover()
        # to persist a well-established filter through brief gaps (startups,
        # throttle changes, stop/restart pauses).
        self._prev_filter = None
        self._prev_filter_run = 0
        self._holdover_gap = 0

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

    def _check_memory_usage(self):
        """Check process RSS and expose it to the dashboard. If RSS exceeds
        the warning threshold, log it. If it exceeds the critical threshold,
        self-terminate so systemd can restart with a clean slate. Normal RSS
        is ~90-120 MB; the MemoryMax=1024M systemd limit is the hard backstop,
        but catching runaway growth early avoids OOM-kill journal noise."""
        try:
            import resource
            # On Linux, ru_maxrss is in KB; on macOS it's in bytes
            raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = raw / 1024 if os.name != "nt" else raw / (1024 * 1024)
            # macOS reports bytes, Linux reports KB
            import sys
            if sys.platform == "darwin":
                rss_mb = raw / (1024 * 1024)
            else:
                rss_mb = raw / 1024

            self.state.set(process_rss_mb=round(rss_mb, 1))

            if rss_mb > 700:
                print(f"[engine] CRITICAL: RSS {rss_mb:.0f} MB — self-restarting to prevent OOM kill")
                self.state.set(last_error=f"Memory critical: {rss_mb:.0f} MB — restarting")
                time.sleep(2)
                os.kill(os.getpid(), signal.SIGTERM)
            elif rss_mb > 500:
                print(f"[engine] WARNING: RSS is {rss_mb:.0f} MB — possible memory leak")
                self.state.set(last_error=f"High memory: {rss_mb:.0f} MB")
        except Exception as exc:
            # resource module not available on all platforms — not fatal
            print(f"[engine] Memory check unavailable: {exc}")

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.running = True
        # Restore armed state from config so pauses survive server restarts
        armed = bool(self.cfg["detection"].get("armed", True))
        recording = bool(self.cfg["audio"].get("recording_enabled", True))
        self.state.set(armed=armed, recording_enabled=recording)

        def _run_with_crash_guard():
            """Wrapper that catches unhandled exceptions from the engine loop.
            If the loop exits unexpectedly, we set an error state so the
            dashboard shows the problem, then SIGTERM the process so systemd
            can restart cleanly. Without this, a daemon-thread crash would
            leave the web server running with stale state and no monitoring."""
            try:
                self.run()
            except Exception as exc:
                print(f"[engine] FATAL: engine thread crashed: {exc}")
                self.state.set(
                    last_error=f"Engine crashed: {exc}",
                    mode="crashed",
                    running=False,
                )
                # Brief delay so an in-flight dashboard poll can serve the
                # crash state before the process exits
                time.sleep(3)
                os.kill(os.getpid(), signal.SIGTERM)

        self.thread = threading.Thread(target=_run_with_crash_guard, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self._stop_response()
        self.relay.cleanup()
        self.capture.close()
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
            "music_like_score": round(mscore, 2),
            "beat_confidence": round(bconf, 2),
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
            "class_journal": [(0, classification)],
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
            preroll_blocks = 0
            if pre:
                for block in pre:
                    wf.write(block)
                    preroll_blocks += 1
                wf.flush()

            # Track actual preroll duration for journal notation and duration display.
            # The WAV starts earlier than the incident's start_ts by this many seconds.
            block_sec = float(self.cfg["audio"].get("block_seconds", 1.0))
            preroll_sec = round(preroll_blocks * block_sec, 1)
            self.active["preroll_seconds"] = preroll_sec

            # Prepend a lead-in marker to the classification journal so the
            # timeline makes it obvious why the WAV is longer than the duration.
            if preroll_sec > 0 and self.active.get("class_journal"):
                pre_entry = (round(-preroll_sec), "lead-in")
                self.active["class_journal"].insert(0, pre_entry)

        self.state.set(active_incident_id=iid, mode="incident_active",
                      forced_test=(classification == "forced_test"))

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

    def _trim_snippet_tail(self, wav_path, blocks_to_trim, block_seconds):
        """Remove trailing silent blocks from a WAV snippet file.

        During the song_gap_merge_sec window, sub-threshold audio accumulates
        at the end of every recording.  This dead air inflates file size and
        carries no evidentiary value.  Trimming it keeps snippets concise.
        NOTE: could be made more precise by trimming to the last
        positively-classified block instead of last above-threshold block.
        """
        try:
            data, sr = sf.read(wav_path, dtype="float32")
            samples_per_block = int(sr * block_seconds)
            samples_to_trim = blocks_to_trim * samples_per_block

            if samples_to_trim <= 0 or samples_to_trim >= len(data):
                return

            trimmed = data[:len(data) - samples_to_trim]
            sf.write(wav_path, trimmed, sr, subtype="PCM_16")
        except (OSError, RuntimeError) as exc:
            # Non-fatal — the un-trimmed snippet is still usable evidence.
            print(f"[engine] Snippet tail trim failed for {wav_path}: {exc}")

    def _identify_filter(self, features, db_now, prev_db, beat_confidence=None):
        """Delegate to dsp.identify_filter + apply holdover. The engine doesn't
        need to know about individual filter parameters or priority order.
        Tracks holdover state so sustained filters persist through brief gaps."""
        raw = identify_filter(
            features, self.db_history, db_now, prev_db, self.cfg["detection"],
            feature_history=self.feature_history,
            beat_confidence=beat_confidence,
        )
        effective, self._prev_filter, self._prev_filter_run, self._holdover_gap = (
            apply_filter_holdover(
                raw, self._prev_filter, self._prev_filter_run,
                self._holdover_gap, self.cfg["detection"],
            )
        )
        return effective

    def _classify_sound(self, mscore, bconf, features=None):
        """Classify a non-excluded sound based on DSP features.

        Categories:
          music        — high music-like score AND high beat confidence (rhythmic music)
          music_like   — high music-like score but low beat confidence (bass-heavy, non-rhythmic)
          engine_noise — mechanical/engine sound: dominant midband OR (moderate bass with
                         flat intra-block envelope and no harmonic structure). This catches
                         flyovers, drive-bys, diesel idle, mowers, and weedwhackers that
                         slipped past the specific exclusion filters — preventing them from
                         being misclassified as music or music_like.
          unknown      — does not match any known pattern

        The min_music_like_score and min_beat_confidence thresholds are configurable
        in detection config. Default: music_like >= 0.62, beat >= 0.38.

        Engine noise detection uses two complementary checks:
          1. Dominant midband (>0.50) — combustion engines and vehicle noise concentrate
             energy in the 180–1200 Hz band. Music follows a "smiley EQ" with scooped mids.
          2. Steady bass without harmonics — when bass is present (lowband > 0.10) but the
             intra-block envelope is flat (cv < 0.10) and harmonic structure is weak
             (ratio < 0.40), it's mechanical rumble, not musical bass. This catches bass-heavy
             engine sounds that have low midband (e.g., diesel idle with dominant rumble).
        """
        det = self.cfg["detection"]
        min_music = float(det["min_music_like_score"])
        min_beat = float(det.get("min_beat_confidence", 0.38))

        if features:
            midband = features.get("midband_ratio", 0)
            envelope_cv = features.get("envelope_cv", 0.5)
            harmonic = features.get("harmonic_ratio", 0.5)
            lowband = features.get("lowband_ratio", 0)

            # Check 1: dominant midband is a strong engine indicator.
            # No known music source has midband_ratio > 0.50 in our recordings.
            if midband > 0.50:
                return "engine_noise"

            # Check 2: bass-present but steady amplitude + no harmonic structure.
            # Musical bass has dynamic envelope (kicks, drops) and harmonic peaks.
            # Engine bass is a flat drone with broadband energy distribution.
            if lowband > 0.10 and envelope_cv < 0.10 and harmonic < 0.40:
                return "engine_noise"

        if mscore >= min_music and bconf >= min_beat:
            return "music"
        if mscore >= min_music:
            return "music_like"
        return "unknown"

    def _update_class_journal(self, classify):
        """Append a classification transition to the active incident's journal.
        Only logs when the classification actually changes from the most recent
        entry, keeping the journal compact (transitions only, not per-block).
        When a filter first identifies a sound, the entry is backdated by the
        filter's detection latency — the pattern was present before we had
        enough history to confirm it."""
        if not self.active:
            return
        journal = self.active.get("class_journal")
        if not journal:
            return
        if journal[-1][1] == classify:
            return  # Same source — no transition
        elapsed = round((datetime.now().astimezone() - self.active["start"]).total_seconds())

        # Backdate filter transitions by their detection latency
        det_cfg = self.cfg["detection"]
        latency = get_filter_detection_latency(classify, det_cfg)
        if latency > 0:
            block_sec = float(self.cfg["audio"]["block_seconds"])
            backdated = round(elapsed - latency * block_sec)

            # If backdating would overlap with or precede a trailing
            # "unknown" entry, replace it — that unknown was really the
            # lead-in to this filter's detection window.
            if (len(journal) >= 1 and journal[-1][1] == "unknown" and
                    backdated <= journal[-1][0]):
                journal.pop()

            earliest = journal[-1][0] + 1 if journal else 0
            elapsed = max(earliest, backdated)

            # After replacing, check if we'd duplicate the previous entry
            if journal and journal[-1][1] == classify:
                return

        journal.append((elapsed, classify))

    def _should_log_excluded(self):
        """Whether to log filter-excluded sounds as informational incidents.
        Only active in continuous or intermittent mode — in music-focus mode we're
        specifically filtering for music, so logging mower/birdsong adds noise."""
        return self.cfg["detection"]["mode"] != "continuous_music_focus"

    def _begin_excluded_incident(self, filter_type, db_now, threshold, mscore, bconf, mode):
        """Start a lightweight excluded incident — metadata only, no audio recording."""
        now = datetime.now().astimezone()
        ts = now.replace(microsecond=0).isoformat()
        row = {
            "start_ts": ts,
            "start_db": round(db_now, 1),
            "peak_db": round(db_now, 1),
            "avg_db": round(db_now, 1),
            "threshold_db": threshold,
            "music_like_score": round(mscore, 2),
            "beat_confidence": round(bconf, 2),
            "classification": filter_type,
            "mode": mode,
            "responded": 0,
            "merge_count": 0,
            "snippet_path": None,
            "notes": "",
            "excluded": 1,
        }
        self._excluded_id = self.storage.create_incident(row)
        self._excluded_filter = filter_type
        self._excluded_peak_db = db_now
        self._excluded_start = now

    def _extend_excluded_incident(self, db_now):
        """Update peak dB for the active excluded incident."""
        if db_now > self._excluded_peak_db:
            self._excluded_peak_db = db_now

    def _end_excluded_incident(self):
        """Finalize the active excluded incident with end timestamp and duration."""
        if self._excluded_id is None:
            return
        end = datetime.now().astimezone()
        dur = round((end - self._excluded_start).total_seconds())
        self.storage.finalize_incident(
            self._excluded_id,
            end.replace(microsecond=0).isoformat(),
            dur,
            round(self._excluded_peak_db, 1),
            round(self._excluded_peak_db, 1),  # avg ≈ peak for brief excluded events
            None,  # no snippet
        )
        self._excluded_id = None
        self._excluded_filter = None
        self._excluded_peak_db = 0.0
        self._excluded_start = None

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
        raw_dur = round((end - self.active["start"]).total_seconds())
        if self.active["recording"] and self.active.get("tmp_wav"):
            snippet_path = self.active["tmp_wav"]
        else:
            snippet_path = None

        # ── Tail trimming ──────────────────────────────────────────────
        # During the song_gap_merge_sec window the mic hears sub-threshold
        # audio that keeps the incident open but contributes nothing useful.
        # Trim those blocks from dbs, duration, and the WAV — but preserve
        # snippet_post_seconds of post-event context for natural-sounding
        # recordings and evidentiary completeness.
        block_sec = float(self.cfg["audio"].get("block_seconds", 1.0))
        post_sec = float(self.cfg["audio"].get("snippet_post_seconds", 2))
        post_blocks = max(1, round(post_sec / block_sec))
        gap_seconds = (end - self.active["last_above"]).total_seconds()
        tail_blocks = max(0, round(gap_seconds / block_sec))
        # Keep post_blocks of tail for context instead of just 1
        blocks_to_trim = max(0, tail_blocks - post_blocks)

        all_dbs = self.active["dbs"]
        # Safety: never trim to fewer than 1 block
        effective_count = max(1, len(all_dbs) - blocks_to_trim)
        trimmed_dbs = all_dbs[:effective_count]
        dur = max(1, raw_dur - int(blocks_to_trim * block_sec))

        if blocks_to_trim > 0:
            print(f"[engine] Tail trim: {blocks_to_trim} silent blocks removed "
                  f"({len(all_dbs)} → {len(trimmed_dbs)} blocks, "
                  f"{raw_dur}s → {dur}s)")

        # Trim the WAV snippet to match
        if snippet_path and blocks_to_trim > 0 and os.path.exists(snippet_path):
            self._trim_snippet_tail(snippet_path, blocks_to_trim, block_sec)

        # ── Snippet denoising ──────────────────────────────────────────
        # Self-adaptive spectral subtraction removes the omnipresent ambient
        # hiss ("seashore whoosh") from USB microphone recordings. Uses
        # minimum-statistics noise estimation — no manual noise profile needed.
        # Runs BEFORE normalization so gain boost amplifies the clean signal.
        if snippet_path and os.path.exists(snippet_path):
            if self.cfg["audio"].get("snippet_denoise", False):
                denoise_snippet(
                    snippet_path,
                    percentile=float(self.cfg["audio"].get("denoise_percentile", 10)),
                    alpha=float(self.cfg["audio"].get("denoise_alpha", 1.0)),
                    beta=float(self.cfg["audio"].get("denoise_beta", 0.02)),
                )

        # ── Snippet normalization ──────────────────────────────────────
        # USB mic recordings are typically -30 to -50 dBFS (nearly silent
        # on consumer playback devices). Normalize the WAV to a target peak
        # so evidence recordings are audible without cranking volume to max.
        # The calibrated dBA measurements in the DB are the quantitative
        # evidence; the WAV is qualitative corroboration, more useful when
        # audible. Only applies when snippet_normalize is enabled in config.
        if snippet_path and os.path.exists(snippet_path):
            target_peak = float(self.cfg["audio"].get("snippet_normalize_peak_dbfs", -6.0))
            if self.cfg["audio"].get("snippet_normalize", False):
                normalize_snippet(snippet_path, target_peak)

        # ── avg_db (on trimmed dbs — excludes silent tail) ─────────────
        # Exponential weighting so later (sustained) readings carry more
        # weight than the initial onset ramp.  For short incidents (<10
        # blocks), the weighting barely differs from a flat mean.
        if trimmed_dbs:
            n = len(trimmed_dbs)
            decay = 0.95
            weights = np.array([decay ** (n - 1 - i) for i in range(n)])
            weights /= weights.sum()
            avg_db = float(np.dot(weights, trimmed_dbs))
        else:
            avg_db = 0.0

        # Classification journal: determine the dominant source based on duration
        # each classification held. If multiple distinct sources were detected,
        # the primary classification becomes the longest-running one with "(multiple)"
        # appended. This prevents the common case where a 30-second mower incident
        # shows as "unknown (multiple)" just because the first 5 seconds were unknown
        # while the filter's min_history was building up.
        journal = self.active.get("class_journal", [])
        unique_classes = set(entry[1] for entry in journal)
        journal_json = json.dumps(journal) if journal else None
        updated_class = None

        if len(unique_classes) > 1 and len(journal) > 1:
            # Structural bookends: completely invisible to classification logic.
            # They don't add "+" and don't count as multiple sources.
            bookends = {"lead-in", "lead-out"}
            ignorable = {"engine_noise", "lead-in", "lead-out", "none", "unknown"}
            non_bookend_classes = unique_classes - bookends

            if len(non_bookend_classes) > 1:
                # Genuinely multiple non-bookend classes — compute durations
                durations = {}
                for idx in range(len(journal)):
                    cls = journal[idx][1]
                    start_sec = journal[idx][0]
                    if idx + 1 < len(journal):
                        end_sec = journal[idx + 1][0]
                    else:
                        end_sec = dur
                    durations[cls] = durations.get(cls, 0) + (end_sec - start_sec)

                meaningful = {k: v for k, v in durations.items() if k not in ignorable}
                if meaningful:
                    dominant = max(meaningful, key=meaningful.get)
                else:
                    dominant = max(durations, key=durations.get)

                if len(meaningful) <= 1:
                    updated_class = f"{dominant}+"
                else:
                    updated_class = f"{dominant} (multiple)"
            elif len(non_bookend_classes) == 1:
                # One real source alongside only bookends — no suffix
                meaningful_classes = non_bookend_classes - ignorable
                if meaningful_classes:
                    updated_class = next(iter(meaningful_classes))
                else:
                    updated_class = next(iter(non_bookend_classes))
            else:
                # All bookends (shouldn't happen in practice) — fall through
                pass

        elif len(unique_classes) == 1 and journal:
            # Single source — update classification to the journal's value in case
            # the initial classification was set before the filter had enough history
            updated_class = journal[0][1]

        self.storage.finalize_incident(
            self.active["id"], end.replace(microsecond=0).isoformat(), dur,
            # peak_db uses un-trimmed dbs — the peak is valid evidence
            round(max(all_dbs), 1) if all_dbs else 0.0,
            round(avg_db, 1),
            snippet_path,
            class_journal=journal_json,
            classification=updated_class,
        )
        self.ha.publish_event({"type": "incident_end", "id": self.active["id"], "duration_sec": dur})

        # Drive-by auto-dismiss: short incidents with a fade-out tail are likely passing
        # vehicles, not sustained nuisance noise. Reclassify as "drive_by", mark excluded,
        # and quarantine the snippet (moved to autodismissed/ for manual review).
        # Uses un-trimmed dbs + raw_dur — drive-by detection needs the full fade-out shape.
        incident_id = self.active["id"]
        dismissed = False
        if not force and self._looks_like_driveby(all_dbs, raw_dur):
            if snippet_path and os.path.exists(snippet_path):
                try:
                    quarantine_dir = os.path.join(os.path.dirname(snippet_path), "autodismissed")
                    os.makedirs(quarantine_dir, exist_ok=True)
                    quarantine_path = os.path.join(quarantine_dir, os.path.basename(snippet_path))
                    shutil.move(snippet_path, quarantine_path)
                except OSError as exc:
                    print(f"[engine] Failed to quarantine drive-by snippet {snippet_path}: {exc}")
            # Reclassify and mark as excluded rather than soft-deleting, so drive-bys
            # appear in the classification audit trail when viewing excluded incidents.
            with self.storage.conn() as c:
                c.execute("UPDATE incidents SET classification='drive_by', excluded=1 WHERE id=?", (incident_id,))
            print(f"[engine] Auto-classified incident {incident_id} as drive_by ({raw_dur:.1f}s, {len(all_dbs)} samples)")
            dismissed = True

        # Minimum incident duration: very short incidents (1–2 seconds of above-
        # threshold audio) are almost always impulse-level transients that shouldn't
        # have been recorded. Quarantine them the same way as drive-bys.
        # Compare against active_dur (time above threshold), not dur — dur
        # includes snippet_post_seconds tail padding that inflates the stored
        # duration. The preroll lead-in is also excluded (it precedes start_ts).
        min_sec = int(self.cfg["audio"].get("min_incident_seconds", 3))
        active_dur = max(1, round((self.active["last_above"] - self.active["start"]).total_seconds()))
        if not force and not dismissed and active_dur < min_sec:
            if snippet_path and os.path.exists(snippet_path):
                try:
                    quarantine_dir = os.path.join(os.path.dirname(snippet_path), "autodismissed")
                    os.makedirs(quarantine_dir, exist_ok=True)
                    quarantine_path = os.path.join(quarantine_dir, os.path.basename(snippet_path))
                    shutil.move(snippet_path, quarantine_path)
                except OSError as exc:
                    print(f"[engine] Failed to quarantine too-short snippet {snippet_path}: {exc}")
            with self.storage.conn() as c:
                c.execute("UPDATE incidents SET classification='too_short', excluded=1 WHERE id=?", (incident_id,))
            print(f"[engine] Auto-dismissed incident {incident_id} as too_short ({active_dur}s active < {min_sec}s minimum)")
            dismissed = True

        # Borderline auto-dismiss: when record_borderline_events is false, incidents
        # whose peak barely exceeds the threshold (within borderline_margin_db) are
        # auto-dismissed. These are likely calibration noise or marginal events that
        # clutter the incident log without providing actionable evidence.
        if not force and not dismissed:
            record_borderline = self.cfg["detection"].get("record_borderline_events", True)
            if not record_borderline:
                margin = float(self.cfg["detection"].get("borderline_margin_db", 10.0))
                threshold = float(self.active.get("threshold_db", 0))
                peak = round(max(all_dbs), 1) if all_dbs else 0.0
                excess = peak - threshold
                if 0 < excess <= margin:
                    if snippet_path and os.path.exists(snippet_path):
                        try:
                            quarantine_dir = os.path.join(os.path.dirname(snippet_path), "autodismissed")
                            os.makedirs(quarantine_dir, exist_ok=True)
                            quarantine_path = os.path.join(quarantine_dir, os.path.basename(snippet_path))
                            shutil.move(snippet_path, quarantine_path)
                        except OSError as exc:
                            print(f"[engine] Failed to quarantine borderline snippet {snippet_path}: {exc}")
                    with self.storage.conn() as c:
                        c.execute("UPDATE incidents SET classification='borderline', excluded=1 WHERE id=?", (incident_id,))
                    print(f"[engine] Auto-dismissed incident {incident_id} as borderline "
                          f"(peak {peak:.1f} dB, threshold {threshold:.1f} dB, excess {excess:.1f} dB ≤ margin {margin:.1f} dB)")
                    dismissed = True

        self.active = None
        self._stop_response()
        self.state.set(active_incident_id=None, mode="idle", forced_test=False)

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

        # Run snippet cleanup at engine startup (only if auto-purge is explicitly enabled)
        retention_days = int(self.cfg["audio"].get("retention_days", 30))
        snippets_dir = os.path.join(self.cfg["app"]["shared_dir"], "snippets")
        auto_purge = bool(self.cfg["audio"].get("auto_purge_enabled", False))
        if auto_purge:
            try:
                removed = self.storage.cleanup_old_snippets(retention_days, snippets_dir)
                if removed:
                    print(f"[engine] Startup cleanup removed {removed} expired snippet(s)")
            except Exception as e:
                print(f"[engine] Startup cleanup error: {e}")
        else:
            print(f"[engine] Auto-purge disabled — skipping snippet cleanup (retention_days={retention_days})")

        self._check_disk_quota()

        # Periodic DB vacuum to reclaim space from soft-deleted rows
        try:
            self.storage.vacuum()
            print("[engine] Startup DB vacuum complete")
        except Exception as e:
            print(f"[engine] DB vacuum error: {e}")

        last_cleanup = time.time()
        CLEANUP_INTERVAL = 86400  # Re-run cleanup once per day
        audio_fail_count = 0      # Consecutive audio I/O failures (for backoff)

        while self.running:
            try:
                if not self.state.snapshot()["armed"]:
                    time.sleep(0.25)
                    continue

                block = self.capture.read_block()
                self.state.set(mic_ok=True)
                audio_fail_count = 0  # Successful read — reset failure counter

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
                self.feature_history.append(features)
                self.feature_history = self.feature_history[-24:]  # Keep ~24 seconds of features
                bconf = beat_confidence(block, self.capture.sr, self.db_history)
                mscore = music_like_score(features)

                prev = self.db_history[-2] if len(self.db_history) > 1 else db_now
                filter_hit = self._identify_filter(features, db_now, prev,
                                                   beat_confidence=bconf)
                classify = filter_hit if filter_hit else self._classify_sound(mscore, bconf, features)

                # Track classification transitions in the active incident's journal.
                # Must run before split/finalize logic so the current block's source
                # is captured before any incident boundary decisions are made.
                # Only log journal entries for above-threshold blocks — sub-threshold
                # blocks during the song_gap_merge_sec tail are just waiting for the
                # gap to expire and would add spurious "unknown" entries that don't
                # represent a real source change.
                if self.active and db_now >= threshold:
                    self._update_class_journal(classify)

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

                if db_now >= threshold:
                    if filter_hit is None:
                        # Sound passed all filters — normal incident path.
                        # End any active excluded incident (filter condition ended).
                        if self._excluded_id:
                            self._end_excluded_incident()

                        if classify in ("music", "music_like") or self.cfg["detection"]["mode"] != "continuous_music_focus":
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
                            # Music-focus mode and not music — don't create incident
                            if self.active:
                                self.active["dbs"].append(db_now)
                                self._append_audio(block)
                    else:
                        # Filter caught this sound — but noise IS still present above threshold.
                        if self.active:
                            self.active["dbs"].append(db_now)
                            # Refresh last_above: the mic is hearing continuous above-threshold
                            # noise even though the source changed. Without this, the incident
                            # zombie-timeouts despite ongoing noise just because a filter matched.
                            self.active["last_above"] = datetime.now().astimezone()
                            self._append_audio(block)
                            # Journal already captures the classification transition (logged above).
                            # Don't log a separate excluded incident — would double-count.
                        else:
                            # No active incident — log as excluded if appropriate.
                            if self._should_log_excluded():
                                if self._excluded_id and self._excluded_filter == filter_hit:
                                    self._extend_excluded_incident(db_now)
                                else:
                                    if self._excluded_id:
                                        self._end_excluded_incident()
                                    mode = "record_only" if is_night(datetime.now(), self.cfg["detection"]["night_start_hour"], self.cfg["detection"]["night_end_hour"]) else "respond"
                                    self._begin_excluded_incident(filter_hit, db_now, threshold, mscore, bconf, mode)
                else:
                    # Below threshold — check gap-timeout for active incidents
                    if self.active:
                        self.active["dbs"].append(db_now)
                        self._append_audio(block)
                        gap = (datetime.now().astimezone() - self.active["last_above"]).total_seconds()
                        if gap >= float(self.cfg["detection"]["song_gap_merge_sec"]):
                            self._finalize_incident()
                    # End any active excluded incident (noise dropped below threshold)
                    if self._excluded_id:
                        self._end_excluded_incident()

                # Throttle MQTT to avoid flooding the broker (~120 msgs/min → ~12 msgs/min)
                now_ts = time.time()
                if now_ts - self._last_mqtt_publish >= self._mqtt_interval:
                    self.ha.publish_state(self.state.snapshot())
                    self._last_mqtt_publish = now_ts

                # Periodic snippet cleanup (once per day, only if auto-purge enabled)
                if time.time() - last_cleanup >= CLEANUP_INTERVAL:
                    if auto_purge:
                        try:
                            removed = self.storage.cleanup_old_snippets(retention_days, snippets_dir)
                            if removed:
                                print(f"[engine] Periodic cleanup removed {removed} expired snippet(s)")
                        except Exception as e:
                            print(f"[engine] Periodic cleanup error: {e}")
                    self._check_disk_quota()
                    self._check_memory_usage()
                    # Periodic device validation — catch slow drift or silent mic swap
                    ok, msg = self.capture.validate_device()
                    if not ok:
                        print(f"[engine] WARNING: Audio device changed since startup: {msg}")
                        self.state.set(mic_ok=False, last_error=f"Device drift: {msg}")
                    last_cleanup = time.time()

            except (sd.PortAudioError, RuntimeError, OSError) as e:
                # Audio I/O errors (USB disconnect, ALSA xrun, disk I/O, callback
                # timeout) are often transient. Reinitialize the capture device and
                # retry rather than spinning on a dead handle.
                audio_fail_count += 1
                error_msg = str(e)
                print(f"[engine] Audio I/O error #{audio_fail_count} (attempting reconnection): {error_msg}")
                self.state.set(mic_ok=False, last_error=error_msg, mode="error")

                # Force PortAudio to rescan devices — the device cache goes
                # stale when PulseAudio/PipeWire profiles change at runtime,
                # causing every reinit to find device index -1 indefinitely.
                AudioCapture.refresh_device_list()

                try:
                    # Close the old capture before creating a new one — prevents
                    # InputStream resource leak when callback mode is active.
                    self.capture.close()
                    a = self.cfg["audio"]
                    self.capture = AudioCapture(
                        sample_rate=int(a["sample_rate"]),
                        block_seconds=float(a["block_seconds"]),
                        channels=int(a.get("input_channels", 1)),
                        device=a.get("input_device")
                    )
                    print("[engine] Audio device reinitialized successfully")
                    audio_fail_count = 0  # Reset on success
                except Exception as reinit_err:
                    print(f"[engine] Audio reinit failed: {reinit_err}")

                    if audio_fail_count >= 10:
                        print(
                            "[engine] WARNING: 10 consecutive audio failures. "
                            "The audio device may be permanently unavailable. "
                            "Check USB connection and restart the service: "
                            "sudo systemctl restart noise-warden"
                        )

                # Escalating backoff: 2s, 2s, 4s, 8s, 16s, capped at 30s
                backoff = min(30.0, 2.0 * (2 ** max(0, audio_fail_count - 2)))
                time.sleep(backoff)

            except Exception as e:
                # Unexpected errors — log but don't crash the loop
                self.state.set(mic_ok=False, last_error=str(e), mode="error")
                print(f"[engine] Unexpected error in audio loop: {e}")
                time.sleep(1.0)
