import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_footprint import config, store as store_mod
from agent_footprint.sources import Syncer
from agent_footprint.sources.base import FileSource
from agent_footprint.sources.claude import ClaudeSource
from agent_footprint.sources.copilot import CopilotSource, estimate_tokens, replay
from agent_footprint.sources.codex import CodexSource
from agent_footprint.sources.gemini import GeminiSource
from agent_footprint.sources.opencode import OpenCodeSource
from agent_footprint.sources.vibe import VibeSource, model_of
from agent_footprint.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _claude_line(request_id, output, model_id="claude-opus-5-5", sidechain=False, ts="2026-10-05T10:00:00Z"):
    return json.dumps({
        "type": "assistant", "requestId": request_id, "timestamp": ts, "isSidechain": sidechain,
        "message": {"model": model_id, "usage": {
            "input_tokens": 3, "output_tokens": output,
            "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 10}},
    }) + "\n"


class Tmp(unittest.TestCase):
    titles = True  # the reader tests check titles too; TitlesTest checks the default (off)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)  # runs last, after the tests' own cleanups close files
        self.store = Store(":memory:")
        self.titles_patch = mock.patch.object(FileSource, "store_titles", self.titles)
        self.titles_patch.start()
        self.addCleanup(self.titles_patch.stop)

    def append(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(text)

    def by_id(self):
        return {r["id"]: r for r in self.store.records()}


class ClaudeTest(Tmp):
    def test_dedupes_by_request_id_and_waits_for_complete_lines(self):
        src, path = ClaudeSource(self.dir), os.path.join(self.dir, "proj", "s1.jsonl")
        self.append(path, json.dumps({"type": "user", "message": {"content": "Fix the login bug"}}) + "\n" + _claude_line("r1", 10))
        src.sync(self.store)
        self.assertEqual(self.by_id()["r1"]["tokens"]["output"], 10)
        self.assertEqual(self.store.sessions()[0]["title"], "Fix the login bug")

        full = _claude_line("r1", 50)  # same response, a later content block with grown usage
        self.append(path, full[:20])
        self.assertEqual(src.sync(self.store), 0)
        self.append(path, full[20:])
        self.assertEqual(src.sync(self.store), 1)
        self.assertEqual(self.by_id()["r1"]["tokens"]["output"], 50)
        self.append(path, _claude_line("r1", 50))  # identical repeat: no change
        self.assertEqual(src.sync(self.store), 0)
        self.assertEqual(len(self.store.records()), 1)

    def test_skips_synthetic_and_reads_subagents(self):
        src = ClaudeSource(self.dir)
        self.append(os.path.join(self.dir, "proj", "s1.jsonl"), _claude_line("r1", 10) + _claude_line("x", 0, model_id="<synthetic>"))
        self.append(os.path.join(self.dir, "proj", "s1", "subagents", "agent-a.jsonl"), _claude_line("r2", 20))
        src.sync(self.store)
        recs = self.by_id()
        self.assertEqual(set(recs), {"r1", "r2"})
        self.assertEqual((recs["r1"]["source"], recs["r2"]["source"]), ("main", "subagent"))
        self.assertEqual(recs["r2"]["session"], "claude:s1")

    def test_history_survives_purged_logs_and_resumes_after_restart(self):
        path = os.path.join(self.dir, "proj", "s1.jsonl")
        db = os.path.join(self.dir, "history.sqlite")
        self.append(path, _claude_line("r1", 10))
        first = Store(db)
        ClaudeSource(self.dir).sync(first)
        first.close()
        self.append(path, _claude_line("r2", 10))
        store = Store(db)                 # a new process: picks up where the last one stopped
        self.addCleanup(store.close)
        self.assertEqual(ClaudeSource(self.dir).sync(store), 1)
        os.remove(path)                   # the agent purges its old logs
        ClaudeSource(self.dir).sync(store)
        self.assertEqual({r["id"] for r in store.records()}, {"r1", "r2"})


class CodexTest(Tmp):
    def test_fixture(self):
        src = CodexSource(os.path.join(FIXTURES, "codex"))
        src.sync(self.store)
        recs = self.store.records()
        self.assertEqual(len(recs), 2)  # the info:null event and the repeated total are skipped
        first, second = recs
        self.assertEqual(first["model"], "gpt-5.3-codex")
        self.assertEqual(first["tokens"], {"output": 600, "input": 4000, "cache_creation": 0, "cache_read": 8000})
        self.assertEqual(second["model"], "gpt-6.1-sol")
        self.assertEqual(second["tokens"]["input"], 3000)
        s = self.store.sessions()[0]
        self.assertEqual((s["id"], s["project"], s["title"]),
                         ("codex:0b7e2c1a-1111-2222-3333-444455556666", "webshop", "Add a checkout page to the webshop"))

    def test_archived_rollout_does_not_double_count(self):
        root = os.path.join(self.dir, "codex")
        shutil.copytree(os.path.join(FIXTURES, "codex"), root)
        CodexSource(root).sync(self.store)
        src_dir = os.path.join(root, "sessions", "2026", "10", "09")
        os.makedirs(os.path.join(root, "archived_sessions"))
        for f in os.listdir(src_dir):
            shutil.move(os.path.join(src_dir, f), os.path.join(root, "archived_sessions", f))
        CodexSource(root).sync(self.store)
        self.assertEqual(len(self.store.records()), 2)


    def test_compressed_rollout(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest("zstandard not installed")
        root = os.path.join(self.dir, "codex")
        shutil.copytree(os.path.join(FIXTURES, "codex"), root)
        day = os.path.join(root, "sessions", "2026", "10", "09")
        for f in os.listdir(day):
            with open(os.path.join(day, f), "rb") as plain, open(os.path.join(day, f + ".zst"), "wb") as z:
                z.write(zstandard.ZstdCompressor().compress(plain.read()))
            os.remove(os.path.join(day, f))
        CodexSource(root).sync(self.store)
        self.assertEqual(len(self.store.records()), 2)


class GeminiTest(Tmp):
    def test_fixture(self):
        GeminiSource(os.path.join(FIXTURES, "gemini", "tmp")).sync(self.store)
        recs = self.by_id()
        self.assertEqual(set(recs), {"m2", "m3", "s1", "l2"})
        self.assertEqual(recs["m2"]["tokens"], {"output": 450, "input": 3050, "cache_creation": 0, "cache_read": 6000})
        self.assertEqual(recs["s1"]["source"], "subagent")
        self.assertEqual(recs["s1"]["session"], recs["m2"]["session"])  # subagent counted under its parent
        self.assertEqual(recs["m2"]["session"], "gemini:0b7e2c1a-aaaa-bbbb-cccc-000000000001")
        titles = {s["id"]: s["title"] for s in self.store.sessions()}
        self.assertEqual(titles["gemini:0b7e2c1a-aaaa-bbbb-cccc-000000000001"], "Explain the build failure")
        self.assertEqual(titles["gemini:legacy-1"], "Old session question")


class OpenCodeTest(Tmp):
    def make_db(self, path):
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT,
                                  title TEXT, version TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        """)
        conn.execute("INSERT INTO session VALUES ('ses_1', 'p', NULL, 's', '/home/me/api', 'Refactor auth', '1', 0, 0)")
        conn.execute("INSERT INTO session VALUES ('ses_2', 'p', 'ses_1', 's', '/home/me/api', 'subtask', '1', 0, 0)")
        return conn

    def add(self, conn, mid, sid, created, data):
        conn.execute("INSERT OR REPLACE INTO message VALUES (?, ?, ?, ?, ?)", (mid, sid, created, created, json.dumps(data)))
        conn.commit()

    def test_reads_database_incrementally(self):
        root = os.path.join(self.dir, "opencode")
        os.makedirs(root)
        conn = self.make_db(os.path.join(root, "opencode.db"))
        self.addCleanup(conn.close)
        assistant = lambda out: {"role": "assistant", "providerID": "anthropic", "modelID": "claude-sonnet-5-5",
                                 "time": {"created": 1791540000000}, "tokens": {"input": 50, "output": out, "reasoning": 10,
                                                                                 "cache": {"read": 4000, "write": 100}}}
        self.add(conn, "msg_u", "ses_1", 1791540000000, {"role": "user", "time": {"created": 1791540000000}})
        self.add(conn, "msg_a", "ses_1", 1791540001000, assistant(200))
        self.add(conn, "msg_b", "ses_2", 1791540002000, assistant(30))
        src = OpenCodeSource(root)
        src.sync(self.store)
        recs = self.by_id()
        self.assertEqual(set(recs), {"msg_a", "msg_b"})
        self.assertEqual(recs["msg_a"]["tokens"], {"output": 210, "input": 50, "cache_creation": 100, "cache_read": 4000})
        self.assertEqual((recs["msg_b"]["session"], recs["msg_b"]["source"]), ("opencode:ses_1", "subagent"))
        self.assertEqual(self.store.sessions()[0]["title"], "Refactor auth")
        self.assertEqual(src.sync(self.store), 0)  # unchanged database: not queried again
        conn.execute("UPDATE message SET data=?, time_updated=? WHERE id='msg_a'", (json.dumps(assistant(500)), 1791540009000))
        conn.commit()
        os.utime(os.path.join(root, "opencode.db"))
        self.assertEqual(src.sync(self.store), 1)
        self.assertEqual(self.by_id()["msg_a"]["tokens"]["output"], 510)


class VibeTest(Tmp):
    titles = False  # Vibe's title is generated from the prompt: check that it stays out by default
    def meta(self, name, prompt, completion, steps, cached=None, end="2026-10-05T10:00:00+00:00", **extra):
        stats = {"steps": steps, "session_prompt_tokens": prompt, "session_completion_tokens": completion}
        if cached is not None:
            stats["session_cached_tokens"] = cached
        meta = dict({"session_id": name, "parent_session_id": None, "start_time": "2026-10-05T09:00:00+00:00",
                     "end_time": end, "title": "Fix the parser", "stats": stats,
                     "environment": {"working_directory": "C:\\Users\\me\\src\\shop"},
                     "config": {"active_model": "mistral-medium-3.5",
                                "models": [{"name": "mistral-vibe-cli-latest", "provider": "mistral",
                                            "alias": "mistral-medium-3.5"}]}}, **extra)
        path = os.path.join(self.dir, "session_20261005_090000_%s" % name[:8], "meta.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(path + ".tmp", path)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # a new mtime even within one tick
        return path

    def test_growing_session_becomes_one_record_per_increase(self):
        src = VibeSource(self.dir)
        self.meta("aaaaaaaa-1", 1000, 100, 2, cached=600)
        src.sync(self.store)
        self.meta("aaaaaaaa-1", 5000, 300, 5, cached=4000, end="2026-10-05T11:30:00+00:00")
        src.sync(self.store)
        src.sync(self.store)  # unchanged: nothing new
        recs = sorted(self.store.records(), key=lambda r: r["id"])
        self.assertEqual([r["requests"] for r in recs], [2, 3])
        self.assertEqual(recs[0]["tokens"], {"output": 100, "input": 400, "cache_creation": 0, "cache_read": 600})
        self.assertEqual(recs[1]["tokens"], {"output": 200, "input": 600, "cache_creation": 0, "cache_read": 3400})
        self.assertEqual(recs[1]["ts"], "2026-10-05T11:30:00+00:00")  # when it happened
        self.assertEqual((recs[0]["model"], recs[0]["provider"], recs[0]["session"]),
                         ("mistral-medium-3.5", "mistral", "vibe:aaaaaaaa-1"))
        self.assertEqual(self.store.sessions()[0]["project"], "shop")  # from a Windows path
        self.assertIsNone(self.store.sessions()[0]["title"])  # titles are off by default

    def test_older_format_subagents_and_resets(self):
        src = VibeSource(self.dir)
        self.meta("bbbbbbbb-1", 2000, 50, 3)  # no cached count (older Vibe)
        self.meta("cccccccc-1", 300, 30, 1, parent_session_id="bbbbbbbb-1")
        src.sync(self.store)
        by = {r["id"]: r for r in self.store.records()}
        self.assertEqual(by["bbbbbbbb-1:1"]["tokens"]["input"], 2000)
        self.assertEqual((by["cccccccc-1:1"]["session"], by["cccccccc-1:1"]["source"]), ("vibe:bbbbbbbb-1", "subagent"))
        self.meta("bbbbbbbb-1", 400, 10, 1)  # counters went back: counted afresh
        src.sync(self.store)
        self.assertEqual(self.by_id()["bbbbbbbb-1:2"]["tokens"]["input"], 400)

    def test_model_lookup_and_configured_folder(self):
        cfg = {"active_model": "local", "models": {"local": {"name": "qwen3-coder", "provider": "llamacpp"}}}
        self.assertEqual(model_of({"config": cfg}), ("local", "llamacpp"))
        self.assertEqual(model_of({"config": {"active_model": "x", "models": ["x", "y"]}}), ("x", None))
        home = os.path.join(self.dir, "vibehome")
        logs = os.path.join(self.dir, "elsewhere")
        os.makedirs(logs)
        os.makedirs(home)
        with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as f:
            f.write('active_model = "m"\n[session_logging]\nsave_dir = %s\nsession_prefix = "session"\n[tools.bash]\n'
                    % json.dumps(logs))
        with mock.patch.dict(os.environ, {"VIBE_HOME": home}):
            self.assertEqual(VibeSource().root, os.path.normpath(logs))


class CopilotTest(Tmp):
    titles = False

    @staticmethod
    def request(rid, ts, prompt=None, output=None, completion=None, rounds=1, model="claude-fable-5.1"):
        r = {"requestId": rid, "timestamp": ts, "modelId": "copilot/auto", "message": {"text": "secret prompt"}}
        if prompt is not None:
            meta = {"promptTokens": prompt, "outputTokens": output, "resolvedModel": model,
                    "toolCallRounds": [{"id": "r%d" % i, "timestamp": ts + 1000 * (i + 1), "modelId": model}
                                       for i in range(rounds)]}
            r["result"] = {"metadata": meta, "timings": {"totalElapsed": 5000}}
            if completion is not None:
                r["completionTokens"], r["promptTokens"] = completion, prompt
        return r

    def write(self, ops, folder="ws1", name="sess-1", ext=".jsonl"):
        path = os.path.join(self.dir, "workspaceStorage", folder, "chatSessions", name + ext)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(os.path.join(self.dir, "workspaceStorage", folder, "workspace.json"), "w", encoding="utf-8") as f:
            json.dump({"folder": "file:///c%3A/Users/Me/My%20Shop"}, f)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(o) for o in ops) if ext == ".jsonl" else json.dumps(ops))
        return path

    def test_replay(self):
        state = replay([{"kind": 0, "v": {"requests": [], "title": "x"}},
                        {"kind": 2, "k": ["requests"], "v": [{"a": 1}, {"a": 2}]},
                        {"kind": 1, "k": ["requests", 1, "a"], "v": 3},
                        {"kind": 2, "k": ["requests"], "v": [{"a": 4}], "i": 1},  # truncate at 1, then append
                        {"kind": 3, "k": ["title"]},
                        {"kind": 1, "k": ["missing", "path"], "v": 1}])           # ignored
        self.assertEqual(state, {"requests": [{"a": 1}, {"a": 4}]})

    def test_estimate(self):
        self.assertEqual(estimate_tokens(30000, None, 1, 50), {"output": 50, "input": 30000, "cache_creation": 0, "cache_read": 0})
        t = estimate_tokens(40000, 30000, 4, 900)  # 4 calls growing from 30k to 40k: 140k read, 10k new
        self.assertEqual((t["input"], t["cache_read"]), (10000, 130000))

    def test_reads_a_session_log(self):
        self.write([{"kind": 0, "v": {"sessionId": "sess-1", "customTitle": "Fix the shop", "requests": []}},
                    {"kind": 2, "k": ["requests"], "v": [self.request("q1", 1791700000000)]},
                    {"kind": 1, "k": ["requests", 0, "result"], "v": self.request("q1", 1791700000000, 30000, 50)["result"]},
                    {"kind": 2, "k": ["requests"], "v": [self.request("q2", 1791700100000, 40000, 300, completion=3900, rounds=4,
                                                                      model="mai-code-1.1-flash")]},
                    {"kind": 2, "k": ["requests"], "v": [self.request("q3", 1791700200000)]}])  # still running
        src = CopilotSource(self.dir)
        src.sync(self.store)
        by = self.by_id()
        self.assertEqual(set(by), {"q1", "q2"})
        self.assertEqual((by["q1"]["requests"], by["q1"]["tokens"]["output"]), (1, 50))
        self.assertEqual((by["q2"]["requests"], by["q2"]["tokens"]["output"], by["q2"]["model"]), (4, 3900, "mai-code-1.1-flash"))
        self.assertEqual(by["q2"]["ts"], store_mod.iso_of((1791700100000 + 4000) / 1000))  # when its last call ran
        self.assertEqual(by["q2"]["session"], "copilot:sess-1")
        session = self.store.sessions()[0]
        self.assertEqual((session["project"], session["title"]), ("My Shop", None))
        self.assertNotIn("secret", json.dumps([list(r) for r in self.store.db.execute("SELECT * FROM files")]))
        self.assertEqual(src.sync(self.store), 0)

    def test_older_json_and_chats_without_a_folder(self):
        state = {"sessionId": "old-1", "requests": [self.request("q9", 1791700000000, 1000, 20)]}
        self.write(state, name="old-1", ext=".json")
        empty = os.path.join(self.dir, "globalStorage", "emptyWindowChatSessions", "e-1.jsonl")
        os.makedirs(os.path.dirname(empty))
        with open(empty, "w", encoding="utf-8") as f:
            f.write(json.dumps({"kind": 0, "v": {"sessionId": "e-1", "requests": [self.request("q8", 1791700000000, 500, 5)]}}))
        CopilotSource(self.dir).sync(self.store)
        self.assertEqual({r["id"]: r["session"] for r in self.store.records()}, {"q9": "copilot:old-1", "q8": "copilot:e-1"})


class TitlesTest(Tmp):
    titles = False

    def write_session(self):
        path = os.path.join(self.dir, "proj", "s1.jsonl")
        self.append(path, json.dumps({"type": "user", "message": {"content": "my secret plan"}}) + "\n")
        self.append(path, json.dumps({"type": "user", "message": {"content": "second prompt"}}) + "\n")
        self.append(path, _claude_line("req_1", 10))
        return path

    def stored_text(self):
        return json.dumps([list(r) for t in ("sessions", "files")
                           for r in self.store.db.execute("SELECT * FROM %s" % t)])

    def test_off_by_default_and_not_kept_in_parser_state(self):
        self.assertFalse(config.load_config(os.path.join(self.dir, "none.json"))["store_titles"])
        path = self.write_session()
        ClaudeSource(self.dir).sync(self.store)
        self.assertIsNone(self.store.sessions()[0]["title"])
        self.assertNotIn("secret", self.stored_text())
        self.append(path, _claude_line("req_2", 10))
        ClaudeSource(self.dir).sync(self.store)
        self.assertNotIn("second prompt", self.stored_text())  # a later prompt doesn't become the title

    def test_turning_titles_off_erases_stored_ones(self):
        self.write_session()
        src = ClaudeSource(self.dir)
        src.store_titles = True
        src.sync(self.store)
        self.assertEqual(self.store.sessions()[0]["title"], "my secret plan")
        cfg = dict(config.load_config(os.path.join(self.dir, "none.json")),
                   sources={k: {"enabled": False} for k in ("claude", "codex", "gemini", "opencode")})
        Syncer(self.store, cfg)
        self.assertIsNone(self.store.sessions()[0]["title"])
        self.assertNotIn("secret", self.stored_text())


class QuickSyncTest(Tmp):
    def test_new_files_are_found_fast_and_old_ones_not_rechecked(self):
        import time
        from agent_footprint.sources import base
        old = os.path.join(self.dir, "proj", "old.jsonl")
        self.append(old, _claude_line("req_old", 1))
        for p in (old, os.path.dirname(old), self.dir):  # an old transcript in folders untouched since
            os.utime(p, (time.time() - 7200, time.time() - 7200))
        src = ClaudeSource(self.dir)
        src.sync(self.store)
        checked = []
        real = src._sync_unit
        src._sync_unit = lambda store, unit: checked.append(unit.path) or real(store, unit)
        src.sync(self.store, quick=True)
        self.assertEqual(checked, [])  # nothing recent: the old transcript isn't even stat'ed
        new = os.path.join(self.dir, "proj2", "new.jsonl")
        self.append(new, _claude_line("req_new", 1))
        src.last_dirs = 0  # as if DIRS_S had passed
        with mock.patch.object(base, "RESCAN_S", 1e9):
            src.sync(self.store, quick=True)
        self.assertIn("req_new", self.by_id())

    def test_a_folder_changed_within_the_same_clock_tick_as_the_scan_is_found(self):
        from agent_footprint.sources import base
        self.append(os.path.join(self.dir, "proj", "a.jsonl"), _claude_line("req_a", 1))
        src = ClaudeSource(self.dir)
        src.sync(self.store)
        scanned = os.stat(self.dir)
        self.append(os.path.join(self.dir, "proj2", "b.jsonl"), _claude_line("req_b", 1))
        # File times advance in clock ticks (~15.6 ms on Windows), so a folder can change after the
        # scan read its time and still show the same time.
        os.utime(self.dir, ns=(scanned.st_atime_ns, scanned.st_mtime_ns))
        src.last_dirs = 0  # as if DIRS_S had passed
        with mock.patch.object(base, "RESCAN_S", 1e9):
            src.sync(self.store, quick=True)
        self.assertIn("req_b", self.by_id())


class SeqTest(unittest.TestCase):
    def test_two_processes_never_share_a_sequence_number(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "h.sqlite")
            a, b = Store(path), Store(path)
            rec = lambda i: [{"id": i, "ts": "2026-10-05T10:00:00Z", "model": "m", "tokens": {"output": 1}}]
            a.upsert("claude", "claude:s", rec("r1"))  # a holds the write lock until it commits
            b.db.execute("PRAGMA busy_timeout = 0")
            with self.assertRaises(sqlite3.OperationalError):
                b.upsert("claude", "claude:s", rec("r2"))  # so b can't take a number meanwhile
            b.db.rollback()
            a.commit()
            b.upsert("claude", "claude:s", rec("r2"))
            b.commit()
            self.assertEqual(sorted(r["seq"] for r in a.records()), [1, 2])
            a.close(), b.close()
        finally:
            shutil.rmtree(d)


class HistoryLocationTest(unittest.TestCase):
    def test_moves_history_from_the_cache_folder(self):
        d = tempfile.mkdtemp()
        try:
            env = {"XDG_CACHE_HOME": os.path.join(d, "cache"), "XDG_DATA_HOME": os.path.join(d, "data")}
            with mock.patch.dict(os.environ, env):
                old = Store(os.path.join(d, "cache", "agent-footprint", "history.sqlite"))
                old.upsert("claude", "claude:s", [{"id": "r1", "ts": "2026-10-05T10:00:00Z", "model": "m",
                                                   "tokens": {"output": 5}}])
                old.commit()  # still open, as a running older version would have it
                path = store_mod.default_path()
                self.assertEqual(path, os.path.join(d, "data", "agent-footprint", "history.sqlite"))
                new = Store(path)
                self.assertEqual([r["id"] for r in new.records()], ["r1"])
                new.close()
                if os.name != "nt":  # Windows can't delete a file that is still open...
                    self.assertFalse(os.path.exists(old.path))
                old.close()  # ...so it goes once the old process has closed it
                self.assertEqual(store_mod.default_path(), path)
                self.assertFalse(os.path.exists(old.path))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
