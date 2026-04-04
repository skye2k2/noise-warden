from __future__ import annotations
import threading
import copy
from datetime import datetime, timezone

class StateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "armed": True,
            "running": False,
            "mic_ok": False,
            "last_error": None,
            "current_db": 0.0,
            "current_threshold_db": 0.0,
            "mode": "idle",
            "active_incident_id": None,
            "updated_at": None,
        }

    def set(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)
            self._state["updated_at"] = datetime.now(timezone.utc).isoformat()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)
