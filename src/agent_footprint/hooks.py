"""`agent-footprint setup`: install (or remove) a session-start hook in each coding agent, so the
footprint view opens whenever a session starts.

Packages can't run code when they are installed, so this is the step to take right after
installing. It edits each agent's own user-level config and touches only its own entries
(recognised by the `agent-footprint` + ` open --hook` command), keeping a one-time backup:

- Claude Code: SessionStart hook in $CLAUDE_CONFIG_DIR/settings.json (default ~/.claude)
- Codex CLI:   SessionStart hook in $CODEX_HOME/hooks.json (default ~/.codex)
- Gemini CLI:  SessionStart hook in ~/.gemini/settings.json ($GEMINI_CLI_HOME/.gemini)
- OpenCode:    a plugin file in ~/.config/opencode/plugins/ (runs when OpenCode starts)
"""
import json
import os
import re
import shlex
import shutil
import subprocess

from . import config, launch, sources

MARKER = " open --hook"
BACKUP_SUFFIX = ".agent-footprint.bak"
PLUGIN_NAME = "agent-footprint.js"


class SetupError(Exception):
    pass


def join_command(argv):
    """argv -> a command line for the agent's shell. On Windows, double quotes (cmd.exe and Git Bash
    accept them; paths there often contain spaces)."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def split_command(cmd):
    """The inverse of join_command (Windows paths keep their backslashes)."""
    if os.name != "nt":
        return shlex.split(cmd)
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t for t in shlex.split(cmd, posix=False)]


def hook_program():
    """argv that starts agent-footprint from a hook. On Windows the bare name when it is on PATH:
    hooks get the user's PATH there, and a quoted path doesn't run in PowerShell (it is read as a
    string). Elsewhere the absolute path, since hooks may run with a reduced PATH."""
    argv = launch.self_command()
    if os.name == "nt" and len(argv) == 1:
        found = shutil.which("agent-footprint")
        if found and os.path.normcase(os.path.abspath(found)) == os.path.normcase(os.path.abspath(argv[0])):
            return ["agent-footprint"]
    return argv


def _home(env, default):
    return os.path.normpath(os.path.expanduser(os.environ.get(env) or default))


class Agent:
    key = label = binary = ""

    def config_dir(self):
        raise NotImplementedError

    def target(self):
        """The file this agent's hook lives in."""
        raise NotImplementedError

    def detected(self):
        return os.path.isdir(self.config_dir()) or bool(shutil.which(self.binary))

    def command(self):
        return join_command(hook_program() + ["open", "--hook", "--agent", self.key])

    def hook_argv(self):
        """The argv our installed hook runs, or None."""
        raise NotImplementedError

    def missing_program(self):
        """The program our installed hook runs, if it no longer exists (e.g. a deleted venv), else None."""
        argv = self.hook_argv()
        if not argv:
            return None
        prog = argv[0]
        found = os.path.exists(prog) if os.path.dirname(prog) else shutil.which(prog)
        return None if found else prog


