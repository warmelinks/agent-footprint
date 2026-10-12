"""`agent-footprint open`: show the footprint next to the agent you just started.

Run by the session-start hooks that `agent-footprint setup` installs (with --hook), or by hand.
It opens `agent-footprint watch` in a split pane of the terminal the agent runs in, or in a
new tab or window where the terminal can't split. Where no terminal can be driven, as in an
IDE's built-in terminal, it falls back to the browser dashboard. It does nothing if a watch
view is already open, so starting more sessions doesn't pile up panes.

With --hook it also reads the agent's hook input from stdin. Claude Code, Codex CLI and Gemini CLI
all send `transcript_path`, which tells us where that agent's logs live even when they are
not in the default location. It prints nothing (Claude Code adds a SessionStart hook's
output to the conversation) and always exits 0, so it can never block an agent from starting.
"""
import json
import os
import select
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from . import config, sources

STARTING_S = 15        # a pane opened this recently counts as open (the watch may still be starting)
IDLE_EXIT_S = 900      # a dashboard started by a hook stops after this long without a browser connected
# Throwaway environments (uvx, pipx run): hooks pointing into them break when they are cleaned up.
# uv keeps them in its cache: ~/.cache/uv/archive-v0/... (Windows: %LOCALAPPDATA%\uv\cache\archive-v0\...).
TEMPORARY_ENVS = ("/archive-v", "/builds-v", "/pipx/.cache/", "/pipx/cache/")
# Start a background process that outlives the hook (and the agent's console) that started it.
# On Windows not DETACHED_PROCESS: the agent-footprint.exe launcher then starts python.exe, which,
# having no console to share, gets a new (empty) console window. CREATE_NO_WINDOW gives the whole
# chain a console without a window.
DETACHED = (dict(creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            if os.name == "nt" else dict(start_new_session=True))


def self_command():
    """argv that runs this agent-footprint, independent of PATH (hooks may run with a reduced one)."""
    exe = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if os.path.basename(exe) in ("agent-footprint", "agent-footprint.exe") and os.path.exists(exe):
        return [os.path.abspath(exe)]
    found = shutil.which("agent-footprint")
    return [found] if found else [sys.executable, "-m", "agent_footprint"]


def temporary_install():
    """True when running from an environment that may be deleted at any time (uvx, pipx run)."""
    prefix = sys.prefix.replace("\\", "/")
    return any(m in prefix for m in TEMPORARY_ENVS)


# ---------- "is a watch view already open?" ----------

def _pid_file():
    return os.path.join(config.cache_dir(), "watch.pid")


def _starting_file():
    return os.path.join(config.cache_dir(), "watch.starting")


def _alive_windows(pid):
    """os.kill(pid, 0) would send Ctrl+C on Windows: ask for the process's exit code instead."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return False
        name = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(name))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return True
        exe = os.path.basename(name.value).lower()  # guard against a recycled pid
        return exe.startswith("python") or exe.startswith("agent-footprint")
    finally:
        kernel32.CloseHandle(handle)


def _alive(pid):
    if os.name == "nt":
        try:
            return _alive_windows(pid)
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    cmdline = _cmdline(pid)  # guard against a recycled pid
    if cmdline is None:
        return True  # can't tell: trust kill(0)
    return b"agent-footprint" in cmdline or b"agent_footprint" in cmdline


def _cmdline(pid):
    """A process's command line (bytes), from /proc (Linux) or ps (macOS, BSD), or None."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read()
    except OSError:
        pass
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def watch_running():
    try:
        with open(_pid_file(), encoding="utf-8") as f:
            if _alive(int(f.read().strip())):
                return True
    except (OSError, ValueError):
        pass
    try:
        return time.time() - os.path.getmtime(_starting_file()) < STARTING_S
    except OSError:
        return False


def mark_watch(pid=None):
    """Called by `watch` on start (pid) and exit (None)."""
    os.makedirs(config.cache_dir(), exist_ok=True)
    if pid is None:
        try:
            with open(_pid_file(), encoding="utf-8") as f:
                if int(f.read().strip()) != os.getpid():
                    return
            os.remove(_pid_file())
        except (OSError, ValueError):
            pass
        return
    with open(_pid_file(), "w", encoding="utf-8") as f:
        f.write(str(pid))


# ---------- terminals ----------

def _osascript(script):
    return ["osascript", "-e", script]


def _applescript_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def _iterm_split(cmd):
    """AppleScript argv splitting the iTerm2 session the agent runs in (ITERM_SESSION_ID is
    "w0t1p0:<unique id>"), or the frontmost one if it can't be found."""
    split = "split vertically with default profile command %s" % _applescript_str(shlex.join(cmd))
    wanted = (os.environ.get("ITERM_SESSION_ID") or "").rpartition(":")[2]
    lines = ['tell application "iTerm2"']
    if wanted:
        lines += ["repeat with w in windows", "repeat with t in tabs of w", "repeat with s in sessions of t",
                  "if unique id of s is %s then" % _applescript_str(wanted), "tell s to " + split, "return",
                  "end if", "end repeat", "end repeat", "end repeat"]
    lines += ["tell current session of current window to " + split, "end tell"]
    return ["osascript"] + [arg for line in lines for arg in ("-e", line)]


# macOS apps keep their command-line tool inside the app bundle, often not on PATH.
APP_BUNDLES = {"kitty": "kitty.app/Contents/MacOS/kitty", "wezterm": "WezTerm.app/Contents/MacOS/wezterm"}


def _program(name):
    """The terminal's command-line tool: on PATH, else (macOS) inside its app bundle."""
    found = shutil.which(name)
    if found or name not in APP_BUNDLES or sys.platform != "darwin":
        return found or name
    for apps in ("/Applications", os.path.expanduser("~/Applications")):
        path = os.path.join(apps, APP_BUNDLES[name])
        if os.path.exists(path):
            return path
    return name


def _wsl_wrap(cmd):
    """Windows Terminal starts Windows programs: re-enter this WSL distro to run the command."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    return (["wsl.exe", "-d", distro, "--"] if distro else ["wsl.exe", "--"]) + cmd


# name -> (is this the terminal we're in?, argv to open `cmd` there, splits?)
TERMINALS = {
    # Multiplexers first: they run inside some other terminal app.
    "tmux": (lambda e: e.get("TMUX"),
             lambda c, pct: ["tmux", "split-window", "-h", "-d", "-l", "%d%%" % pct, shlex.join(c)], True),
    "zellij": (lambda e: e.get("ZELLIJ"),
               lambda c, pct: ["zellij", "run", "--direction", "right", "--close-on-exit", "--"] + c, True),
    "terminator": (lambda e: e.get("TERMINATOR_UUID") and shutil.which("remotinator"),
                   lambda c, pct: ["remotinator", "vsplit", "-x", shlex.join(c)], True),
    "kitty": (lambda e: e.get("KITTY_WINDOW_ID"),
              lambda c, pct: [_program("kitty"), "@", "launch", "--location=vsplit", "--keep-focus", "--cwd=current"] + c,
              True),
    "wezterm": (lambda e: e.get("WEZTERM_PANE"),
                lambda c, pct: [_program("wezterm"), "cli", "split-pane", "--right", "--percent", str(pct), "--"] + c,
                True),
    "iterm2": (lambda e: e.get("ITERM_SESSION_ID") or e.get("TERM_PROGRAM") == "iTerm.app",
               lambda c, pct: _iterm_split(c), True),
    "konsole": (lambda e: e.get("KONSOLE_DBUS_SERVICE") or e.get("KONSOLE_VERSION"),
                lambda c, pct: ["konsole", "--new-tab", "-e"] + c, False),
    "gnome-terminal": (lambda e: e.get("GNOME_TERMINAL_SCREEN") or e.get("GNOME_TERMINAL_SERVICE"),
                       lambda c, pct: ["gnome-terminal", "--tab", "--"] + c, False),
    "apple-terminal": (lambda e: e.get("TERM_PROGRAM") == "Apple_Terminal",
                       lambda c, pct: _osascript('tell application "Terminal" to do script %s'
                                                 % _applescript_str(shlex.join(c))), False),
    # Last: WT_SESSION is inherited by GUI terminals started from Windows Terminal.
    "windows-terminal": (lambda e: e.get("WT_SESSION") and shutil.which("wt.exe"),
                         lambda c, pct: ["wt.exe", "-w", "0", "split-pane", "-V", "-s", "%.2f" % (pct / 100)]
                         + (_wsl_wrap(c) if sources.in_wsl() else c), True),
}


IDE_VARS = ("VSCODE_PID", "VSCODE_IPC_HOOK", "VSCODE_IPC_HOOK_CLI", "VSCODE_CWD")


def in_ide(env):
    """True inside an IDE: its terminal panel, or an agent extension it started. Variables of the
    terminal the IDE was started from (ITERM_SESSION_ID, KITTY_WINDOW_ID, TMUX, WT_SESSION, ...)
    are inherited there and must not be taken for the terminal we are in."""
    return (env.get("TERM_PROGRAM") == "vscode" or any(env.get(k) for k in IDE_VARS)
            or str(env.get("TERMINAL_EMULATOR", "")).startswith("JetBrains"))


def detect_terminal(env=None):
    env = os.environ if env is None else env
    if env.get("TMUX") and env.get("TERM_PROGRAM") == "tmux":
        return "tmux"  # tmux started inside (e.g. in an IDE's terminal) says so itself
    if in_ide(env):
        return None
    for name, (detect, _, _) in TERMINALS.items():
        if detect(env):
            return name
    return None


def describe_terminal(name):
    if not name:
        return "no supported terminal detected"
    return "%s (%s)" % (name, "opens a split pane" if TERMINALS[name][2] else "opens a new tab or window")


def _run(argv):
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=10, **DETACHED)
        return r.returncode == 0, r.stderr.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


# ---------- browser fallback ----------

def _listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def dashboard_at(port):
    """'ours' if an agent-footprint dashboard answers on the port, 'other' if another program
    does, None if the port is free."""
    if not _listening(port):
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    try:
        with opener.open("http://127.0.0.1:%d/api/info" % port, timeout=2) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return "other"
    if isinstance(data, dict) and (data.get("app") == "agent-footprint" or {"ecologits", "history"} <= set(data)):
        return "ours"
    return "other"


def _open_url(url):
    if sources.in_wsl():
        for argv in (["wslview", url], ["explorer.exe", url]):
            if shutil.which(argv[0]):
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **DETACHED)
                return True
    return webbrowser.open(url)


def open_browser(cfg):
    """Start the dashboard if it isn't running, and open it in the browser (only when we started it,
    so new sessions don't keep adding tabs). Returns a short description."""
    port = cfg["port"]
    url = "http://localhost:%d" % port
    owner = dashboard_at(port)
    if owner == "ours":
        return "dashboard already running at %s" % url
    if owner == "other":
        return "port %d is used by another program; set \"port\" in %s" % (port, config.user_path())
    os.makedirs(config.cache_dir(), exist_ok=True)
    log = open(os.path.join(config.cache_dir(), "serve.log"), "ab")
    # Stops by itself once no browser has been connected for a while, so hooks don't leave it running.
    subprocess.Popen(self_command() + ["serve", "--port", str(port), "--idle-exit", str(IDLE_EXIT_S)],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, **DETACHED)
    for _ in range(30):
        if _listening(port):
            break
        time.sleep(0.1)
    _open_url(url)
    return "started the dashboard at %s" % url


# ---------- the hook ----------

def read_hook_input(timeout=1.0):
    """The agent's JSON hook input on stdin, or {} (nothing sent, not JSON, or a terminal)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = json.loads(_read_stdin(timeout) or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_stdin(timeout):
    """All of stdin, or "" if nothing arrives within `timeout` (an agent that sends no input)."""
    if os.name != "nt":
        return sys.stdin.read() if select.select([sys.stdin], [], [], timeout)[0] else ""
    got = []  # select() only works on sockets on Windows: read in a thread instead
    reader = threading.Thread(target=lambda: got.append(sys.stdin.read()), daemon=True)
    reader.start()
    reader.join(timeout)
    return got[0] if got else ""


def learn_locations(agent, payload):
    """Remember where this agent's logs are, from the transcript path or its environment."""
    learned = []
    path = payload.get("transcript_path")
    tools = [agent] if agent in sources.SOURCES else list(sources.SOURCES)
    for tool in tools:
        cls = sources.SOURCES[tool]
        root = cls.root_from_transcript(path) if isinstance(path, str) else None
        if root is None and agent == tool:
            root = cls(None).root  # the default location, as the agent's environment defines it
        if root and sources.learn_root(tool, root):
            learned.append((tool, root))
    return learned


def open_view(cfg, agent=None, payload=None, force_terminal=None):
    """Open the watch view (or the dashboard). Returns a list of human-readable lines."""
    out = []
    if agent or payload:
        for tool, root in learn_locations(agent, payload or {}):
            out.append("learned %s logs at %s" % (sources.TOOL_LABEL[tool], root))
    if watch_running():
        return out + ["a watch view is already open"]
    launch = cfg["launch"]
    choice = force_terminal or launch.get("terminal") or "auto"
    name = detect_terminal() if choice == "auto" else (choice if choice in TERMINALS else None)
    if name:
        cmd = self_command() + ["watch"]
        ok, err = _run(TERMINALS[name][1](cmd, int(launch.get("split_percent") or 40)))
        if ok:
            os.makedirs(config.cache_dir(), exist_ok=True)
            open(_starting_file(), "w", encoding="utf-8").close()
            return out + ["opened the watch view in %s" % describe_terminal(name)]
        out.append("couldn't open a pane in %s: %s%s" % (name, err or "command failed", _hint(name, err)))
    elif choice not in ("auto", "none"):
        out.append("unknown terminal %r in config" % choice)
    if launch.get("fallback", "browser") == "browser":
        return out + [open_browser(cfg)]
    return out + ["no terminal to open a pane in; fallback is off"]


def _hint(name, err):
    """What to do about a failed split, where we know."""
    if name == "kitty":
        return " (enable allow_remote_control in kitty.conf)"
    if name in ("iterm2", "apple-terminal") and ("-1743" in err or "not authorized" in err.lower()
                                                  or "timed out" in err.lower()):
        return (" (macOS asks once whether agent-footprint may control the terminal: run `agent-footprint open`"
                " there by hand and allow it, or enable it under System Settings > Privacy & Security > Automation)")
    return ""


def run(agent=None, hook=False, terminal=None):
    """Entry point for `agent-footprint open`. In hook mode: silent, never fails."""
    if not hook:
        for line in open_view(config.load_config(), agent, None, terminal):
            print(line)
        return 0
    try:
        payload = read_hook_input()
        lines = open_view(config.load_config(), agent, payload, terminal)
        try:
            os.makedirs(config.cache_dir(), exist_ok=True)
            with open(os.path.join(config.cache_dir(), "hook.log"), "w", encoding="utf-8") as f:  # last run, for troubleshooting
                f.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), agent or "", "\n".join(lines)))
        except OSError:
            pass
    except Exception:
        pass
    return 0
