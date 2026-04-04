import sqlite3
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    duration_sec REAL DEFAULT 0,
    start_db REAL,
    peak_db REAL,
    avg_db REAL,
    threshold_db REAL,
    music_like_score REAL,
    beat_confidence REAL,
    classification TEXT,
    mode TEXT,
    responded INTEGER DEFAULT 0,
    merge_count INTEGER DEFAULT 0,
    snippet_path TEXT,
    notes TEXT,
    deleted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calibration_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    offset_db REAL NOT NULL,
    created_ts TEXT NOT NULL
);
'''
class Storage:
    def __init__(self, db_path):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as c:
            c.executescript(SCHEMA); c.commit()
    def conn(self): return sqlite3.connect(self.db_path)
    def create_incident(self, d):
        with self.conn() as c:
            cur = c.execute('''INSERT INTO incidents (
                start_ts,start_db,peak_db,avg_db,threshold_db,music_like_score,beat_confidence,
                classification,mode,responded,merge_count,snippet_path,notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                d['start_ts'], d['start_db'], d['peak_db'], d['avg_db'], d['threshold_db'],
                d['music_like_score'], d['beat_confidence'], d['classification'], d['mode'],
                int(d['responded']), d['merge_count'], d.get('snippet_path'), d.get('notes')
            ))
            c.commit(); return cur.lastrowid
    def update_incident_end(self, iid, end_ts, duration_sec, peak_db, avg_db, merge_count):
        with self.conn() as c:
            c.execute('UPDATE incidents SET end_ts=?, duration_sec=?, peak_db=?, avg_db=?, merge_count=? WHERE id=?',
                      (end_ts, duration_sec, peak_db, avg_db, merge_count, iid)); c.commit()
    def list_incidents(self):
        with self.conn() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM incidents WHERE deleted=0 ORDER BY id DESC').fetchall()]
    def get_incident(self, iid):
        with self.conn() as c:
            c.row_factory = sqlite3.Row
            r = c.execute('SELECT * FROM incidents WHERE id=?', (iid,)).fetchone()
            return dict(r) if r else None
    def soft_delete_incident(self, iid):
        with self.conn() as c:
            c.execute('UPDATE incidents SET deleted=1 WHERE id=?', (iid,)); c.commit()
    def clear_incidents(self):
        with self.conn() as c:
            c.execute('UPDATE incidents SET deleted=1'); c.commit()
    def add_calibration_profile(self, name, offset_db, created_ts):
        with self.conn() as c:
            c.execute('INSERT INTO calibration_profiles(name, offset_db, created_ts) VALUES (?,?,?)', (name, offset_db, created_ts)); c.commit()
    def list_calibration_profiles(self):
        with self.conn() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM calibration_profiles ORDER BY id DESC').fetchall()]
