"""OpenAI Codex CLI: ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl (and archived_sessions/).

Each line is {timestamp, type, payload}. `turn_context` carries the model; `event_msg` with
payload.type == "token_count" carries info.last_token_usage (this request) and
info.total_token_usage (running total, used to skip repeated events). Cached input is part of
input_tokens; output_tokens already includes reasoning tokens. Cold rollouts are compressed to
.jsonl.zst; those are read with compression.zstd (Python 3.14+) or the `zstandard` package.
"""
import glob
import io
import json
import os
import re

from .base import FileSource, Unit, project_of, title_from

_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _zstd_reader():
    try:
        from compression import zstd  # Python 3.14+
        return lambda f: zstd.ZstdFile(f)
    except ImportError:
        pass
    try:
        import zstandard
        return lambda f: zstandard.ZstdDecompressor().stream_reader(f)
    except ImportError:
        return None


class CodexSource(FileSource):
    tool = "codex"
    home_relative = ".codex"

    def default_root(self):
        return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")

    @classmethod
    def root_from_transcript(cls, path):
        parts = os.path.abspath(path).split(os.sep)  # <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl
        for sub in ("sessions", "archived_sessions"):
            if sub in parts[:-1] and os.path.basename(path).startswith("rollout-"):
                i = len(parts) - 1 - parts[::-1].index(sub)
                return os.sep.join(parts[:i]) or os.sep
        return None

    def units(self):
        for sub in ("sessions", "archived_sessions"):
            for p in glob.glob(os.path.join(self.root, sub, "**", "rollout-*.jsonl*"), recursive=True):
                if not (p.endswith(".jsonl") or p.endswith(".jsonl.zst")):
                    continue
                m = _UUID.search(os.path.basename(p))
                yield Unit(p, m.group(1) if m else os.path.basename(p).split(".")[0], whole=p.endswith(".zst"))

    def read_whole(self, unit):
        opener = _zstd_reader()
        if opener is None:
            self.notices.add("Compressed Codex sessions (.jsonl.zst) skipped: install agent-footprint[zstd] to read them.")
            return None
        out = []
        with open(unit.path, "rb") as raw, opener(raw) as z:
            for line in io.TextIOWrapper(z, encoding="utf-8", errors="replace"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    out.append(d)
        return out

    def parse_line(self, d, st, unit):
        kind, p = d.get("type"), d.get("payload") or {}
        if kind == "session_meta":
            st["session"] = p.get("id") or st.get("session")
            if p.get("cwd"):
                st["project"] = project_of(p["cwd"])
            src = p.get("source")
            if (isinstance(src, dict) and any("sub" in str(k).lower() for k in src)) or str(src).startswith("subagent"):
                st["subagent"] = True
            return []
        if kind == "turn_context":
            if p.get("model"):
                st["model"] = p["model"]
            return []
        if kind != "event_msg":
            return []
        if p.get("type") == "user_message" and not st.get("title"):
            st["title"] = title_from(p.get("message"))
            return []
        if p.get("type") != "token_count" or not p.get("info"):
            return []
        info = p["info"]
        total = (info.get("total_token_usage") or {}).get("total_tokens")
        if total is not None and total == st.get("last_total"):
            return []  # the same usage reported again (e.g. a rate-limit refresh)
        st["last_total"] = total
        u = info.get("last_token_usage") or {}
        cached = u.get("cached_input_tokens") or 0
        written = u.get("cache_write_input_tokens") or 0
        tokens = {
            "output": u.get("output_tokens") or 0,
            "input": max(0, (u.get("input_tokens") or 0) - cached - written),
            "cache_creation": written,
            "cache_read": cached,
        }
        if not any(tokens.values()):
            return []
        st["n"] = st.get("n", 0) + 1
        return [{
            "id": "%s:%d" % (st.get("session") or unit.session, st["n"]),
            "ts": d.get("timestamp"),
            "model": st.get("model") or "",  # none logged yet: model.py estimates and says so
            "provider": "openai",
            "source": "subagent" if st.get("subagent") else "main",
            "tokens": tokens,
        }]