class JsonHookAgent(Agent):
    """Agents with Claude Code-style hooks: {"hooks": {"SessionStart": [{"hooks": [{"type": "command", ...}]}]}}."""
    timeout = 15  # in the agent's unit
    extra = {}
    matcher = None  # which session starts (all when None)

    def _load(self):
        path = self.target()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            return json.loads(text) if text.strip() else {}
        except ValueError as e:
            raise SetupError("%s isn't plain JSON (%s); add this SessionStart hook by hand:\n  %s"
                             % (path, e, self.command()))

    def _save(self, data):
        path = self.target()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and not os.path.exists(path + BACKUP_SUFFIX):
            shutil.copy2(path, path + BACKUP_SUFFIX)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)  # leave the user's own text as it was
            f.write("\n")
        os.replace(tmp, path)

    @staticmethod
    def _ours(hook):
        cmd = str(hook.get("command", ""))
        return MARKER in cmd and ("agent-footprint" in cmd or "agent_footprint" in cmd)

    @staticmethod
    def _legacy(hook):
        """The earlier open_split.sh hook from this project. Returns (is legacy, script still exists)."""
        cmd = str(hook.get("command", ""))
        if "open_split.sh" not in cmd:
            return False, False
        script = next((t for t in shlex.split(cmd, posix=True) if t.endswith("open_split.sh")), "")
        return True, bool(script) and os.path.exists(os.path.expanduser(script))

    def _strip(self, data, notes):
        """Remove our entries (and dead legacy ones) from data, in place."""
        groups = (data.get("hooks") or {}).get("SessionStart")
        if not isinstance(groups, list):
            return
        kept_groups = []
        for g in groups:
            hooks = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(hooks, list):
                kept_groups.append(g)
                continue
            kept = []
            for h in hooks:
                legacy, exists = self._legacy(h) if isinstance(h, dict) else (False, False)
                if isinstance(h, dict) and self._ours(h):
                    continue
                if legacy and not exists:
                    notes.append("removed an old hook whose script no longer exists: %s" % h.get("command"))
                    continue
                if legacy:
                    notes.append("note: an older open_split.sh hook is also installed; remove it to avoid two panes")
                kept.append(h)
            if kept:
                kept_groups.append(dict(g, hooks=kept))
        if kept_groups:
            data["hooks"]["SessionStart"] = kept_groups
        else:
            data["hooks"].pop("SessionStart", None)
            if not data["hooks"]:
                data.pop("hooks")

    def _our_hooks(self):
        try:
            groups = (self._load().get("hooks") or {}).get("SessionStart") or []
        except SetupError:
            return []
        return [h for g in groups if isinstance(g, dict)
                for h in (g.get("hooks") or []) if isinstance(h, dict) and self._ours(h)]

    def installed(self):
        return bool(self._our_hooks())

    def hook_argv(self):
        hooks = self._our_hooks()
        try:
            return split_command(str(hooks[0]["command"])) if hooks else None
        except ValueError:
            return None

    def install(self):
        notes = []
        data = self._load()
        if not isinstance(data, dict):
            raise SetupError("%s doesn't hold a JSON object" % self.target())
        self._strip(data, notes)
        hook = dict({"type": "command", "command": self.command(), "timeout": self.timeout}, **self.extra)
        group = {"matcher": self.matcher, "hooks": [hook]} if self.matcher else {"hooks": [hook]}
        data.setdefault("hooks", {}).setdefault("SessionStart", []).append(group)
        self._save(data)
        return notes

    def remove(self):
        if not os.path.exists(self.target()):
            return []
        notes = []
        data = self._load()
        if isinstance(data, dict) and self.installed():
            self._strip(data, notes)
            self._save(data)
            notes.append("removed")
        return notes


class Claude(JsonHookAgent):
    key, label, binary = "claude", "Claude Code", "claude"
    timeout = 15  # seconds
    matcher = "startup|resume"  # not on /clear or compaction

    def config_dir(self):
        return _home("CLAUDE_CONFIG_DIR", "~/.claude")

    def target(self):
        return os.path.join(self.config_dir(), "settings.json")


class Codex(JsonHookAgent):
    key, label, binary = "codex", "Codex CLI", "codex"
    timeout = 15  # seconds

    def config_dir(self):
        return _home("CODEX_HOME", "~/.codex")

    def target(self):
        return os.path.join(self.config_dir(), "hooks.json")


class Gemini(JsonHookAgent):
    key, label, binary = "gemini", "Gemini CLI", "gemini"
    timeout = 15000  # milliseconds
    extra = {"name": "agent-footprint"}

    def config_dir(self):
        return os.path.join(_home("GEMINI_CLI_HOME", "~"), ".gemini")

    def target(self):
        return os.path.join(self.config_dir(), "settings.json")


PLUGIN = """// Installed by agent-footprint (`agent-footprint setup`): opens the footprint view when OpenCode
// starts. Delete this file, or run `agent-footprint setup --remove`, to stop.
export const AgentFootprint = async ({ $ }) => {
  const cmd = %s
  $`${cmd}`.quiet().nothrow().then(() => {}, () => {})
  return {}
}
"""


class OpenCode(Agent):
    key, label, binary = "opencode", "OpenCode", "opencode"

    def config_dir(self):
        return os.path.join(_home("XDG_CONFIG_HOME", "~/.config"), "opencode")

    def detected(self):
        data = sources.SOURCES["opencode"](None).root
        return os.path.isdir(self.config_dir()) or os.path.isdir(data) or bool(shutil.which(self.binary))

    def target(self):
        return os.path.join(self.config_dir(), "plugins", PLUGIN_NAME)

    def installed(self):
        return os.path.exists(self.target())

    def hook_argv(self):
        try:
            with open(self.target(), encoding="utf-8") as f:
                m = re.search(r"const cmd = (\[.*\])", f.read())
            return json.loads(m.group(1)) if m else None
        except (OSError, ValueError):
            return None

    def install(self):
        os.makedirs(os.path.dirname(self.target()), exist_ok=True)
        argv = launch.self_command() + ["open", "--hook", "--agent", self.key]
        with open(self.target(), "w", encoding="utf-8") as f:
            f.write(PLUGIN % json.dumps(argv))
        return []

    def remove(self):
        if self.installed():
            os.remove(self.target())
            return ["removed"]
        return []


