import subprocess
from pathlib import Path
class RelayController:
    def __init__(self, gpio_pin):
        self.enabled = False
        try:
            from gpiozero import OutputDevice
            self.dev = OutputDevice(gpio_pin, active_high=True, initial_value=False)
            self.enabled = True
        except Exception:
            self.dev = None
    def on(self):
        if self.enabled and self.dev: self.dev.on()
    def off(self):
        if self.enabled and self.dev: self.dev.off()
class PlaylistPlayer:
    def __init__(self, playlist_path, player_cmd):
        self.playlist_path = Path(playlist_path); self.player_cmd = player_cmd; self.proc = None
    def start(self):
        if self.proc and self.proc.poll() is None: return
        files = sorted([str(p) for p in self.playlist_path.glob('*') if p.is_file()])
        if not files: return
        self.proc = subprocess.Popen(self.player_cmd.split() + [files[0]])
    def stop(self):
        if self.proc and self.proc.poll() is None: self.proc.terminate()
        self.proc = None
