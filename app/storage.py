from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any


class IncidentStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        cur = self.conn.cursor()
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                day_or_night TEXT NOT NULL,
                peak_db REAL NOT NULL,
                threshold_db REAL NOT NULL,
                mode TEXT NOT NULL,
                retaliated INTEGER NOT NULL,
                notes_json TEXT NOT NULL
            )
            '''
        )
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS state_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL
            )
            '''
        )
        self.conn.commit()

    def create_incident(self, started_at: datetime, day_or_night: str, threshold_db: float, mode: str, retaliated: bool, notes_json: str) -> int:
        cur = self.conn.cursor()
        cur.execute(
            '''
            INSERT INTO incidents (started_at, ended_at, day_or_night, peak_db, threshold_db, mode, retaliated, notes_json)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
            ''',
            (started_at.isoformat(), day_or_night, 0.0, threshold_db, mode, int(retaliated), notes_json),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_incident_peak(self, incident_id: int, peak_db: float):
        cur = self.conn.cursor()
        cur.execute("UPDATE incidents SET peak_db = MAX(peak_db, ?) WHERE id = ?", (peak_db, incident_id))
        self.conn.commit()

    def close_incident(self, incident_id: int, ended_at: datetime):
        cur = self.conn.cursor()
        cur.execute("UPDATE incidents SET ended_at = ? WHERE id = ?", (ended_at.isoformat(), incident_id))
        self.conn.commit()

    def list_incidents(self, limit: int = 200):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def clear_incidents(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM incidents")
        self.conn.commit()

    def log_state(self, key: str, value: str):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO state_log (ts, key, value) VALUES (?, ?, ?)", (datetime.now().isoformat(), key, value))
        self.conn.commit()

    def get_state_log(self, limit: int = 200):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM state_log ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def purge_old(self, retention_days: int):
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        cur = self.conn.cursor()
        cur.execute("DELETE FROM incidents WHERE started_at < ?", (cutoff,))
        cur.execute("DELETE FROM state_log WHERE ts < ?", (cutoff,))
        self.conn.commit()
