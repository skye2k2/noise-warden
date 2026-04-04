from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class RuntimeState:
    armed: bool = True
    manual_kill: bool = False
    active_incident_id: Optional[int] = None
    response_active: bool = False
    current_db: float = 0.0
    current_slow_db: float = 0.0
    current_fast_db: float = 0.0
    last_classification: str = "idle"
    last_update: Optional[str] = None

    def snapshot(self):
        d = asdict(self)
        d["last_update"] = datetime.now().isoformat()
        return d
