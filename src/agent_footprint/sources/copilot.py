"""GitHub Copilot Chat in VS Code: <VS Code user folder>/workspaceStorage/<hash>/chatSessions/<id>.jsonl
(and globalStorage/emptyWindowChatSessions/ for chats without a folder).

A session file is a log of changes to one JSON document: {kind: 0, v: whole state}, then
{kind: 1, k: path, v: value} (set), {kind: 2, k: path, v: items, i?: index} (append, optionally
after truncating at i) and {kind: 3, k: path} (delete). Older versions wrote the state as plain
.json. Replaying gives state["requests"]; a finished request has:

- result.metadata.resolvedModel: the model that answered (also when "Auto" was selected);
- result.metadata.toolCallRounds: one entry per model call;
- completionTokens (all calls, reasoning included; else result.metadata.outputTokens);
- promptTokens: the prompt of ONE call (the last), not of all of them, and no cached share.

So output tokens and the number of calls are exact, but input is estimated: the calls of a request
are taken to grow linearly from the previous request's prompt to this one's, and only that growth
is new input; the rest is counted as cache reads (Copilot's backends cache the shared prefix).
"""
import glob
import json
import os
import sys
from urllib.parse import unquote, urlparse

from .base import FileSource, Unit, project_of

NOTE = ("GitHub Copilot logs one prompt size per request, not per model call: its input tokens are "
        "estimated (output tokens and the number of calls are exact)")


def replay(lines):
    """VS Code's chat session change log -> the session state (dict), or None."""
    state = None
    for op in lines:
        if not isinstance(op, dict):
            continue
        kind, path = op.get("kind"), op.get("k") or []
        if kind == 0:
            state = op.get("v")
            continue
        if not isinstance(state, (dict, list)) or not path:
            continue
        try:
            target = state
            for key in path[:-1]:
                target = target[key]
            last = path[-1]
            if kind == 1:
                target[last] = op.get("v")
            elif kind == 2:
                items = target[last]
                if op.get("i") is not None:
                    del items[op["i"]:]
                items.extend(op.get("v") or [])
            elif kind == 3:
                del target[last]
        except (KeyError, IndexError, TypeError):
            continue  # a change to something we don't have: skip it
    return state if isinstance(state, dict) else None


def estimate_tokens(prompt, previous, calls, output):
    """Token counts for a request whose last call had `prompt` input tokens, `previous` being the
    last prompt of the request before it (None for the first)."""
    start = previous if previous and previous <= prompt else prompt
    total = round(calls * (start + prompt) / 2)
    new = min(prompt - previous if previous and previous <= prompt else prompt, total)
    return {"output": output, "input": new, "cache_creation": 0, "cache_read": total - new}


def vscode_user_dirs():
    """VS Code's (and Insiders') user folders on this system."""
    if os.name == "nt":
        bases = [os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")]
    elif sys.platform == "darwin":
        bases = [os.path.expanduser("~/Library/Application Support")]
    else:
        bases = [os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")]
    return [os.path.join(b, app, "User") for b in bases for app in ("Code", "Code - Insiders")]


class CopilotSource(FileSource):
    tool = "copilot"
    home_relative = os.path.join("AppData", "Roaming", "Code", "User")  # a Windows home, seen from WSL

    def default_root(self):
        dirs = vscode_user_dirs()
        return next((d for d in dirs if os.path.isdir(d)), dirs[0])

    def units(self):
        found = glob.glob(os.path.join(self.root, "workspaceStorage", "*", "chatSessions", "*.json*"))
        found += glob.glob(os.path.join(self.root, "globalStorage", "emptyWindowChatSessions", "*.json*"))
        for p in found:
            if p.endswith((".json", ".jsonl")):
                yield Unit(p, os.path.splitext(os.path.basename(p))[0], "main", self._project(p), whole=True)

    @staticmethod
    def _project(path):
        """The folder the chat ran in, from workspaceStorage/<hash>/workspace.json."""
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(path)), "workspace.json"), encoding="utf-8") as f:
                ws = json.load(f)
            uri = ws.get("folder") or ws.get("workspace") or ""
        except (OSError, ValueError, AttributeError):
            return ""
        return project_of(unquote(urlparse(uri).path)).replace(".code-workspace", "")

    def read_whole(self, unit):
        with open(unit.path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if unit.path.endswith(".json"):
            try:
                state = json.loads(text)
            except ValueError:
                return None
        else:
            ops = []
            for line in text.splitlines():
                try:
                    ops.append(json.loads(line))
                except ValueError:
                    continue
            state = replay(ops)
        return [state] if isinstance(state, dict) else None

    def parse_line(self, state, st, unit):
        if state.get("sessionId"):
            st["session"] = state["sessionId"]
        title = state.get("customTitle")
        if isinstance(title, str) and title:
            st["title"] = title[:90]
        out, previous = [], None
        for r in state.get("requests") or []:
            if not isinstance(r, dict):
                continue
            meta = (r.get("result") or {}).get("metadata") or {}
            prompt = r.get("promptTokens") or meta.get("promptTokens")
            output = r.get("completionTokens") or meta.get("outputTokens") or 0
            if not prompt and not output:
                continue  # not answered (yet), or cancelled before any usage
            rounds = [x for x in meta.get("toolCallRounds") or [] if isinstance(x, dict)]
            prompt = int(prompt or 0)
            stamp = next((x.get("timestamp") for x in reversed(rounds) if x.get("timestamp")), None) or r.get("timestamp")
            out.append({
                "id": r.get("requestId") or "%s:%d" % (unit.session, len(out)),
                "ts": _iso(stamp),
                "model": meta.get("resolvedModel") or next((x.get("modelId") for x in rounds if x.get("modelId")), "")
                         or str(r.get("modelId") or "").split("/")[-1],
                "provider": None,  # Copilot serves several makers' models: guessed from the model name
                "source": "main",
                "requests": max(1, len(rounds)),
                "tokens": estimate_tokens(prompt, previous, max(1, len(rounds)), int(output)),
            })
            previous = prompt or previous
        return out


def _iso(ms):
    from ..store import iso_of
    try:
        return iso_of(float(ms) / 1000)
    except (TypeError, ValueError):
        return None
