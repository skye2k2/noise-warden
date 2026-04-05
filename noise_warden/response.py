from __future__ import annotations
import glob, os, random, shlex, subprocess, time

# ---------------------------------------------------------------------------
# GPIO HARDWARE CONTROL — DISABLED BY DEFAULT
#
# Set _RELAY_HW_ENABLED = True to activate real GPIO relay control via gpiozero.
# Requires: `pip install gpiozero` and `pip install lgpio` (or RPi.GPIO) on the Pi.
# On non-Pi systems (dev machines, CI), leave False — the relay degrades to a
# boolean flag with no hardware interaction.
#
# When enabled, RelayController drives a physical GPIO pin to power an amplifier
# relay module. When disabled, on()/off() only toggle self.enabled for state
# tracking (the engine's response flow still runs, but no pin changes).
# ---------------------------------------------------------------------------
_RELAY_HW_ENABLED = False

_gpiozero_available = False
if _RELAY_HW_ENABLED:
    try:
        from gpiozero import OutputDevice  # type: ignore[import-untyped]
        _gpiozero_available = True
    except ImportError:
        print("[relay] WARNING: _RELAY_HW_ENABLED is True but gpiozero is not installed. "
              "Falling back to boolean-only relay. Install with: pip install gpiozero lgpio")


class RelayController:
    """Controls a GPIO-driven relay for powering an amplifier during response playback.

    When _RELAY_HW_ENABLED is True and gpiozero is available, on()/off() drive
    a physical GPIO pin. Otherwise, they toggle a boolean flag only.

    The optional amp_power_on_delay_sec introduces a pause after relay activation
    to allow the amplifier's power supply to stabilize before audio playback begins.
    Most Class D amp boards need 0.5–2 seconds."""

    def __init__(self, gpio_pin: int, active_high: bool = True,
                 amp_power_on_delay_sec: float = 0.0):
        self.gpio_pin = gpio_pin
        self.active_high = active_high
        self.amp_power_on_delay_sec = amp_power_on_delay_sec
        self.enabled = False
        self._device = None

        if _RELAY_HW_ENABLED and _gpiozero_available:
            try:
                self._device = OutputDevice(
                    gpio_pin, active_high=active_high, initial_value=False
                )
                print(f"[relay] GPIO pin {gpio_pin} initialized (active_high={active_high})")
            except Exception as exc:
                # Catch broad exceptions — gpiozero can raise GPIOError, PinError,
                # RuntimeError depending on platform and pin factory availability.
                print(f"[relay] WARNING: GPIO init failed for pin {gpio_pin}: {exc}. "
                      "Falling back to boolean-only relay.")
                self._device = None

    def on(self):
        """Activate the relay (power on the amplifier)."""
        self.enabled = True
        if self._device:
            self._device.on()
            if self.amp_power_on_delay_sec > 0:
                # Allow amp PSU to stabilize before audio starts playing.
                # This is blocking but only runs at incident-start, not in the hot loop.
                time.sleep(self.amp_power_on_delay_sec)

    def off(self):
        """Deactivate the relay (power off the amplifier)."""
        self.enabled = False
        if self._device:
            self._device.off()

    def cleanup(self):
        """Release the GPIO pin. Call during engine shutdown to avoid resource leaks."""
        self.enabled = False
        if self._device:
            try:
                self._device.off()
                self._device.close()
            except Exception:
                pass
            self._device = None

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
        # shlex.split handles quoted paths and avoids shell metacharacter injection
        args = shlex.split(self.player_command)
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
