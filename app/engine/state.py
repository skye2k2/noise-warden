from dataclasses import dataclass
@dataclass
class RuntimeState:
    armed: bool = True
    active: bool = False
    current_mode: str = "standby"
    last_db: float = -120.0
    last_classification: str | None = None
    last_update: str | None = None
    ha_status: str = "UNKNOWN"
    current_incident_id: int | None = None
    current_incident_start = None
    peak_db: float = -120.0
    sum_db: float = 0.0
    count_db: int = 0
    pending_gap_since = None
