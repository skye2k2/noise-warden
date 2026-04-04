from __future__ import annotations
import json
import threading
import time
from datetime import datetime

from app.audio.input import AudioInput
from app.audio.a_weighting import apply_a_weighting
from app.audio.features import FeatureExtractor
from app.classifier import NoiseClassifier
from app.storage import IncidentStore
from app.playback import PlaybackController
from app.state import RuntimeState
from app.utils.time_utils import day_or_night, is_day


class NoiseWardenEngine:
    def __init__(self, settings):
        self.settings = settings
        self.audio = AudioInput(
            device_name=settings.audio.input_device_name,
            sample_rate=settings.audio.sample_rate,
            channels=settings.audio.channels,
            block_seconds=settings.audio.block_seconds,
        )
        self.features = FeatureExtractor(
            fs=settings.audio.sample_rate,
            bass_low=settings.classification.bass_band_low_hz,
            bass_high=settings.classification.bass_band_high_hz,
            mower_low=settings.classification.mower_tonal_band_low_hz,
            mower_high=settings.classification.mower_tonal_band_high_hz,
        )
        self.classifier = NoiseClassifier(settings.classification)
        self.store = IncidentStore(settings.logging.db_path)
        self.playback = PlaybackController(settings.playback)
        self.state = RuntimeState()

        self._thread = None
        self._stop = threading.Event()

        self._trigger_accum = 0.0
        self._clear_accum = 0.0
        self._incident_started_at = None
        self._a_zi = None

    def start(self):
        self.audio.start()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.store.log_state("engine", "started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self.audio.stop()
        self.playback.stop()
        self.store.log_state("engine", "stopped")

    def _start_incident(self, now, cls, feat, retaliated):
        iid = self.store.create_incident(
            started_at=now,
            day_or_night=day_or_night(now),
            threshold_db=cls.threshold_db,
            mode=cls.mode,
            retaliated=retaliated,
            notes_json=json.dumps(cls.notes),
        )
        self.state.active_incident_id = iid
        self._incident_started_at = now
        self.store.log_state("incident_start", str(iid))

    def _end_incident(self, now):
        if self.state.active_incident_id is not None:
            self.store.close_incident(self.state.active_incident_id, now)
            self.store.log_state("incident_end", str(self.state.active_incident_id))
        self.state.active_incident_id = None
        self._incident_started_at = None
        self._trigger_accum = 0.0
        self._clear_accum = 0.0

    def _run_loop(self):
        block_dt = self.settings.audio.block_seconds
        calib = self.settings.audio.calibration_offset_db

        while not self._stop.is_set():
            try:
                x = self.audio.read_block(timeout=1.0)
            except Exception:
                continue

            # A-weighting approximation
            y, self._a_zi = apply_a_weighting(x, self.settings.audio.sample_rate, self._a_zi)
            y = y.astype("float64")

            feat = self.features.extract(y)
            feat.rms_db += calib
            feat.slow_db += calib
            feat.fast_db += calib

            now = datetime.now()
            self.state.current_db = feat.rms_db
            self.state.current_slow_db = feat.slow_db
            self.state.current_fast_db = feat.fast_db

            # self-playback suppression
            suppressed = self.playback.is_suppression_active(
                self.settings.audio.suppress_detection_while_playing,
                self.settings.audio.suppress_after_stop_seconds,
            )

            cls = self.classifier.classify(feat, now)
            if suppressed:
                cls.should_trigger = False
                cls.notes["suppressed_by_playback"] = True

            self.state.last_classification = (
                "music" if cls.is_music_like else
                "bass_pulse" if cls.is_bass_pulse_like else
                "mower_like" if cls.is_mower_like else
                "intermittent" if cls.is_intermittent_like else
                "impulse" if cls.is_impulse else
                cls.mode
            )

            # Incident lifecycle
            if cls.is_above_threshold:
                if self.state.active_incident_id is None:
                    # Start logging as soon as ordinance-like threshold is crossed
                    retaliated = False
                    self._start_incident(now, cls, feat, retaliated)
                self.store.update_incident_peak(self.state.active_incident_id, feat.slow_db)

            # Trigger policy
            can_retaliate = (
                self.state.armed
                and not self.state.manual_kill
                and self.settings.playback.enabled
                and is_day(now)
            )
            if self.settings.classification.night_record_only and not is_day(now):
                can_retaliate = False

            if cls.should_trigger and can_retaliate:
                self._trigger_accum += block_dt
                self._clear_accum = 0.0
            else:
                self._clear_accum += block_dt
                self._trigger_accum = max(0.0, self._trigger_accum - block_dt * 0.5)

            if (
                not self.state.response_active
                and self._trigger_accum >= self.settings.classification.trigger_persist_seconds
            ):
                if self.playback.start():
                    self.state.response_active = True
                    self.store.log_state("response", "started")

            if self.playback.exceeded_max_play():
                self.playback.stop()
                self.state.response_active = False
                self.store.log_state("response", "stopped_max_runtime")

            if self.state.response_active and self._clear_accum >= self.settings.classification.clear_below_seconds:
                self.playback.stop()
                self.state.response_active = False
                self.store.log_state("response", "stopped_cleared")

            # End incident after sustained below-threshold
            if self.state.active_incident_id is not None and not cls.is_above_threshold:
                self._clear_accum += block_dt
                if self._clear_accum >= self.settings.classification.clear_below_seconds:
                    self._end_incident(now)

            self.state.last_update = now.isoformat()

    # external control
    def arm(self):
        self.state.armed = True
        self.state.manual_kill = False
        self.store.log_state("armed", "true")

    def disarm(self):
        self.state.armed = False
        self.store.log_state("armed", "false")

    def emergency_kill(self):
        self.state.manual_kill = True
        self.playback.stop()
        self.state.response_active = False
        self.store.log_state("manual_kill", "true")

    def clear_manual_kill(self):
        self.state.manual_kill = False
        self.store.log_state("manual_kill", "false")

    def get_status(self):
        return self.state.snapshot()
