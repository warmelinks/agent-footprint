"""Durable usage history in SQLite.

Agents purge old logs (Claude Code after `cleanupPeriodDays`, default 30; Gemini CLI after
`sessionRetention.maxAge`, default 30d), so every sync copies the usage records it finds into
~/.local/share/agent-footprint/history.sqlite ($XDG_DATA_HOME) and keeps them after the source is
gone. It holds token counts, timestamps, model names and each session's id and project folder
name. No prompt or response content, except a session title (the first 90 characters of its
first prompt) when the user turned `store_titles` on.

Sources write through upsert()/upsert_session()/save_file(); views read with records(),
sessions() and changed_since(). Every write bumps a sequence number so live views can ask
"what changed since I last looked".
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from .config import TOKEN_KINDS, cache_dir, data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    tool TEXT NOT NULL, id TEXT NOT NULL, session TEXT NOT NULL, ts TEXT, epoch REAL,
    model TEXT, provider TEXT, source TEXT,
    output INTEGER, input INTEGER, cache_creation INTEGER, cache_read INTEGER,
    seq INTEGER NOT NULL,
    requests INTEGER NOT NULL DEFAULT 1,  -- API calls the record stands for (agents logging totals only)
    PRIMARY KEY (tool, id)
);
CREATE INDEX IF NOT EXISTS records_epoch ON records (epoch);
CREATE INDEX IF NOT EXISTS records_session ON records (session);
CREATE INDEX IF NOT EXISTS records_seq ON records (seq);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, tool TEXT, native_id TEXT, project TEXT, title TEXT,
    first_epoch REAL, last_epoch REAL
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, state TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
RECORD_COLS = ("tool, id, session, ts, epoch, model, provider, source, output, input, cache_creation, cache_read, "
               "seq, requests")


def default_path():
    """The history in the data folder. Versions up to 0.1.0 kept it in the cache folder, which
    cleanup tools may empty; it is moved from there the first time."""
    path = os.path.join(data_dir(), "history.sqlite")
    old = os.path.join(cache_dir(), "history.sqlite")
    if os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(path)):
        return path  # cache and data folders configured to be the same
    if os.path.exists(old) and not os.path.exists(path):
        try:
            _move(old, path)
        except (OSError, sqlite3.Error):
            return old  # keep using it; the move is tried again next time
    elif os.path.exists(old):
        _remove_db(old)  # copied before, but still open then (Windows doesn't delete open files)
    return path


def _move(old, new):
    """Copy through SQLite's backup API (consistent even mid-write, and across file systems),
    then remove the old database with its WAL files."""
    os.makedirs(os.path.dirname(new), exist_ok=True)
    tmp = new + ".moving"
    src, dst = sqlite3.connect(old, timeout=30), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    os.replace(tmp, new)
    _remove_db(old)


def _remove_db(path):
    """Remove a database and its WAL files. A process of an older version may still have it open
    and write to it: those records are read again from the agents' logs (the copied file
    offsets predate them), so nothing is lost."""
    for suffix in ("-wal", "-shm", ""):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def epoch_of(ts):
    """ISO timestamp (UTC 'Z' or with offset) -> POSIX seconds, or None."""
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def iso_of(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Store:
    """One connection per thread; writes are serialised by SQLite (WAL mode)."""

    def __init__(self, path=None):
        self.path = path or default_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._local = threading.local()
        self._shared = None
        if self.path == ":memory:":  # tests: one shared connection
            self._shared = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.executescript(SCHEMA)
        if "requests" not in [r[1] for r in self.db.execute("PRAGMA table_info(records)")]:  # history from 0.1.0
            self.db.execute("ALTER TABLE records ADD COLUMN requests INTEGER NOT NULL DEFAULT 1")
        self.db.commit()

    @property
    def db(self):
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # ---------- writing (sources) ----------

    def _next_seq(self):
        # Write first, read after: the write takes SQLite's write lock, so a process syncing at the
        # same time can't hand out the same number (live views would then miss its records).
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('seq', '0')")
        self.db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='seq'")
        return int(self.db.execute("SELECT value FROM meta WHERE key='seq'").fetchone()[0])

    def file_state(self, path):
        """(size, mtime, state dict) recorded for a source file, or None."""
        row = self.db.execute("SELECT size, mtime, state FROM files WHERE path=?", (path,)).fetchone()
        return (row[0], row[1], json.loads(row[2] or "{}")) if row else None

    def save_file(self, path, size, mtime, state):
        self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)", (path, size, mtime, json.dumps(state)))

    def upsert(self, tool, session, records):
        """Insert or update usage records; unchanged ones are left alone. Returns how many changed."""
        if not records:
            return 0
        changed = 0
        seq = None
        for r in records:
            t = r["tokens"]
            row = (r["ts"], epoch_of(r["ts"]), r["model"], r.get("provider"), r.get("source", "main"),
                   *(int(t.get(k) or 0) for k in TOKEN_KINDS), int(r.get("requests") or 1))
            old = self.db.execute(
                "SELECT ts, epoch, model, provider, source, output, input, cache_creation, cache_read, requests "
                "FROM records WHERE tool=? AND id=?", (tool, r["id"])).fetchone()
            if old == row:
                continue
            seq = seq or self._next_seq()
            self.db.execute("INSERT OR REPLACE INTO records (%s) VALUES (%s)" % (RECORD_COLS, ", ".join("?" * 14)),
                            (tool, r["id"], r.get("session") or session) + row[:-1] + (seq, row[-1]))
            changed += 1
            if row[1] is not None:
                sid = r.get("session") or session
                self.db.execute(
                    "INSERT INTO sessions (id, tool, first_epoch, last_epoch) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET first_epoch=min(coalesce(first_epoch, excluded.first_epoch), excluded.first_epoch), "
                    "last_epoch=max(coalesce(last_epoch, excluded.last_epoch), excluded.last_epoch)",
                    (sid, tool, row[1], row[1]))
        return changed

    def upsert_session(self, sid, tool, native_id=None, project=None, title=None):
        """Session metadata; empty values never overwrite known ones."""
        self.db.execute(
            "INSERT INTO sessions (id, tool, native_id, project, title) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET native_id=coalesce(excluded.native_id, native_id), "
            "project=coalesce(excluded.project, project), "
            "title=CASE WHEN coalesce(title, '')='' THEN excluded.title ELSE title END",
            (sid, tool, native_id, project or None, title or None))

    def forget_titles(self):
        """Erase stored session titles, also from the parsers' saved state (titles turned off)."""
        self.db.execute("UPDATE sessions SET title=NULL WHERE title IS NOT NULL")
        for path, state in self.db.execute("SELECT path, state FROM files WHERE state LIKE '%\"title\": \"_%'").fetchall():
            d = json.loads(state)
            parser = d.get("parser") or {}
            if isinstance(parser.get("title"), str) and parser["title"]:
                parser["title"] = True
                self.db.execute("UPDATE files SET state=? WHERE path=?", (json.dumps(d), path))
        self.db.commit()

    def commit(self):
        self.db.commit()

    def close(self):
        """Close this thread's connection (Windows keeps open database files locked)."""
        if self._shared is not None:
            self._shared.close()
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------- reading (views) ----------

    def seq(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='seq'").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _row(row):
        d = dict(zip(("tool", "id", "session", "ts", "epoch", "model", "provider", "source"), row[:8]))
        d["tokens"] = dict(zip(TOKEN_KINDS, row[8:12]))
        d["seq"], d["requests"] = row[12], row[13]
        return d

    def records(self, start=None, end=None, session=None, after_seq=None):
        """Usage records filtered by epoch range [start, end), session id and/or seq > after_seq."""
        where, args = [], []
        if start is not None:
            where.append("epoch >= ?"); args.append(start)
        if end is not None:
            where.append("epoch < ?"); args.append(end)
        if session is not None:
            where.append("session = ?"); args.append(session)
        if after_seq is not None:
            where.append("seq > ?"); args.append(after_seq)
        sql = "SELECT %s FROM records%s ORDER BY epoch" % (RECORD_COLS, " WHERE " + " AND ".join(where) if where else "")
        return [self._row(r) for r in self.db.execute(sql, args)]

    def sessions(self, ids=None, limit=None, since=None):
        """Session metadata, newest activity first."""
        sql = "SELECT id, tool, native_id, project, title, first_epoch, last_epoch FROM sessions WHERE last_epoch IS NOT NULL"
        args = []
        if since is not None:
            sql += " AND last_epoch >= ?"; args.append(since)
        if ids is not None:
            ids = list(ids)
            if not ids:
                return []
            sql += " AND id IN (%s)" % ",".join("?" * len(ids)); args += ids
        sql += " ORDER BY last_epoch DESC"
        if limit:
            sql += " LIMIT %d" % int(limit)
        keys = ("id", "tool", "native_id", "project", "title", "first", "last")
        return [dict(zip(keys, r)) for r in self.db.execute(sql, args)]

    def find_session(self, wanted):
        """A session by full id ('codex:abc…'), native id, or unique id prefix."""
        rows = self.db.execute(
            "SELECT id FROM sessions WHERE id=? OR native_id=? OR id LIKE ? OR native_id LIKE ? ORDER BY last_epoch DESC",
            (wanted, wanted, wanted + "%", wanted + "%")).fetchall()
        return self.sessions(ids=[rows[0][0]])[0] if rows else None

    def local_days(self):
        """Local calendar dates (ISO strings) that have usage, newest first."""
        rows = self.db.execute("SELECT DISTINCT date(epoch, 'unixepoch', 'localtime') AS d FROM records "
                               "WHERE epoch IS NOT NULL ORDER BY d DESC").fetchall()
        return [r[0] for r in rows]

    def stats(self):
        n = self.db.execute("SELECT count(*), min(epoch) FROM records").fetchone()
        s = self.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
        size = sum(os.path.getsize(p) for p in (self.path, self.path + "-wal") if p[0] != ":" and os.path.exists(p))
        return {"records": n[0], "since": n[1], "sessions": s, "bytes": size}
