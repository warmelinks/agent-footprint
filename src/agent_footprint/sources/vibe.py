"""Mistral Vibe: ~/.vibe/logs/session/<prefix>_<time>_<id>/meta.json (and messages.jsonl).

Vibe doesn't log token counts per response, only running totals per session in meta.json, which it
rewrites after every turn (checked live on Windows, 2026-10-11): stats.session_prompt_tokens (including cached),
session_completion_tokens, session_cached_tokens (newer versions) and steps (LLM calls). Each
increase becomes one record, timed at the session's latest activity (end_time), standing for the
calls made since the previous one. So timing is exact to the sync interval while a view runs; a
session first seen afterwards is one record at its last activity.

The model is config.active_model (an alias such as "mistral-medium-3.5", looked up in config.models
for its provider). Child sessions (subagents) are counted under their parent.
"""
import glob
import json
import os
import re

from .base import FileSource, Unit, project_of

TOTALS = {"prompt": "session_prompt_tokens", "completion": "session_completion_tokens",
          "cached": "session_cached_tokens", "steps": "steps"}


def _windows_path(path):
    """C:\\Users\\... as seen from WSL or Linux (/mnt/c/Users/...); other paths unchanged."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if not m or os.name == "nt":
        return path
    return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/"))


def model_of(meta):
    """(model id, provider) of the session's active model."""
    c = meta.get("config") or {}
    alias = c.get("active_model") or ""
    models = c.get("models") or []
    entry = models.get(alias) if isinstance(models, dict) else next(
        (m for m in models if isinstance(m, dict) and alias in (m.get("alias"), m.get("name"))), None)
    if not isinstance(entry, dict):
        return alias, None
    return alias or entry.get("name") or "", entry.get("provider")


class VibeSource(FileSource):
    tool = "vibe"
    home_relative = os.path.join(".vibe", "logs", "session")

    def default_root(self):
        home = os.environ.get("VIBE_HOME") or os.path.join(os.path.expanduser("~"), ".vibe")
        return self.configured_dir(home) or os.path.join(home, "logs", "session")

    @staticmethod
    def configured_dir(home):
        """[session_logging] save_dir from Vibe's config.toml, if set and present."""
        try:
            with open(os.path.join(home, "config.toml"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None
        section = re.search(r"^\[session_logging\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        m = re.search(r'^save_dir\s*=\s*("(?:[^"\\]|\\.)*")', section.group(1), re.M) if section else None
        if not m:
            return None
        try:
            path = _windows_path(os.path.expanduser(json.loads(m.group(1))))  # TOML basic strings escape like JSON
        except ValueError:
            return None
        return path if os.path.isdir(path) else None

    def units(self):
        for p in glob.glob(os.path.join(self.root, "*", "meta.json")):
            yield Unit(p, os.path.basename(os.path.dirname(p)).rsplit("_", 1)[-1], whole=True)

    def _sync_unit(self, store, unit):
        st_ = os.stat(unit.path)
        prev = store.file_state(unit.path)
        if prev and prev[0] == st_.st_size and prev[1] == st_.st_mtime:
            return 0
        try:
            with open(unit.path, encoding="utf-8") as f:
                meta = json.load(f)
        except ValueError:
            return 0  # being rewritten: next sync
        if not isinstance(meta, dict):
            return 0
        state = prev[2] if prev else {}
        stats = meta.get("stats") or {}
        totals = {k: int(stats.get(v) or 0) for k, v in TOTALS.items()}
        last = state.get("totals") or {}
        if any(totals[k] < last.get(k, 0) for k in totals):
            last = {}  # counters went back (a reset session): count from zero again
        delta = {k: totals[k] - last.get(k, 0) for k in totals}
        native = meta.get("session_id") or unit.session
        parent = meta.get("parent_session_id")
        session = self.sid(parent or native)
        recs, n = [], state.get("n", 0)
        if delta["prompt"] > 0 or delta["completion"] > 0:
            n += 1
            model, provider = model_of(meta)
            cached = min(delta["cached"], delta["prompt"])
            recs.append({
                "id": "%s:%d" % (native, n),
                "ts": meta.get("end_time") or meta.get("start_time"),
                "model": model,
                "provider": provider,
                "source": "subagent" if parent else "main",
                "requests": max(1, delta["steps"]),
                "tokens": {"output": delta["completion"], "input": delta["prompt"] - cached,
                           "cache_creation": 0, "cache_read": cached},
            })
        changed = store.upsert(self.tool, session, recs)
        title = meta.get("title") if self.store_titles and not parent else None
        project = project_of((meta.get("environment") or {}).get("working_directory"))
        store.upsert_session(session, self.tool, parent or native, project, title)
        store.save_file(unit.path, st_.st_size, st_.st_mtime, {"totals": totals, "n": n})
        return changed
