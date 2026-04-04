import sqlite3
from pathlib import Path
DB_PATH = Path("data/noise_warden.db")
SCHEMA = '''
CREATE TABLE IF NOT EXISTS incidents (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 start_ts TEXT NOT NULL,
 end_ts TEXT,
 initial_db REAL NOT NULL,
 peak_db REAL NOT NULL,
 avg_db REAL NOT NULL,
 mode TEXT NOT NULL,
 classification TEXT,
 snippet_path TEXT,
 notes TEXT
);
CREATE TABLE IF NOT EXISTS build_info (
 id INTEGER PRIMARY KEY CHECK (id = 1),
 photo_path TEXT,
 details TEXT
);
INSERT OR IGNORE INTO build_info (id, photo_path, details) VALUES (1, NULL, '');
'''
class IncidentStore:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.executescript(SCHEMA); self.conn.commit()
    def start_incident(self, start_ts, initial_db, mode, classification):
        cur = self.conn.cursor()
        cur.execute("INSERT INTO incidents (start_ts, initial_db, peak_db, avg_db, mode, classification) VALUES (?, ?, ?, ?, ?, ?)",
                    (start_ts.isoformat(), initial_db, initial_db, initial_db, mode, classification))
        self.conn.commit(); return cur.lastrowid
    def update_incident(self, incident_id, peak_db, avg_db):
        self.conn.execute("UPDATE incidents SET peak_db = MAX(peak_db, ?), avg_db = ? WHERE id = ?", (peak_db, avg_db, incident_id)); self.conn.commit()
    def end_incident(self, incident_id, end_ts, snippet_path):
        self.conn.execute("UPDATE incidents SET end_ts = ?, snippet_path = ? WHERE id = ?", (end_ts.isoformat(), snippet_path, incident_id)); self.conn.commit()
    def list_incidents(self):
        cur = self.conn.cursor(); rows = cur.execute("SELECT * FROM incidents ORDER BY start_ts DESC").fetchall()
        cols = [d[0] for d in cur.description]; return [dict(zip(cols, r)) for r in rows]
    def delete_incident(self, incident_id):
        row = self.conn.execute("SELECT snippet_path FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if row and row[0]:
            try: Path(row[0]).unlink(missing_ok=True)
            except Exception: pass
        self.conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,)); self.conn.commit()
    def clear_incidents(self):
        for (p,) in self.conn.execute("SELECT snippet_path FROM incidents").fetchall():
            if p:
                try: Path(p).unlink(missing_ok=True)
                except Exception: pass
        self.conn.execute("DELETE FROM incidents"); self.conn.commit()
    def get_build_info(self):
        row = self.conn.execute("SELECT photo_path, details FROM build_info WHERE id = 1").fetchone()
        return {"photo_path": row[0], "details": row[1]}
    def set_build_info(self, photo_path, details):
        self.conn.execute("UPDATE build_info SET photo_path = ?, details = ? WHERE id = 1", (photo_path, details or "")); self.conn.commit()
