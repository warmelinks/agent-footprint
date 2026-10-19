"""Shared machinery for agents that log to JSONL files.

A source lists its log files as Units (path + which session they belong to) and turns one
parsed JSON line into zero or more usage records. FileSource.sync() does the rest: it reads
only the bytes added since the last sync (the offset and the parser's state are kept in the
store, so this survives restarts), re-reads a file from the start if it shrank, and parses
whole-file formats (legacy .json, compressed archives) in one go when they change.

A usage record: {id, ts (ISO UTC), model, provider (agent's name for it, or None),
source ("main" | "subagent"), tokens {output, input, cache_creation, cache_read}}.
Optional "session" overrides the unit's session.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field

RECENT_S = 3600        # quick syncs only stat files written this recently...
RESCAN_S = 30.0        # ...and list every log file (and re-check which are recent) this often
DIRS_S = 2.0           # how often a quick sync checks the log folders for new files
RACY_S = 2.0           # a folder time this close to the scan may hide a later change (coarse clock ticks)


@dataclass
class Unit:
    path: str
    session: str           # native session id
    source: str = "main"   # "subagent" for a subagent's own log
    project: str = ""
    whole: bool = False    # parse the whole file on change instead of tailing it
    extra: dict = field(default_factory=dict)


class FileSource:
    tool = ""
    home_relative = ""     # default log folder relative to a home directory (used for WSL's Windows homes)
    store_titles = False   # keep session titles (the start of the first prompt); set from the config by Syncer

    def __init__(self, root=None):
        self.root = os.path.normpath(os.path.expanduser(root) if root else self.default_root())
        self.known = {}           # path -> Unit
        self.recent = []          # Units written within RECENT_S, as of the last full scan
        self.dirs = {}            # folder -> mtime as of the last full scan (a new file changes it)
        self.last_discover = self.last_dirs = 0.0
        self.notices = set()      # one-off problems worth showing (e.g. unreadable archives)

    # --- to implement ---
    def default_root(self):
        raise NotImplementedError

    def units(self):
        """Every log file under root, as Units."""
        raise NotImplementedError

    def parse_line(self, d, st, unit):
        """One JSON line -> list of usage records. `st` is this file's mutable parser state;
        set st["title"] / st["project"] / st["session"] to describe the session (the title is
        only stored when the user turned titles on)."""
        raise NotImplementedError

    @classmethod
    def root_from_transcript(cls, path):
        """The log folder (this source's root) holding a transcript the agent reported, or None."""
        return None

    def read_whole(self, unit):
        """For whole-file units: (list of JSON objects to feed to parse_line) or None if unreadable."""
        with open(unit.path, errors="replace", encoding="utf-8") as f:
            return [json.load(f)]

    # --- shared ---
    def available(self):
        return os.path.isdir(self.root)

    def sid(self, native):
        return "%s:%s" % (self.tool, native)

    def sync(self, store, quick=False):
        """Ingest new log data into the store. Returns the number of records that changed."""
        if not self.available():
            return 0
        # A full sync lists and checks every log file. Quick syncs (several a second in the live
        # views) only check the files written recently, so thousands of old transcripts, or logs
        # on a slow drive such as WSL's /mnt/c, cost nothing between full scans.
        now = time.time()
        if not quick or not self.known or now - self.last_discover >= RESCAN_S or self._new_files(now):
            self.last_discover = now
            self.known = {u.path: u for u in self.units()}
            self.dirs = _folders(self.root, self.known, now)
            paths = list(self.known.values())
            self.recent = [u for u in paths if _mtime(u.path) >= now - RECENT_S]
        else:
            paths = self.recent
        changed = 0
        for unit in paths:
            try:
                changed += self._sync_unit(store, unit)
            except OSError:
                continue
            finally:
                # Per file, and also when a file changed without new records: an open write
                # transaction holds SQLite's lock, and a process syncing alongside times out on it.
                store.commit()
        return changed

    def _new_files(self, now):
        """True if a log folder changed since the last full scan (checked every DIRS_S)."""
        if now - self.last_dirs < DIRS_S:
            return False
        self.last_dirs = now
        return any(_mtime(d) != m for d, m in self.dirs.items())

    def _sync_unit(self, store, unit):
        st_ = os.stat(unit.path)
        prev = store.file_state(unit.path)
        if prev and prev[0] == st_.st_size and prev[1] == st_.st_mtime:
            return 0
        if unit.whole:
            objs = self.read_whole(unit)
            st, recs = {}, []
            for d in objs or []:
                recs += self.parse_line(d, st, unit)
            offset = st_.st_size
        else:
            offset = prev[2].get("offset", 0) if prev else 0
            st = prev[2].get("parser", {}) if prev else {}
            if st_.st_size < offset:  # truncated or rewritten: start over
                offset, st = 0, {}
            recs, offset = self._read_from(unit, offset, st)
        session = st.get("session") or unit.session
        sid = self.sid(session)
        for r in recs:
            if r.get("session"):
                r["session"] = self.sid(r["session"])
            r.setdefault("source", unit.source)
        n = store.upsert(self.tool, sid, recs)
        title = st.get("title") if unit.source == "main" and isinstance(st.get("title"), str) else None
        if not self.store_titles:
            title = None
            if st.get("title"):
                st["title"] = True  # found, but not kept (the parsers look for a title until one is set)
        store.upsert_session(sid, self.tool, session, st.get("project") or unit.project, title)
        store.save_file(unit.path, st_.st_size, st_.st_mtime, {"offset": offset, "parser": st})
        return n

    def _read_from(self, unit, offset, st):
        """Parse complete lines after `offset`; returns (records, new offset). A line still being
        written is left for the next sync."""
        with open(unit.path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return [], offset
        recs = []
        for line in chunk[:end].decode("utf-8", errors="replace").split("\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                recs += self.parse_line(d, st, unit)
        return recs, offset + end + 1


def _folders(root, known, now):
    """{folder: mtime} for root and every folder between it and the known log files: a new
    file, or a new subfolder (e.g. Codex's next YYYY/MM/DD), changes one of their mtimes.

    File times advance in clock ticks (~15.6 ms on Windows, a few ms on Linux), so a folder
    changed just after this scan can keep the time read here. Folders changed within RACY_S of
    the scan are stored as None, which makes the next check scan again (as git does for "racy"
    index entries)."""
    out = {}
    for path in known:
        d = os.path.dirname(path)
        while d not in out and len(d) >= len(root):
            out[d] = _mtime(d)
            if d == root:
                break
            d = os.path.dirname(d)
    out.setdefault(root, _mtime(root))
    return {d: None if m >= now - RACY_S else m for d, m in out.items()}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def text_of(content):
    """Message content (string or list of parts) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(text_of(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    if isinstance(content, dict):
        return text_of(content.get("text") or content.get("parts") or "")
    return ""


def project_of(path):
    """Project name from a working directory, also for a Windows path read elsewhere."""
    parts = re.split(r"[\\/]", str(path or "").rstrip("\\/"))
    return parts[-1] if parts else ""


def title_from(text):
    text = (text or "").strip()
    return text[:90] if text and not text.startswith("<") else ""
