import subprocess, time
class PlaybackController:
    def __init__(self, cfg: dict, runtime):
        self.cfg = cfg
        self.runtime = runtime
        self.proc = None
    def start(self):
        if not self.cfg["playback"]["enabled"] or self.runtime.playback_active:
            return
        cmd = [self.cfg["playback"]["vlc_binary"], "--loop", self.cfg["playback"]["playlist_path"]]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.runtime.playback_active = True
        except Exception as e:
            self.runtime.last_error = f"playback start failed: {e}"
    def stop(self):
        if self.proc:
            try:
                self.proc.terminate(); self.proc.wait(timeout=3)
            except Exception:
                try: self.proc.kill()
                except Exception: pass
        self.proc = None
        self.runtime.playback_active = False
    def test(self):
        self.start(); time.sleep(3); self.stop()
