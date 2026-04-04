from __future__ import annotations
import json
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

class HAClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = None
        if cfg["home_assistant"].get("enabled") and cfg["home_assistant"].get("mode") == "mqtt" and mqtt:
            self.client = mqtt.Client()
            u = cfg["home_assistant"].get("mqtt_username") or None
            p = cfg["home_assistant"].get("mqtt_password") or None
            if u:
                self.client.username_pw_set(u, p)
            self.client.connect_async(cfg["home_assistant"]["mqtt_host"], int(cfg["home_assistant"]["mqtt_port"]), 60)
            self.client.loop_start()

    def publish_state(self, state: dict):
        if not self.client:
            return
        prefix = self.cfg["home_assistant"]["mqtt_topic_prefix"]
        self.client.publish(f"{prefix}/state", json.dumps(state), retain=True)

    def publish_event(self, event: dict):
        if not self.client:
            return
        prefix = self.cfg["home_assistant"]["mqtt_topic_prefix"]
        self.client.publish(f"{prefix}/event", json.dumps(event), retain=False)
