"""Agent log readers. Each source copies usage records from one agent's logs into the Store.

To add an agent: subclass FileSource (sources/base.py), implement default_root(), units()
and parse_line(), and register it in SOURCES below.

Where the logs are: an agent can have several log folders (e.g. two CLAUDE_CONFIG_DIR
profiles, or Windows-side installs seen from WSL). Each source reads every folder from
- the config's `path` (a string or a list), else the agent's default location, which follows the
  agent's own environment variables (CLAUDE_CONFIG_DIR, CODEX_HOME, GEMINI_CLI_HOME, XDG_*,
  VIBE_HOME) and, for Vibe, the save_dir in its config.toml;
- folders reported by the session-start hook (`agent-footprint open --hook` gets the
  transcript path from the agent, so non-default locations are learned automatically);
- under WSL, the same agents' default folders in each Windows user profile on C: (other drives
  aren't scanned: one may be a slow or disconnected network drive; add them with `path`).
Records are keyed by agent and request id, so a folder reached twice is never counted twice.
"""
import glob
import json
import os

from ..config import cache_dir
from .claude import ClaudeSource
from .copilot import NOTE as COPILOT_NOTE, CopilotSource
from .codex import CodexSource
from .gemini import GeminiSource
from .opencode import OpenCodeSource
from .vibe import VibeSource

SOURCES = {"claude": ClaudeSource, "codex": CodexSource, "gemini": GeminiSource, "opencode": OpenCodeSource,
           "vibe": VibeSource, "copilot": CopilotSource}
TOOL_LABEL = {"claude": "Claude Code", "codex": "Codex CLI", "gemini": "Gemini CLI", "opencode": "OpenCode",
              "vibe": "Mistral Vibe", "copilot": "GitHub Copilot"}
# Shown with the results whenever an agent's numbers are partly estimated.
TOOL_NOTES = {"copilot": COPILOT_NOTE}
WINDOWS_SKIP = {"Public", "Default", "Default User", "All Users", "WDAGUtilityAccount"}


# ---------- folders learned from hooks ----------

def _roots_file():
    return os.path.join(cache_dir(), "roots.json")


def learned_roots():
    """{tool: [folders]} reported by session-start hooks."""
    try:
        with open(_roots_file(), encoding="utf-8") as f:
            data = json.load(f)
        return {k: [p for p in v if isinstance(p, str)] for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError, AttributeError):
        return {}


def learn_root(tool, root):
    """Remember a log folder for `tool`. Returns True if it was new."""
    if tool not in SOURCES or not root or not os.path.isdir(root):
        return False
    data = learned_roots()
    known = data.setdefault(tool, [])
    if any(_same(root, k) for k in known):
        return False
    known.append(os.path.abspath(root))
    os.makedirs(cache_dir(), exist_ok=True)
    tmp = _roots_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _roots_file())
    return True


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


# ---------- WSL: agents installed on the Windows side ----------

def in_wsl():
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def windows_homes():
    if not in_wsl():
        return []
    homes = []
    for home in glob.glob("/mnt/c/Users/*"):
        if os.path.basename(home) not in WINDOWS_SKIP and os.path.isdir(home):
            homes.append(home)
    return homes


# ---------- assembling sources ----------

def roots_for(name, opts, learned=None, homes=None):
    """Folders to read for one agent, existing ones first-come, without duplicates."""
    cls = SOURCES[name]
    paths = opts.get("path")
    if isinstance(paths, str):
        paths = [paths]
    candidates = [os.path.expanduser(p) for p in paths] if paths else [cls(None).root]
    candidates += (learned if learned is not None else learned_roots()).get(name, [])
    for home in (homes if homes is not None else windows_homes()):
        candidates.append(os.path.join(home, cls.home_relative))
    out = []
    for c in candidates:
        if not any(_same(c, o) for o in out):
            out.append(c)
    first = out[:1]  # the primary folder is listed even when missing, so `info` can say where it looked
    return first + [p for p in out[1:] if os.path.isdir(p)]


def enabled(cfg):
    """Instantiated sources (one per agent folder) that are enabled in the config."""
    learned, homes = learned_roots(), windows_homes()
    out = []
    for name, cls in SOURCES.items():
        opts = cfg["sources"].get(name) or {}
        if opts.get("enabled", True):
            out += [cls(root) for root in roots_for(name, opts, learned, homes)]
    for s in out:
        s.store_titles = cfg["store_titles"]
    return out


class Syncer:
    """Keeps the store in step with every enabled agent's logs."""

    def __init__(self, store, cfg):
        self.store = store
        self.sources = enabled(cfg)
        if not cfg["store_titles"]:
            store.forget_titles()

    def sync(self, quick=False):
        return sum(s.sync(self.store, quick) for s in self.sources)

    def found(self):
        """{tool: [folders that exist]}."""
        out = {}
        for s in self.sources:
            if s.available():
                out.setdefault(s.tool, []).append(s.root)
        return out

    def notices(self):
        return sorted(set().union(*(s.notices for s in self.sources))) if self.sources else []
