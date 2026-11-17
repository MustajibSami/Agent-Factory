"""The single source of truth: job cards and the event log, in one SQLite file.

Rooms never talk to each other. They pick up job cards in the status they
handle and move them to the next status. Every action writes an event, and
the visual view is drawn from those events.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, line TEXT, status TEXT, title TEXT, data TEXT,
  key TEXT UNIQUE, worker TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL, room TEXT, job_id INTEGER, kind TEXT,
  message TEXT, cost REAL DEFAULT 0, data TEXT);
CREATE TABLE IF NOT EXISTS rooms(name TEXT PRIMARY KEY, paused INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS suppression(email TEXT PRIMARY KEY, ts REAL);
"""

# Statuses where a card has left the factory floor.
FINAL = ("posted", "sent", "rejected", "disqualified", "failed")


def day_start() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


class DB:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)
            # Cards left "in an agent's hands" by a previous run go back on the shelf.
            self.conn.execute("UPDATE jobs SET worker=NULL")
            self.conn.commit()

    def _q(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    @staticmethod
    def _job(row):
        if row is None:
            return None
        job = dict(row)
        job["data"] = json.loads(job["data"] or "{}")
        return job

    # --- jobs ---
    def create_job(self, line, status, title, data=None, key=None):
        """Returns the new job id, or None if a job with this key already exists."""
        now = time.time()
        try:
            cur = self._q(
                "INSERT INTO jobs(line,status,title,data,key,created,updated) VALUES(?,?,?,?,?,?,?)",
                (line, status, title, json.dumps(data or {}), key, now, now))
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid

    def get_job(self, job_id):
        return self._job(self._q("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def claim(self, line, status, room):
        """Give the oldest free card in this status to a room."""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE line=? AND status=? AND worker IS NULL ORDER BY id LIMIT 1",
                (line, status)).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE jobs SET worker=? WHERE id=?", (room, row["id"]))
            self.conn.commit()
            return self._job(row)

    def claim_peek(self, line, status):
        """Is any free card waiting in this status?"""
        return self._q("SELECT 1 FROM jobs WHERE line=? AND status=? AND worker IS NULL LIMIT 1",
                       (line, status)).fetchone() is not None

    def queue_size(self, line, status):
        return self._q("SELECT COUNT(*) FROM jobs WHERE line=? AND status=?", (line, status)).fetchone()[0]

    def finish(self, job_id, status, patch=None):
        with self.lock:
            job = self.get_job(job_id)
            data = job["data"]
            data.update(patch or {})
            self._q("UPDATE jobs SET status=?, data=?, worker=NULL, updated=? WHERE id=?",
                    (status, json.dumps(data), time.time(), job_id))

    def jobs(self, active_only=True, limit=300):
        sql = "SELECT * FROM jobs"
        if active_only:
            sql += " WHERE status NOT IN (%s)" % ",".join("?" * len(FINAL))
        sql += " ORDER BY id DESC LIMIT ?"
        args = (*FINAL, limit) if active_only else (limit,)
        return [self._job(r) for r in self._q(sql, args).fetchall()]

    def count_status(self, status):
        return self._q("SELECT COUNT(*) FROM jobs WHERE status=?", (status,)).fetchone()[0]

    # --- events ---
    def add_event(self, room, job_id, kind, message, cost=0.0, data=None):
        self._q("INSERT INTO events(ts,room,job_id,kind,message,cost,data) VALUES(?,?,?,?,?,?,?)",
                (time.time(), room, job_id, kind, message, cost, json.dumps(data or {})))

    def events_after(self, after_id=0, limit=200, job_id=None):
        if job_id is not None:
            rows = self._q("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        else:
            rows = self._q("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            e["data"] = json.loads(e["data"] or "{}")
            out.append(e)
        return out

    def last_event_id(self):
        return self._q("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]

    def cost_today(self, room=None):
        sql, args = "SELECT COALESCE(SUM(cost),0) FROM events WHERE ts>=?", [day_start()]
        if room:
            sql += " AND room=?"
            args.append(room)
        return self._q(sql, args).fetchone()[0]

    def moves_today(self, room, to_status):
        """How many cards this room moved into a status today."""
        rows = self._q("SELECT data FROM events WHERE ts>=? AND room=? AND kind='moved'",
                       (day_start(), room)).fetchall()
        return sum(1 for r in rows if json.loads(r["data"] or "{}").get("to_status") == to_status)

    # --- rooms ---
    def paused(self, room):
        row = self._q("SELECT paused FROM rooms WHERE name=?", (room,)).fetchone()
        return bool(row and row["paused"])

    def set_paused(self, room, paused):
        self._q("INSERT INTO rooms(name,paused) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET paused=excluded.paused",
                (room, int(paused)))

    # --- do-not-contact list ---
    def suppress(self, email):
        self._q("INSERT OR IGNORE INTO suppression(email,ts) VALUES(?,?)", (email.lower(), time.time()))

    def is_suppressed(self, email):
        return self._q("SELECT 1 FROM suppression WHERE email=?", (email.lower(),)).fetchone() is not None
