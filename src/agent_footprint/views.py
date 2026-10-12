"""A view = what one screen shows: a period (day/week/month/year), one session, or the newest
session. It loads matching records from the store, then picks up changes by sequence number.
Shared by the web server and the terminal watcher.
"""
from . import periods
from .summary import Summary


class View:
    def __init__(self, store, wanted):
        """wanted: a period id ('day:today', 'month:2026-09', ...), 'latest', or a session id.
        Raises LookupError for an unknown session, ValueError for a malformed period."""
        self.store = store
        self.wanted = wanted
        self.follow = wanted == "latest"
        self.period = periods.parse(wanted)
        self.session = None
        if not self.period and not self.follow:
            self.session = store.find_session(wanted)
            if self.session is None:
                raise LookupError("Session %s not found" % wanted)
        if self.follow:
            self.session = self._newest()
        self.load()

    def _newest(self):
        s = self.store.sessions(limit=1)
        return s[0] if s else None

    def load(self):
        self.seq = self.store.seq()
        self.summary = Summary(self.period, keep_series=self.period is None)
        self.summary.apply(self._query())

    def _query(self, after_seq=None):
        if self.period:
            start, end = self.period.epochs()
            return self.store.records(start=start, end=end, after_seq=after_seq)
        if self.session:
            return self.store.records(session=self.session["id"], after_seq=after_seq)
        return []

    def poll(self):
        """'reset' if the view now shows something else (midnight rollover, a newer session to
        follow), else the list of changed (record, previous tokens), possibly empty."""
        if self.period and self.period.current and not self.period.is_now():
            self.period = periods.refresh(self.period)
            self.load()
            return "reset"
        if self.follow:
            newest = self._newest()
            if newest and (self.session is None or newest["id"] != self.session["id"]):
                self.session = newest
                self.load()
                return "reset"
        seq = self.store.seq()
        if seq == self.seq:
            return []
        new = self._query(after_seq=self.seq)
        self.seq = seq
        return self.summary.apply(new)

    def session_meta(self):
        ids = [k[0] for k in self.summary.groups]
        return {s["id"]: s for s in self.store.sessions(ids=set(ids))}

    def describe(self):
        """Header info for the view."""
        if self.period:
            p = self.period
            return {"kind": "period", "period": p.kind, "id": p.id, "view": p.view_id, "label": p.label(),
                    "current": p.current, "is_now": p.is_now(),
                    "prev": p.shift(-1).id, "next": self._next(p)}
        s = self.session or {}
        return {"kind": "session", "id": s.get("id"), "view": self.wanted, "follow": self.follow,
                "title": s.get("title") or "", "tool": s.get("tool"), "project": s.get("project") or "",
                "first": s.get("first"), "last": s.get("last")}

    @staticmethod
    def _next(p):
        if p.is_now():
            return None
        n = p.shift(1)
        return periods.containing(n.kind, n.start, current=True).view_id if n.is_now() else n.id

    def snapshot(self, cfg):
        summary = self.summary.to_json(cfg, self.session_meta())
        from .sources import TOOL_NOTES
        notes = [TOOL_NOTES[t["tool"]] for t in summary["by_tool"] if t["tool"] in TOOL_NOTES]
        summary["warnings"] = notes + summary["warnings"]
        return {"view": self.describe(), "summary": summary}
