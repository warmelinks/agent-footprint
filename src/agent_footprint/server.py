"""Live footprint dashboard in the browser.

    agent-footprint serve            # then open http://localhost:8765
    agent-footprint serve --port 9000

A background thread keeps the history store in sync with every agent's logs; each browser tab
streams one view (a period or a session) over Server-Sent Events. Standard library only.
"""
import errno
import json
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, model, periods, sources, updates
from .store import Store
from .summary import Summary
from .views import View

HERE = os.path.dirname(os.path.abspath(__file__))
POLL_S = 0.5
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
SEND_EVERY_S = 1.0     # coalesce bursts of updates
KEEPALIVE_S = 15


class App:
    """State shared by all request threads."""

    def __init__(self, store):
        self.store = store
        self.cfg_mtime = config.mtime()
        self.cfg = config.load_config()
        self.cfg_version = 0
        self.syncer = sources.Syncer(store, self.cfg)
        self.update_notice = None
        self.host = "127.0.0.1"
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.streams = 0                 # browser tabs connected
        self.last_seen = time.time()     # last request or stream activity

    def touch(self, streams=0):
        with self.lock:
            self.streams += streams
            self.last_seen = time.time()

    def idle_for(self):
        """Seconds without a browser connected (0 while one is)."""
        with self.lock:
            return 0 if self.streams else time.time() - self.last_seen

    def start(self):
        self.syncer.sync()
        threading.Thread(target=self._sync_loop, daemon=True).start()
        if self.cfg["auto_update_check"]:
            threading.Thread(target=self._check_updates, daemon=True).start()

    def _sync_loop(self):
        while not self.stop.wait(POLL_S):
            try:
                if config.mtime() != self.cfg_mtime:
                    self.cfg_mtime, self.cfg = config.mtime(), config.load_config()
                    self.syncer = sources.Syncer(self.store, self.cfg)
                    self.syncer.sync()
                    self.cfg_version += 1  # streams recompute with the new assumptions
                self.syncer.sync(quick=True)
            except Exception as e:  # keep syncing whatever one bad file does
                print("sync error: %s" % e)

    def _check_updates(self):
        self.update_notice = updates.check()

    def info(self):
        return {"app": "agent-footprint", "version": updates.own_version(), "ecologits": model.ecologits_version(),
                "sources": self.syncer.found(),
                "notices": ["Config: %s" % p for p in self.cfg["problems"]] + self.syncer.notices(),
                "history": self.store.stats(), "config": config.user_path(), "update": self.update_notice}


def make_handler(app):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=os.path.join(HERE, "web"), **kwargs)

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            app.touch()
            if not self._host_ok():
                return self.send_error(403, "Unexpected Host header")
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/api/sessions":
                return self._json([dict(s, tool_label=sources.TOOL_LABEL.get(s["tool"], s["tool"]))
                                   for s in app.store.sessions(limit=60)])
            if url.path == "/api/periods":
                return self._json(periods.available(app.store.local_days()))
            if url.path == "/api/info":
                return self._json(app.info())
            if url.path == "/api/stream":
                return self._stream((q.get("view") or q.get("session") or ["day:today"])[0])
            return super().do_GET()

        def _host_ok(self):
            """Refuse requests addressed to another host name: a web page that rebinds its own
            domain to 127.0.0.1 must not be able to read your sessions. Bound to another
            interface on purpose (--host), any host name is accepted."""
            if app.host not in ("127.0.0.1", "localhost", "::1"):
                return True
            host = (self.headers.get("Host") or "").lower()
            name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            return name in LOCAL_HOSTS

        def _json(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, event, data):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
            self.wfile.flush()

        def _reset(self, view):
            self._send("reset", dict(view.snapshot(app.cfg), assumptions=model.describe(app.cfg),
                                     tools=sources.TOOL_LABEL, info=app.info()))

        def _stream(self, wanted):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            app.touch(+1)
            try:
                try:
                    view = View(app.store, wanted)
                except (LookupError, ValueError) as e:
                    self._send("error", {"message": str(e)})
                    return
                self._reset(view)
                cfg_version, pending, last_send, last_write = app.cfg_version, [], 0.0, time.time()
                while True:
                    time.sleep(POLL_S)
                    now = time.time()
                    if app.cfg_version != cfg_version:
                        cfg_version = app.cfg_version
                        view.load()
                        self._reset(view)
                        pending, last_write = [], now
                        continue
                    res = view.poll()
                    if res == "reset":
                        self._reset(view)
                        pending, last_write = [], now
                        continue
                    pending += res
                    if pending and now - last_send >= SEND_EVERY_S:
                        self._send("update", dict(view.snapshot(app.cfg), latest=Summary.delta(pending, app.cfg)))
                        pending, last_send, last_write = [], now, now
                    elif now - last_write >= KEEPALIVE_S:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_write = now
            except ConnectionError:  # the tab was closed (broken pipe, reset, or aborted on Windows)
                return
            finally:
                app.touch(-1)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that is in use (and then requests go
    # to either one), so it is left off there; elsewhere it only allows a quick restart.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):
            return  # a browser tab closed mid-response (WinError 10053 on Windows): nothing to report
        super().handle_error(request, client_address)


def _stop_when_idle(app, server, idle_s):
    while not app.stop.wait(5):
        if app.idle_for() >= idle_s:
            print("No browser connected for %s; stopping." % ("%d minutes" % (idle_s // 60) if idle_s >= 120 else "%d s" % idle_s))
            server.shutdown()
            return


def serve(host="127.0.0.1", port=None, store_path=None, idle_exit=None):
    """idle_exit: stop after this many seconds without a browser connected (None: run until stopped)."""
    store = Store(store_path)
    app = App(store)
    app.host = host
    port = port or app.cfg["port"]
    try:
        server = Server((host, port), make_handler(app))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise SystemExit("Port %d is already in use. Use --port, or set \"port\" in %s."
                             % (port, config.user_path()))
        raise
    server.daemon_threads = True
    app.start()
    if idle_exit:
        threading.Thread(target=_stop_when_idle, args=(app, server, idle_exit), daemon=True).start()
    print("Agent footprint dashboard: http://localhost:%d" % port)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: listening on %s without authentication; anyone who can reach this port sees your "
              "sessions, projects and usage." % host)
    for problem in app.cfg["problems"]:
        print("Config: %s" % problem)
    found = app.syncer.found()
    print("Reading: %s" % (", ".join(sources.TOOL_LABEL[t] for t in found) or "no agent logs found yet"))
    from .hooks import AGENTS
    if not any(a.installed() for a in AGENTS.values()):
        print("Tip: run `agent-footprint setup` to open the footprint whenever an agent session starts.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
