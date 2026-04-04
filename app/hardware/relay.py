from gpiozero import OutputDevice
class RelayController:
    def __init__(self, pin, active_high=True, enabled=True):
        self.dev = OutputDevice(pin, active_high=active_high, initial_value=False) if enabled else None
    def on(self):
        if self.dev: self.dev.on()
    def off(self):
        if self.dev: self.dev.off()
