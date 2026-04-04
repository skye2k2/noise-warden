import csv, os, sqlite3
from datetime import datetime
from pathlib import Path

class IncidentDB:
    def __init__(self, db_path: str, export_dir: str):
        self.db_path = db_path
        self.export_dir = export_dir
        os.makedirs(Path(db_path).parent, exist_ok=True)
        os.makedirs(export_dir, exist_ok=True)
        self._init()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init(self):
        with self._conn() as c:
            c.execute('''
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                peak_db REAL,
                initial_db REAL,
                duration_seconds REAL,
                classification TEXT,
                action_taken INTEGER,
                record_only INTEGER,
                snippet_path TEXT,
                notes TEXT
            )
            ''')
            c.commit()

    def create_incident(self, started_at, peak_db, initial_db, classification, action_taken, record_only, snippet_path):
        with self._conn() as c:
            cur = c.execute(
                '''INSERT INTO incidents
                (started_at, peak_db, initial_db, duration_seconds, classification, action_taken, record_only, snippet_path, notes)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?, '')''',
                (started_at, peak_db, initial_db, classification, int(action_taken), int(record_only), snippet_path)
            )
            c.commit()
            return cur.lastrowid

    def close_incident(self, incident_id, ended_at, peak_db, duration_seconds):
        with self._conn() as c:
            c.execute("UPDATE incidents SET ended_at=?, peak_db=?, duration_seconds=? WHERE id=?",
                      (ended_at, peak_db, duration_seconds, incident_id))
            c.commit()

    def list_incidents(self, limit=200, offset=0):
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM incidents ORDER BY started_at DESC LIMIT ? OFFSET ?",
                             (limit, offset)).fetchall()
            return [dict(r) for r in rows]

    def get_incident(self, incident_id):
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            return dict(row) if row else None

    def delete_incident(self, incident_id):
        item = self.get_incident(incident_id)
        if item and item.get("snippet_path") and os.path.exists(item["snippet_path"]):
            try: os.remove(item["snippet_path"])
            except OSError: pass
        with self._conn() as c:
            c.execute("DELETE FROM incidents WHERE id=?", (incident_id,))
            c.commit()

    def clear_incidents(self):
        for item in self.list_incidents(limit=100000, offset=0):
            if item.get("snippet_path") and os.path.exists(item["snippet_path"]):
                try: os.remove(item["snippet_path"])
                except OSError: pass
        with self._conn() as c:
            c.execute("DELETE FROM incidents")
            c.commit()

    def export_csv(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.export_dir, f"incidents_{ts}.csv")
        rows = self.list_incidents(limit=100000, offset=0)
        with open(path, "w", newline="", encoding="utf-8") as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            else:
                writer = csv.writer(f)
                writer.writerow(["id", "started_at", "ended_at"])
        return path

    def timeline(self, span="day"):
        return self.list_incidents(limit=100000, offset=0)
