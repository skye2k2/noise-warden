from pathlib import Path
import subprocess, random
class PlaylistPlayer:
    def __init__(self, playlist_dir, command_template):
        self.playlist_dir = Path(playlist_dir); self.command_template = command_template; self.proc = None
    def start(self):
        files = [p for p in self.playlist_dir.glob('*') if p.is_file()]
        if not files: return
        target = random.choice(files)
        self.proc = subprocess.Popen(self.command_template.format(file=str(target)).split())
    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(timeout=2)
            except Exception: self.proc.kill()
        self.proc = None
