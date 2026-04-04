from __future__ import annotations
import glob, os, random, subprocess

class RelayController:
    def __init__(self, gpio_pin: int):
        self.gpio_pin = gpio_pin
        self.enabled = False

    def on(self):
        self.enabled = True

    def off(self):
        self.enabled = False

class PlaylistPlayer:
    # Supported audio extensions for playlist file selection
    AUDIO_EXTENSIONS = ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a")

    def __init__(self, player_command: str, playlist_dir: str):
        self.player_command = player_command
        self.playlist_dir = playlist_dir
        self.proc = None

    def _pick_file(self):
        """Select a random audio file from the playlist directory, if any exist."""
        files = []
        for ext in self.AUDIO_EXTENSIONS:
            files.extend(glob.glob(os.path.join(self.playlist_dir, ext)))
        if not files:
            return None
        return random.choice(files)

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        track = self._pick_file()
        if not track:
            return
        args = self.player_command.split()
        args.append(track)
        self.proc = subprocess.Popen(args)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
        self.proc = None
