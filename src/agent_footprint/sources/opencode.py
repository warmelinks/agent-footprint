"""OpenCode: SQLite database ~/.local/share/opencode/opencode.db (opencode-<channel>.db for
non-release channels); older versions kept one JSON file per message under storage/.

Assistant messages carry providerID, modelID, time.created (ms) and
tokens {input (non-cached), output (excluding reasoning), reasoning, cache {read, write}}.
Child sessions (subagents started by the task tool) are counted under their parent session.
The database is opened read-only and only queried when it (or its WAL) changed.
"""
import glob
import json
import os
import sqlite3

from .base import FileSource, Unit, project_of
from ..store import iso_of


def _tokens(t):
    cache = t.get("cache") or {}
    return {
        "output": (t.get("output") or 0) + (t.get("reasoning") or 0),
        "input": t.get("input") or 0,
        "cache_creation": cache.get("write") or 0,
        "cache_read": cache.get("read") or 0,
    }


def _record(mid, data, session, source):
    if data.get("role") != "assistant" or not isinstance(data.get("tokens"), dict):
        return None
    tokens = _tokens(data["tokens"])
    created = (data.get("time") or {}).get("created")
    if not any(tokens.values()) or not created:
        return None
    return {"id": mid, "ts": iso_of(created / 1000), "model": data.get("modelID") or "",
            "provider": data.get("providerID"), "source": source, "session": session, "tokens": tokens}


class OpenCodeSource(FileSource):
    tool = "opencode"
    home_relative = os.path.join(".local", "share", "opencode")

    def default_root(self):
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        return os.path.join(base, "opencode")

    def databases(self):
        dbs = sorted(glob.glob(os.path.join(self.root, "opencode*.db")))
        custom = os.environ.get("OPENCODE_DB")  # OpenCode's own override: an absolute path or a name in its data dir
        if custom and custom != ":memory:":
            path = custom if os.path.isabs(custom) else os.path.join(self.root, custom)
            if os.path.isfile(path) and path not in dbs:
                dbs.append(path)
        return dbs

    def sync(self, store, quick=False):
        if not self.available():
            return 0
        changed = sum(self._sync_db(store, db) for db in self.databases())
        changed += super().sync(store, quick)  # legacy JSON storage, via units()/parse_line()
        if changed:
            store.commit()
        return changed

    def _sync_db(self, store, path):
        sig = [os.path.getmtime(p) if os.path.exists(p) else 0 for p in (path, path + "-wal")]
        prev = store.file_state(path)
        state = prev[2] if prev else {}
        if state.get("sig") == sig:
            return 0
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        except sqlite3.Error:
            return 0
        try:
            sessions = {r[0]: r for r in conn.execute("SELECT id, parent_id, title, directory FROM session")}
            rows = conn.execute("SELECT id, session_id, time_updated, data FROM message WHERE time_updated > ? "
                                "ORDER BY time_updated", (state.get("last", 0),)).fetchall()
        except sqlite3.Error:
            conn.close()
            return 0
        conn.close()

        def root(sid):
            seen = set()
            while sid in sessions and sessions[sid][1] and sid not in seen:
                seen.add(sid)
                sid = sessions[sid][1]
            return sid

        by_session, last = {}, state.get("last", 0)
        for mid, sid, updated, data in rows:
            last = max(last, updated or 0)
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                continue
            top = root(sid)
            rec = _record(mid, data, self.sid(top), "main" if top == sid else "subagent")
            if rec:
                by_session.setdefault(top, []).append(rec)
        n = 0
        for top, recs in by_session.items():
            n += store.upsert(self.tool, self.sid(top), recs)
            s = sessions.get(top)
            store.upsert_session(self.sid(top), self.tool, top,
                                 project_of(s[3]) if s else None,
                                 s[2] if s and self.store_titles else None)
        store.save_file(path, 0, 0, {"sig": sig, "last": last})
        return n

    # legacy storage/message/<session>/<message>.json files, one message each
    def units(self):
        for pattern in ("storage/message/*/*.json", "storage/session/message/*/*.json"):
            for p in glob.glob(os.path.join(self.root, pattern)):
                yield Unit(p, os.path.basename(os.path.dirname(p)), whole=True)

    def parse_line(self, d, st, unit):
        rec = _record(d.get("id") or os.path.basename(unit.path)[:-5], d, d.get("sessionID") or unit.session, "main")
        return [rec] if rec else []
