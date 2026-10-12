"""Claude Code: ~/.claude/projects/<project>/<session>.jsonl plus <session>/subagents/*.jsonl.

Every API response is logged with its model and token usage, one line per content block;
the lines of one response share a requestId and the last one has the final usage.
"""
import glob
import os

from .base import FileSource, Unit, text_of, title_from


class ClaudeSource(FileSource):
    tool = "claude"
    home_relative = os.path.join(".claude", "projects")

    def default_root(self):
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        return os.path.join(base, "projects")

    @classmethod
    def root_from_transcript(cls, path):
        root = os.path.dirname(os.path.dirname(path))  # <config dir>/projects/<project>/<session>.jsonl
        return root if path.endswith(".jsonl") and os.path.basename(root) == "projects" else None

    def units(self):
        for p in glob.glob(os.path.join(self.root, "*", "*.jsonl")):
            yield Unit(p, os.path.splitext(os.path.basename(p))[0], "main", os.path.basename(os.path.dirname(p)))
        for p in glob.glob(os.path.join(self.root, "*", "*", "subagents", "*.jsonl")):
            session_dir = os.path.dirname(os.path.dirname(p))
            yield Unit(p, os.path.basename(session_dir), "subagent", os.path.basename(os.path.dirname(session_dir)))

    def parse_line(self, d, st, unit):
        kind = d.get("type")
        if kind == "user" and not st.get("title") and not d.get("isMeta") and unit.source == "main":
            st["title"] = title_from(text_of((d.get("message") or {}).get("content")))
            return []
        if kind != "assistant":
            return []
        msg = d.get("message") or {}
        usage, model = msg.get("usage"), msg.get("model")
        if not usage or not model or model.startswith("<"):  # <synthetic> = no API call
            return []
        tokens = {
            "output": usage.get("output_tokens") or 0,
            "input": usage.get("input_tokens") or 0,
            "cache_creation": usage.get("cache_creation_input_tokens") or 0,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
        }
        if not any(tokens.values()):
            return []
        return [{
            "id": d.get("requestId") or msg.get("id") or d.get("uuid"),
            "ts": d.get("timestamp"),
            "model": model,
            "provider": "anthropic",
            "source": "subagent" if (unit.source == "subagent" or d.get("isSidechain")) else "main",
            "tokens": tokens,
        }]