AGENTS = {a.key: a for a in (Claude(), Codex(), Gemini(), OpenCode())}


# ---------- the setup command ----------

def _ask(prompt, default=True):
    try:
        answer = input("%s [%s] " % (prompt, "Y/n" if default else "y/N")).strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes")


def _short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def status_lines():
    lines = []
    for a in AGENTS.values():
        missing = a.missing_program() if a.installed() else None
        if missing:
            state = "HOOK BROKEN"
        else:
            state = "hook installed" if a.installed() else ("not installed" if a.detected() else "agent not found")
        lines.append("  %-12s %-16s %s" % (a.label, state, _short(a.target())))
        if missing:
            lines.append("  %-12s %-16s it runs %s, which no longer exists; run `agent-footprint setup` again"
                         % ("", "", missing))
    return lines


TEMPORARY_MESSAGE = """This agent-footprint runs from a temporary environment (uvx or pipx run), which can be
deleted at any time; hooks pointing into it would then fail at every session start.
Install it first, then run setup again:
    uv tool install agent-footprint      (or: pipx install agent-footprint)
    agent-footprint setup"""

TITLES_PROMPT = ("Name sessions after their first prompt? This stores the first 90 characters of each\n"
                 "  session's first prompt in the usage history (otherwise sessions show their project and id).")


def _set_titles(on):
    config.set_user_value("store_titles", bool(on))
    print("  Session titles %s (\"store_titles\" in %s)." % ("on" if on else "off: any stored ones are erased",
                                                               _short(config.user_path())))


def setup(agents=None, yes=False, remove=False, status=False, interactive=True, titles=None):
    """Returns a process exit code. titles: True/False to set `store_titles`, None to ask (interactive only)."""
    if status:
        print("Session-start hooks:")
        print("\n".join(status_lines()))
        print("Terminal here: %s" % launch.describe_terminal(launch.detect_terminal()))
        print("Session titles: %s" % ("on" if config.load_config()["store_titles"] else "off"))
        return 0
    chosen = [AGENTS[k] for k in agents] if agents else list(AGENTS.values())
    if remove:
        for a in chosen:
            try:
                notes = a.remove()
            except SetupError as e:
                print("%s: %s" % (a.label, e))
                continue
            print("%-12s %s" % (a.label, "; ".join(notes) or "nothing to remove"))
        return 0

    if titles is not None:
        _set_titles(titles)
    elif interactive and not yes:
        current = config.load_config()["store_titles"]
        wanted = _ask(TITLES_PROMPT, current)
        if wanted != current:
            _set_titles(wanted)
        print()
    if launch.temporary_install():
        print(TEMPORARY_MESSAGE)
        return 1

    print("agent-footprint can open its live view whenever a coding-agent session starts.")
    print("Terminal here: %s." % launch.describe_terminal(launch.detect_terminal()))
    print("Elsewhere it uses the terminal the agent runs in, or the browser dashboard if it can't drive it.\n")
    failed = False
    for a in chosen:
        if not agents and not a.detected():
            print("  %-12s not found, skipped" % a.label)
            continue
        missing = a.missing_program() if a.installed() else None
        if missing:
            prompt = "  %-12s the hook runs %s, which no longer exists. Repair it?" % (a.label, missing)
            default = True
        elif a.installed() and not agents:
            prompt = "  %-12s already set up (%s). Refresh it?" % (a.label, _short(a.target()))
            default = False
        else:
            prompt = "  %-12s add the hook to %s?" % (a.label, _short(a.target()))
            default = True
        if not yes and interactive and not _ask(prompt, default):
            continue
        try:
            notes = a.install()
        except (SetupError, OSError) as e:
            print("  %-12s %s" % (a.label, e))
            failed = True
            continue
        print("  %-12s hook installed" % a.label + "".join("\n               " + n for n in notes))
        if a.key == "codex":
            print("               Codex may ask you to trust the new hook the first time it runs.")
    print("\nThe hooks run: %s --agent <agent>" % join_command(hook_program() + ["open", "--hook"]))
    print("Change what opens under \"launch\" in %s; undo with `agent-footprint setup --remove`."
          % _short(config.user_path()))
    return 1 if failed else 0
