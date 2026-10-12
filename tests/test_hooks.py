import json
import os
import shlex
import shutil
import tempfile
import unittest
from unittest import mock

from agent_footprint import config, hooks, launch, sources
from agent_footprint.store import Store


EXE = os.path.abspath("/opt/bin/agent-footprint")  # what sys.argv[0] below resolves to ("C:\\opt\\..." on Windows)


class Env(unittest.TestCase):
    """Every agent's config and our cache live in a temp dir."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        env = {
            "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude-work"),
            "CODEX_HOME": os.path.join(self.dir, "codex"),
            "GEMINI_CLI_HOME": os.path.join(self.dir, "ghome"),
            "XDG_CONFIG_HOME": os.path.join(self.dir, "xdgconfig"),
            "XDG_DATA_HOME": os.path.join(self.dir, "xdgdata"),
            "XDG_CACHE_HOME": os.path.join(self.dir, "cache"),
            "AGENT_FOOTPRINT_CONFIG": os.path.join(self.dir, "af.json"),
        }
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.argv = mock.patch("sys.argv", ["/opt/bin/agent-footprint"])
        self.argv.start()
        self.exists = mock.patch("agent_footprint.launch.os.path.exists",
                                 side_effect=lambda p: p in ("/opt/bin/agent-footprint", EXE) or os.path.lexists(p))
        self.exists.start()

    def tearDown(self):
        self.exists.stop()
        self.argv.stop()
        self.env.stop()
        shutil.rmtree(self.dir)

    def write_json(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

    def read_json(self, path):
        with open(path) as f:
            return json.load(f)


class HookInstallTest(Env):
    def test_claude_hook_keeps_other_settings_and_is_idempotent(self):
        path = os.path.join(self.dir, "claude-work", "settings.json")
        other = {"type": "command", "command": "echo hi"}
        dead = {"type": "command", "command": "/gone/impact_plugin/open_split.sh >/dev/null 2>&1 || true"}
        self.write_json(path, {"model": "opus", "hooks": {"SessionStart": [{"hooks": [other, dead]}],
                                                         "Stop": [{"hooks": [other]}]}})
        notes = hooks.AGENTS["claude"].install()
        hooks.AGENTS["claude"].install()  # twice: still one entry
        data = self.read_json(path)
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["Stop"], [{"hooks": [other]}])
        cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertEqual(cmds, ["echo hi", hooks.join_command([EXE, "open", "--hook", "--agent", "claude"])])
        self.assertTrue(any("no longer exists" in n for n in notes))
        self.assertTrue(os.path.exists(path + hooks.BACKUP_SUFFIX))
        self.assertTrue(hooks.AGENTS["claude"].installed())
        self.assertEqual(data["hooks"]["SessionStart"][-1]["matcher"], "startup|resume")

        hooks.AGENTS["claude"].remove()
        data = self.read_json(path)
        self.assertEqual([h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]], ["echo hi"])
        self.assertFalse(hooks.AGENTS["claude"].installed())

    def test_settings_with_byte_order_mark_and_non_ascii_text(self):
        path = os.path.join(self.dir, "claude-work", "settings.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8-sig") as f:  # as Windows Notepad saves it
            json.dump({"statusLine": {"command": "echo 'café → ok'"}}, f, ensure_ascii=False)
        hooks.AGENTS["claude"].install()
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("café → ok", text)  # kept as written, not as \u escapes
        self.assertTrue(hooks.AGENTS["claude"].installed())

    def test_windows_hook_command(self):
        self.assertEqual(hooks.join_command(["C:\\Users\\Steven Warmelink\\bin\\agent-footprint.exe", "open"])
                         if os.name == "nt" else None, None if os.name != "nt" else
                         '"C:\\Users\\Steven Warmelink\\bin\\agent-footprint.exe" open')
        with mock.patch.object(hooks.os, "name", "nt"), \
                mock.patch("agent_footprint.hooks.shutil.which", return_value=EXE):
            self.assertEqual(hooks.hook_program(), ["agent-footprint"])  # on PATH: works in any shell
        with mock.patch.object(hooks.os, "name", "nt"), \
                mock.patch("agent_footprint.hooks.shutil.which", return_value=None):
            self.assertEqual(hooks.hook_program(), [EXE])
        for argv in (["C:\\opt\\bin\\agent-footprint.exe", "open"], ["C:\\Users\\A B\\af.exe", "open", "--hook"]):
            with mock.patch.object(hooks.os, "name", "nt"):
                self.assertEqual(hooks.split_command(hooks.subprocess.list2cmdline(argv)), argv)

    def test_codex_and_gemini_formats(self):
        hooks.AGENTS["codex"].install()
        hooks.AGENTS["gemini"].install()
        codex = self.read_json(os.path.join(self.dir, "codex", "hooks.json"))
        gemini = self.read_json(os.path.join(self.dir, "ghome", ".gemini", "settings.json"))
        c = codex["hooks"]["SessionStart"][0]["hooks"][0]
        g = gemini["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual((c["type"], c["timeout"]), ("command", 15))           # seconds
        self.assertEqual((g["timeout"], g["name"]), (15000, "agent-footprint"))  # milliseconds
        self.assertIn("--agent gemini", g["command"])
        hooks.AGENTS["codex"].remove()
        self.assertEqual(self.read_json(os.path.join(self.dir, "codex", "hooks.json")), {})

    def test_opencode_plugin(self):
        a = hooks.AGENTS["opencode"]
        a.install()
        with open(a.target()) as f:
            text = f.read()
        self.assertIn(json.dumps([EXE, "open", "--hook", "--agent", "opencode"]), text)
        self.assertTrue(a.target().startswith(os.path.join(self.dir, "xdgconfig", "opencode", "plugins")))
        a.remove()
        self.assertFalse(a.installed())

    def test_refuses_to_rewrite_non_json(self):
        path = os.path.join(self.dir, "ghome", ".gemini", "settings.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write('{ // comments\n "theme": "x" }')
        with self.assertRaises(hooks.SetupError):
            hooks.AGENTS["gemini"].install()
        with open(path) as f:
            self.assertIn("comments", f.read())  # left untouched


class RobustnessTest(Env):
    def test_reports_and_repairs_a_hook_whose_program_is_gone(self):
        hooks.AGENTS["claude"].install()
        hooks.AGENTS["opencode"].install()
        os.makedirs(os.path.join(self.dir, "codex"))
        lines = "\n".join(hooks.status_lines())
        self.assertNotIn("BROKEN", lines)  # /opt/bin/agent-footprint "exists" (see Env)
        self.exists.stop()
        self.exists = mock.patch("agent_footprint.launch.os.path.exists", side_effect=os.path.lexists)
        self.exists.start()
        lines = "\n".join(hooks.status_lines())
        self.assertEqual(lines.count("HOOK BROKEN"), 2)
        self.assertIn("%s, which no longer exists" % EXE, lines)

    def test_refuses_hooks_from_a_temporary_environment(self):
        with mock.patch("sys.prefix", "/home/me/.cache/uv/archive-v0/AbC123"), mock.patch("builtins.print"):
            self.assertEqual(hooks.setup(yes=True, titles=False), 1)
        self.assertFalse(hooks.AGENTS["claude"].installed())
        self.assertFalse(config.load_config()["store_titles"])

    def test_titles_choice_is_saved_in_the_config(self):
        with mock.patch("builtins.print"), mock.patch("builtins.input", side_effect=["y"] + ["n"] * 4):
            hooks.setup(interactive=True)
        self.assertTrue(config.load_config()["store_titles"])
        with mock.patch("builtins.print"):
            hooks.setup(agents=["claude"], yes=True, titles=False)
        self.assertFalse(config.load_config()["store_titles"])
        self.assertTrue(hooks.AGENTS["claude"].installed())

    @unittest.skipIf(os.name == "nt", "reads /proc")
    def test_watch_pid_must_be_ours(self):
        def cmdline(data):
            return mock.patch("builtins.open", mock.mock_open(read_data=data))
        with mock.patch("os.kill"):
            with cmdline(b"/usr/bin/ssh-agent\x00-D\x00"):
                self.assertFalse(launch._alive(4242))
            with cmdline(b"/usr/bin/python3\x00-m\x00agent_footprint\x00watch\x00"):
                self.assertTrue(launch._alive(4242))
            with cmdline(b"/usr/bin/python3\x00/home/me/.local/bin/agent-footprint\x00watch\x00"):
                self.assertTrue(launch._alive(4242))


    def test_process_check_without_proc(self):  # macOS: ps instead of /proc
        ps = mock.Mock(returncode=0, stdout=b"/usr/bin/python3 /Users/me/.local/bin/agent-footprint watch\n")
        with mock.patch("os.kill"), mock.patch("builtins.open", side_effect=OSError), \
                mock.patch("agent_footprint.launch.subprocess.run", return_value=ps):
            self.assertTrue(launch._cmdline(4242).endswith(b"watch\n"))
            if os.name != "nt":
                self.assertTrue(launch._alive(4242))
                ps.stdout = b"/usr/bin/ssh-agent -l\n"
                self.assertFalse(launch._alive(4242))

    @unittest.skipUnless(os.name == "nt", "Windows process check")
    def test_windows_process_check(self):
        self.assertTrue(launch._alive(os.getpid()))  # python.exe
        self.assertFalse(launch._alive(4_000_000))   # no such process


class PortTest(Env):
    def serve(self, handler):
        from http.server import ThreadingHTTPServer
        import threading
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_tells_our_dashboard_from_another_program(self):
        from http.server import BaseHTTPRequestHandler

        def handler(body):
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass
            return H
        self.assertEqual(launch.dashboard_at(self.serve(handler(b'{"app": "agent-footprint"}'))), "ours")
        self.assertEqual(launch.dashboard_at(self.serve(handler(b"<html>jupyter</html>"))), "other")
        port = self.serve(handler(b"{}"))
        with mock.patch("agent_footprint.launch.subprocess.Popen") as popen:
            out = launch.open_browser({"port": port})
        self.assertIn("used by another program", out)
        popen.assert_not_called()

    def test_serve_reports_a_busy_port(self):
        from http.server import BaseHTTPRequestHandler
        from agent_footprint import server
        port = self.serve(BaseHTTPRequestHandler)
        with self.assertRaises(SystemExit) as e, mock.patch("agent_footprint.server.App.start") as start:
            server.serve(port=port, store_path=":memory:")
        self.assertIn("already in use", str(e.exception))
        start.assert_not_called()

    def test_idle_exit(self):
        from agent_footprint import server
        app = server.App(Store(":memory:"))
        app.touch(+1)
        app.last_seen -= 3600
        self.assertEqual(app.idle_for(), 0)  # a browser is connected
        app.touch(-1)
        app.last_seen -= 3600
        self.assertGreater(app.idle_for(), 3000)


class ConfigProblemsTest(Env):
    def test_problems_are_reported_not_hidden(self):
        path = os.environ["AGENT_FOOTPRINT_CONFIG"]
        self.write_json(path, {"compare_contry": "NLD", "compare_country": "XXX", "batch_size": "lots",
                               "launch": {"terminal": "alacritty"}, "sources": {"cursor": {}},
                               "providers": {"openai": {"datacenter_pue": "low"}}, "_note": "fine"})
        cfg = config.load_config()
        text = "\n".join(cfg["problems"])
        for expected in ('"compare_contry"', '"XXX"', '"batch_size"', '"alacritty"', '"sources.cursor"',
                         '"providers.openai.datacenter_pue"'):
            self.assertIn(expected, text)
        self.assertNotIn("_note", text)
        self.assertEqual(cfg["batch_size"], 64)  # the default instead
        with open(path, "w") as f:
            f.write('{"port": 9000,')  # half-saved
        cfg = config.load_config()
        self.assertEqual(cfg["port"], 8765)
        self.assertIn("can't be read", cfg["problems"][0])

    def test_defaults_have_no_problems(self):
        self.assertEqual(config.load_config()["problems"], [])
        config.ensure_user_config()
        self.assertEqual(config.load_config()["problems"], [])


class HostTest(Env):
    def test_only_local_host_names(self):
        import threading
        import urllib.error
        import urllib.request
        from agent_footprint import server
        app = server.App(Store(":memory:"))
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(host):
            req = urllib.request.Request("http://127.0.0.1:%d/api/sessions" % port, headers={"Host": host})
            try:
                return opener.open(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(get("localhost:%d" % port), 200)
        self.assertEqual(get("127.0.0.1:%d" % port), 200)
        self.assertEqual(get("evil.example:%d" % port), 403)
        app.host = "0.0.0.0"  # deliberately exposed: any name
        self.assertEqual(get("my-laptop.lan:%d" % port), 200)


class LaunchTest(Env):
    def test_ide_is_not_mistaken_for_the_terminal_it_was_started_from(self):
        inherited = {"ITERM_SESSION_ID": "w0t0p0:ABC", "KITTY_WINDOW_ID": "1", "TMUX": "/tmp/t", "WT_SESSION": "x"}
        self.assertIsNone(launch.detect_terminal(dict(inherited, TERM_PROGRAM="vscode")))  # VS Code's terminal
        self.assertIsNone(launch.detect_terminal(dict(inherited, VSCODE_PID="42")))  # an extension's hook
        self.assertIsNone(launch.detect_terminal(dict(inherited, TERMINAL_EMULATOR="JetBrains-JediTerm")))
        self.assertEqual(launch.detect_terminal({"VSCODE_PID": "42", "TMUX": "/tmp/t", "TERM_PROGRAM": "tmux"}), "tmux")

    def test_iterm_splits_the_agents_own_session(self):
        with mock.patch.dict(os.environ, {"ITERM_SESSION_ID": "w0t1p0:4F2A-UUID"}):
            argv = launch.TERMINALS["iterm2"][1](["/opt/bin/agent-footprint", "watch"], 40)
        script = "\n".join(argv[2::2])
        self.assertEqual(argv[:2], ["osascript", "-e"])
        self.assertIn('if unique id of s is "4F2A-UUID" then', script)
        self.assertIn('tell s to split vertically with default profile command "/opt/bin/agent-footprint watch"', script)
        self.assertTrue(script.endswith("end tell"))

    def test_mac_app_bundle_tools_and_permission_hint(self):
        bundle = os.path.join("/Applications", launch.APP_BUNDLES["wezterm"])
        with mock.patch("agent_footprint.launch.shutil.which", return_value=None), \
                mock.patch.object(launch.sys, "platform", "darwin"), \
                mock.patch("agent_footprint.launch.os.path.exists", side_effect=lambda p: p == bundle):
            self.assertEqual(launch.TERMINALS["wezterm"][1](["x"], 40)[0], bundle)
        self.assertIn("Automation", launch._hint("iterm2", "execution error: Not authorized to send Apple events (-1743)"))
        self.assertEqual(launch._hint("tmux", "boom"), "")

    def test_detect_terminal(self):
        self.assertEqual(launch.detect_terminal({"TMUX": "/tmp/x", "KITTY_WINDOW_ID": "1"}), "tmux")
        self.assertEqual(launch.detect_terminal({"WEZTERM_PANE": "0"}), "wezterm")
        self.assertEqual(launch.detect_terminal({"TERM_PROGRAM": "Apple_Terminal"}), "apple-terminal")
        self.assertIsNone(launch.detect_terminal({"TERM_PROGRAM": "vscode"}))

    def test_opens_split_once_and_learns_custom_locations(self):
        transcript = os.path.join(self.dir, "claude-work", "projects", "-home-me-app", "abc.jsonl")
        os.makedirs(os.path.dirname(transcript))
        open(transcript, "w").close()
        cfg = config.load_config()
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1", "TERM_PROGRAM": "tmux"}), \
                mock.patch("agent_footprint.launch._run", return_value=(True, "")) as run:
            lines = launch.open_view(cfg, "claude", {"transcript_path": transcript})
            argv = run.call_args[0][0]
            self.assertEqual(argv[:3], ["tmux", "split-window", "-h"])
            self.assertIn(shlex.quote(EXE) + " watch", argv[-1])
            self.assertTrue(any("learned Claude Code logs" in l for l in lines))
            launch.open_view(cfg, "claude", {"transcript_path": transcript})  # second session: no second pane
            self.assertEqual(run.call_count, 1)
        root = os.path.join(self.dir, "claude-work", "projects")
        self.assertEqual(sources.learned_roots()["claude"], [root])
        self.assertIn(root, sources.roots_for("claude", {}, homes=[]))

    def test_falls_back_to_browser_when_no_terminal(self):
        cfg = config.load_config()
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "ZELLIJ", "TERMINATOR_UUID", "KITTY_WINDOW_ID",
                                                                 "WEZTERM_PANE", "ITERM_SESSION_ID", "WT_SESSION",
                                                                 "GNOME_TERMINAL_SCREEN", "KONSOLE_VERSION", "TERM_PROGRAM")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("agent_footprint.launch.open_browser", return_value="started the dashboard") as browser:
            self.assertEqual(launch.open_view(cfg)[-1], "started the dashboard")
            browser.assert_called_once()

    def test_hook_mode_is_silent_and_never_fails(self):
        with mock.patch("agent_footprint.launch.open_view", side_effect=RuntimeError("boom")), \
                mock.patch("builtins.print") as p:
            self.assertEqual(launch.run("claude", hook=True), 0)
            p.assert_not_called()


class RootsTest(unittest.TestCase):
    def test_transcript_roots(self):
        self.assertEqual(sources.SOURCES["claude"].root_from_transcript("/x/.claude-alt/projects/p/s.jsonl"),
                         "/x/.claude-alt/projects")
        self.assertEqual(sources.SOURCES["codex"].root_from_transcript(
            "/srv/codex/sessions/2026/10/09/rollout-2026-10-09T10-00-00-abc.jsonl"), os.path.abspath("/srv/codex"))
        self.assertEqual(sources.SOURCES["gemini"].root_from_transcript(
            "/h/.gemini/tmp/proj/chats/session-1.jsonl"), "/h/.gemini/tmp")
        self.assertIsNone(sources.SOURCES["claude"].root_from_transcript("/x/notes.txt"))

    def test_config_paths_and_windows_homes(self):
        d = tempfile.mkdtemp()
        try:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            win = os.path.join(d, "Users", "Steven")
            for p in (a, b, os.path.join(win, ".claude", "projects")):
                os.makedirs(p)
            roots = sources.roots_for("claude", {"path": [a, b, a]}, learned={}, homes=[win])
            self.assertEqual(roots, [a, b, os.path.join(win, ".claude", "projects")])
            self.assertEqual(sources.roots_for("claude", {"path": os.path.join(d, "missing")}, learned={}, homes=[]),
                             [os.path.join(d, "missing")])  # primary listed even if missing, for `info`
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
