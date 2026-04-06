from __future__ import annotations
import threading
import copy
from datetime import datetime, timezone

class StateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "active_incident_id": None,
            "armed": True,
            "current_db": 0.0,
            "current_threshold_db": 0.0,
            "disk_free_mb": None,
            "disk_warning": None,
            "last_error": None,
            "mic_ok": False,
            "mode": "idle",
            "responding": False,
            "running": False,
            "updated_at": None,
        }

    def set(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)
            self._state["updated_at"] = datetime.now().astimezone().replace(microsecond=0).isoformat()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)
