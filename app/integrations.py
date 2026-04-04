class HomeAssistantMonitor:
    def __init__(self, cfg: dict, runtime):
        self.cfg = cfg
        self.runtime = runtime
    def poll(self):
        if self.cfg["integrations"]["home_assistant"]["enabled"]:
            self.runtime.last_ha_ok = None  # UNKNOWN unless explicitly confirmed

class MQTTPublisher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
    def publish_status(self, payload: dict):
        return
