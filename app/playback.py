from __future__ import annotations
import time
import threading
from datetime import datetime, timedelta

try:
    from gpiozero import OutputDevice
except Exception:
    OutputDevice = None

try:
    import vlc
except Exception:
    vlc = None


class PlaybackController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.amp = None
        self.player = None
        self.playing = False
        self.started_at = None
        self.last_stopped_at = None

        if OutputDevice is not None:
            self.amp = OutputDevice(cfg.amp_gpio_pin, active_high=True, initial_value=False)

    def _amp_on(self):
        if self.amp:
            self.amp.on()
        time.sleep(self.cfg.amp_power_on_delay_ms / 1000.0)

    def _amp_off(self):
        time.sleep(self.cfg.amp_power_off_delay_ms / 1000.0)
        if self.amp:
            self.amp.off()

    def start(self):
        if not self.cfg.enabled or self.playing:
            return False
        self._amp_on()

        if self.cfg.player == "vlc" and vlc is not None:
            inst = vlc.Instance("--quiet")
            media_list = inst.media_list_new([self.cfg.playlist_path])
            mlp = inst.media_list_player_new()
            mlp.set_media_list(media_list)
            mlp.play()
            self.player = mlp
        else:
            # if vlc unavailable, we still simulate amp control
            self.player = None

        self.playing = True
        self.started_at = datetime.now()
        return True

    def stop(self):
        if not self.playing:
            return False
        if self.player is not None:
            try:
                self.player.stop()
            except Exception:
                pass
            self.player = None

        self._amp_off()
        self.playing = False
        self.last_stopped_at = datetime.now()
        self.started_at = None
        return True

    def is_suppression_active(self, suppress_while_playing: bool, suppress_after_stop_seconds: float) -> bool:
        if suppress_while_playing and self.playing:
            return True
        if self.last_stopped_at is None:
            return False
        return (datetime.now() - self.last_stopped_at).total_seconds() < suppress_after_stop_seconds

    def exceeded_max_play(self) -> bool:
        if not self.playing or not self.started_at:
            return False
        return datetime.now() - self.started_at > timedelta(minutes=self.cfg.max_play_minutes)
