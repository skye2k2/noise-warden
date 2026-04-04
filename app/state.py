from dataclasses import dataclass, field
from app.db import IncidentDB

@dataclass
class RuntimeState:
    armed: bool = True
    emergency_kill: bool = False
    current_db_slow: float = 0.0
    current_db_fast: float = 0.0
    classification: str = "idle"
    incident_active: bool = False
    playback_active: bool = False
    record_only_now: bool = False
    last_ha_ok: bool | None = None
    last_error: str | None = None
    db: IncidentDB | None = None
    recent_status_cache: dict = field(default_factory=dict)

    def status_payload(self):
        payload = {
            "armed": self.armed,
            "emergency_kill": self.emergency_kill,
            "current_db_slow": round(self.current_db_slow, 2),
            "current_db_fast": round(self.current_db_fast, 2),
            "classification": self.classification,
            "incident_active": self.incident_active,
            "playback_active": self.playback_active,
            "record_only_now": self.record_only_now,
            "home_assistant_state": (
                "CONNECTED" if self.last_ha_ok is True else
                "UNKNOWN" if self.last_ha_ok is None else
                "DISCONNECTED"
            ),
            "last_error": self.last_error,
        }
        self.recent_status_cache = payload
        return payload

_runtime = None
def get_runtime():
    global _runtime
    if _runtime is None:
        _runtime = RuntimeState()
    return _runtime
