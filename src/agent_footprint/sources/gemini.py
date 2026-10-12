"""Gemini CLI: ~/.gemini/tmp/<project>/chats/session-<time>-<id>.jsonl (subagents in chats/<parent>/).

Append-only JSONL: a metadata line ({sessionId, projectHash, startTime, ...}), message lines
({id, timestamp, type, content, model, tokens}) and update lines ($set, $patch, $rewindTo). A
"gemini" message is appended again with the same id once its token counts arrive, so the last
line for an id wins. tokens.input (promptTokenCount) includes tokens.cached; thoughts are
generated tokens. Older versions wrote one whole .json file per session; those are read too.
Rewinds are ignored: the compute already happened.
"""
import glob
import os

from .base import FileSource, Unit, text_of, title_from


class GeminiSource(FileSource):
    tool = "gemini"
    home_relative = os.path.join(".gemini", "tmp")

    def default_root(self):
        return os.path.join(os.environ.get("GEMINI_CLI_HOME") or os.path.expanduser("~"), ".gemini", "tmp")

    @classmethod
    def root_from_transcript(cls, path):
        chats = os.path.dirname(path)  # <home>/.gemini/tmp/<project>/chats/session-*.jsonl
        return os.path.dirname(os.path.dirname(chats)) if os.path.basename(chats) == "chats" else None

    def units(self):
        for p in glob.glob(os.path.join(self.root, "*", "chats", "session-*.json*")):
            if p.endswith((".json", ".jsonl")):
                project = os.path.basename(os.path.dirname(os.path.dirname(p)))
                stem = os.path.splitext(os.path.basename(p))[0]
                yield Unit(p, stem, "main", project, whole=p.endswith(".json"))
        for p in glob.glob(os.path.join(self.root, "*", "chats", "*", "*.jsonl")):
            parent = os.path.dirname(p)
            project = os.path.basename(os.path.dirname(os.path.dirname(parent)))
            yield Unit(p, os.path.basename(parent), "subagent", project)

    def parse_line(self, d, st, unit):
        if "messages" in d and isinstance(d["messages"], list):  # legacy whole-file record
            self._meta(d, st, unit)
            out = []
            for m in d["messages"]:
                if isinstance(m, dict):
                    out += self._message(m, st, unit)
            return out
        if "sessionId" in d and "type" not in d:
            self._meta(d, st, unit)
            return []
        if any(k.startswith("$") for k in d):
            return []
        return self._message(d, st, unit)

    def _meta(self, d, st, unit):
        if unit.source == "main" and d.get("sessionId"):
            st["session"] = d["sessionId"]

    def _message(self, m, st, unit):
        kind = m.get("type")
        if kind == "user" and not st.get("title") and unit.source == "main":
            st["title"] = title_from(text_of(m.get("displayContent") or m.get("content")))
            return []
        t = m.get("tokens")
        if kind != "gemini" or not isinstance(t, dict) or not m.get("id"):
            return []
        cached = t.get("cached") or 0
        tokens = {
            "output": (t.get("output") or 0) + (t.get("thoughts") or 0),
            "input": max(0, (t.get("input") or 0) - cached) + (t.get("tool") or 0),
            "cache_creation": 0,
            "cache_read": cached,
        }
        if not any(tokens.values()):
            return []
        return [{
            "id": m["id"],
            "ts": m.get("timestamp"),
            "model": m.get("model") or "",  # not logged: model.py estimates and says so
            "provider": "google_genai",
            "source": unit.source,
            "tokens": tokens,
        }]
