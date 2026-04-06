from __future__ import annotations
import os, sqlite3, csv, io, glob, time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start_ts TEXT NOT NULL,
  end_ts TEXT,
  duration_sec REAL,
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

CREATE INDEX IF NOT EXISTS idx_incidents_start_ts ON incidents(start_ts DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_deleted ON incidents(deleted);

CREATE TABLE IF NOT EXISTS build_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  notes TEXT DEFAULT '',
  ordinance_excerpt TEXT DEFAULT ''
);
INSERT OR IGNORE INTO build_meta (id, notes, ordinance_excerpt) VALUES (1, '', '');

CREATE TABLE IF NOT EXISTS calibration_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  offset_db REAL NOT NULL,
  created_ts TEXT NOT NULL
);
'''

# Increment this when adding migrations. Each migration runs exactly once.
SCHEMA_VERSION = 1

class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with self.conn() as c:
            # WAL mode: allows concurrent reads during engine writes (critical on Pi)
            c.execute("PRAGMA journal_mode=WAL")
            # NORMAL sync is safe with WAL — trades a tiny crash-window for much better
            # write throughput on the Pi's slow I/O
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript(SCHEMA)
            self._run_migrations(c)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _run_migrations(self, c):
        """Run any pending schema migrations. Uses SQLite's built-in user_version pragma
        to track the current schema version. Each migration block runs exactly once,
        guarded by a version check. Add new migrations at the end with the next version number."""
        current = c.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            return

        # Migration 1: baseline — indexes added via SCHEMA, WAL mode set in __init__
        if current < 1:
            print(f"[storage] Running migration to schema version 1")
            # Indexes are created by SCHEMA above; this just stamps the version
            pass

        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        print(f"[storage] Schema version now {SCHEMA_VERSION}")

    def create_incident(self, row: dict) -> int:
        with self.conn() as c:
            cur = c.execute('''
                INSERT INTO incidents (
                    start_ts,start_db,peak_db,avg_db,threshold_db,music_like_score,
                    beat_confidence,classification,mode,responded,merge_count,snippet_path,notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                row["start_ts"], row["start_db"], row["peak_db"], row["avg_db"], row["threshold_db"],
                row["music_like_score"], row["beat_confidence"], row["classification"], row["mode"],
                int(row.get("responded", 0)), int(row.get("merge_count", 0)), row.get("snippet_path"), row.get("notes", "")
            ))
            return int(cur.lastrowid)

    def finalize_incident(self, incident_id: int, end_ts: str, duration_sec: float, peak_db: float, avg_db: float, snippet_path: str | None):
        with self.conn() as c:
            c.execute('''
                UPDATE incidents
                SET end_ts=?, duration_sec=?, peak_db=?, avg_db=?, snippet_path=?
                WHERE id=?
            ''', (end_ts, duration_sec, peak_db, avg_db, snippet_path, incident_id))

    def update_incident_notes(self, incident_id: int, notes: str):
        with self.conn() as c:
            c.execute("UPDATE incidents SET notes=? WHERE id=?", (notes, incident_id))

    def soft_delete_incident(self, incident_id: int):
        with self.conn() as c:
            c.execute("UPDATE incidents SET deleted=1 WHERE id=?", (incident_id,))

    def soft_delete_all_incidents(self):
        """Soft-delete all non-deleted incidents."""
        with self.conn() as c:
            c.execute("UPDATE incidents SET deleted=1 WHERE deleted=0")

    def get_incident(self, incident_id: int):
        """Fetch a single incident by ID (returns None if not found or soft-deleted)."""
        with self.conn() as c:
            row = c.execute("SELECT * FROM incidents WHERE id=? AND deleted=0", (incident_id,)).fetchone()
            return dict(row) if row else None

    def list_incidents(self, limit=200, offset=0, since=None):
        q = "SELECT * FROM incidents WHERE deleted=0"
        params = []
        if since:
            q += " AND start_ts >= ?"
            params.append(since)
        q += " ORDER BY start_ts DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    def count_incidents(self, since=None):
        q = "SELECT COUNT(*) FROM incidents WHERE deleted=0"
        params = []
        if since:
            q += " AND start_ts >= ?"
            params.append(since)
        with self.conn() as c:
            return int(c.execute(q, params).fetchone()[0])

    def export_csv(self):
        rows = self.list_incidents(limit=100000, offset=0)
        out = io.StringIO()
        if not rows:
            return ""
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return out.getvalue()

    def get_build_meta(self):
        with self.conn() as c:
            r = c.execute("SELECT * FROM build_meta WHERE id=1").fetchone()
            return dict(r)

    def save_build_meta(self, notes: str, ordinance_excerpt: str):
        with self.conn() as c:
            c.execute("UPDATE build_meta SET notes=?, ordinance_excerpt=? WHERE id=1", (notes, ordinance_excerpt))

    def add_calibration_profile(self, name: str, offset_db: float, created_ts: str):
        """Store a named calibration profile (offset computed from reference SPL vs. observed dBFS)."""
        with self.conn() as c:
            c.execute(
                "INSERT INTO calibration_profiles (name, offset_db, created_ts) VALUES (?,?,?)",
                (name, offset_db, created_ts)
            )

    def list_calibration_profiles(self):
        """Return all calibration profiles, newest first."""
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM calibration_profiles ORDER BY id DESC").fetchall()]

    def delete_calibration_profile(self, profile_id: int):
        """Delete a calibration profile by ID."""
        with self.conn() as c:
            c.execute("DELETE FROM calibration_profiles WHERE id = ?", (profile_id,))

    def cleanup_old_snippets(self, retention_days: int, snippets_dir: str = None):
        """Remove snippet files older than retention_days. Also purges the autodismissed/
        quarantine folder of files older than the same retention window."""
        cutoff = datetime.now().astimezone() - timedelta(days=retention_days)
        removed = 0
        with self.conn() as c:
            rows = c.execute("SELECT id, snippet_path, start_ts FROM incidents WHERE deleted=0 AND snippet_path IS NOT NULL").fetchall()
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["start_ts"])
                except Exception:
                    continue
                if ts < cutoff:
                    p = r["snippet_path"]
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                            removed += 1
                        except Exception:
                            pass

        # Purge old quarantined drive-by snippets (moved, not deleted, by the engine)
        if snippets_dir:
            removed += self._cleanup_autodismissed(snippets_dir, retention_days)
        return removed

    @staticmethod
    def _cleanup_autodismissed(snippets_dir: str, retention_days: int):
        """Remove quarantined drive-by snippets older than retention_days.
        These live in snippets/autodismissed/ and were preserved for manual review."""
        quarantine_dir = os.path.join(snippets_dir, "autodismissed")
        removed = 0
        for filepath in glob.glob(os.path.join(quarantine_dir, "*.wav")):
            try:
                age_days = (time.time() - os.path.getmtime(filepath)) / 86400
                if age_days > retention_days:
                    os.remove(filepath)
                    removed += 1
            except OSError:
                pass
        return removed

    def repair_stale_incidents(self):
        """Finalize any incidents left without an end_ts (abandoned after a crash).
        Marks them with a sentinel end_ts and notes indicating they were crash-repaired."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, start_ts, start_db FROM incidents WHERE end_ts IS NULL AND deleted=0"
            ).fetchall()
            repaired = 0
            for r in rows:
                # Use the start_ts as a fallback end_ts — we can't know the real end
                c.execute(
                    "UPDATE incidents SET end_ts=?, duration_sec=0, notes=COALESCE(notes,'') || ? WHERE id=?",
                    (r["start_ts"], " [crash-repaired: incident was active when engine stopped unexpectedly]", r["id"])
                )
                repaired += 1
            return repaired

    def vacuum(self):
        """Reclaim disk space from soft-deleted rows and fragmentation.
        Must run outside a transaction (SQLite requirement for VACUUM)."""
        c = sqlite3.connect(self.db_path)
        try:
            c.execute("VACUUM")
        finally:
            c.close()
