import unittest
from datetime import date, datetime, timedelta

from agent_footprint import config, model, periods
from agent_footprint.store import Store, iso_of
from agent_footprint.summary import Summary, display
from agent_footprint.views import View

CFG = config.load_config("/nonexistent")


def local_iso(*args):
    """A local wall-clock time written the way agents log it (UTC)."""
    return iso_of(datetime(*args).timestamp())


class PeriodTest(unittest.TestCase):
    def test_parse_and_ids(self):
        self.assertEqual(periods.parse("week:2026-W41").start, date(2026, 10, 5))  # ISO weeks start on Monday
        self.assertEqual(periods.parse("month:2026-12").end, date(2027, 1, 1))
        self.assertEqual(periods.parse("year:2026").id, "year:2026")
        self.assertTrue(periods.parse("week:current").is_now())
        self.assertIsNone(periods.parse("claude:abc"))
        with self.assertRaises(ValueError):
            periods.parse("day:2026-13-01")

    def test_buckets(self):
        self.assertEqual(len(periods.parse("day:2026-10-10").buckets()), 24)
        self.assertEqual([b[1] for b in periods.parse("week:2026-W41").buckets()][:2], ["Mon", "Tue"])
        self.assertEqual(len(periods.parse("month:2026-02").buckets()), 28)
        self.assertEqual(periods.parse("year:2026").buckets()[9][1], "Oct")
        p = periods.parse("month:2026-10")
        self.assertEqual(p.bucket_of(datetime(2026, 10, 9, 23, 59).timestamp()), "2026-10-09")

    def test_shift_crosses_boundaries(self):
        self.assertEqual(periods.parse("month:2026-01").shift(-1).id, "month:2025-12")
        self.assertEqual(periods.parse("week:2026-W01").shift(-1).id, "week:2025-W52")
        self.assertEqual(periods.parse("day:2026-03-01").shift(-1).id, "day:2026-02-28")

    def test_available(self):
        out = periods.available(["2026-10-10", "2026-10-05", "2026-09-30"])
        self.assertEqual(out["month"], ["month:2026-10", "month:2026-09"])
        self.assertEqual(out["week"], ["week:2026-W41", "week:2026-W40"])


def _rec(rid, session, ts, output, model_id="claude-opus-5-5", tool="claude"):
    return {"id": rid, "ts": ts, "model": model_id, "provider": None, "source": "main", "session": session,
            "tokens": {"output": output, "input": 5, "cache_creation": 0, "cache_read": 2000}}


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.upsert("claude", "claude:a", [
            _rec("late", "claude:a", local_iso(2026, 10, 4, 23, 50), 10),
            _rec("early", "claude:a", local_iso(2026, 10, 5, 0, 10), 20)])
        self.store.upsert("codex", "codex:b", [
            _rec("noon", "codex:b", local_iso(2026, 10, 5, 12, 0), 30, "gpt-6.1-sol")])
        self.store.upsert_session("claude:a", "claude", "a", "proj", "First session")
        self.store.commit()

    def test_day_view_buckets_by_local_date(self):
        snap = View(self.store, "day:2026-10-05").snapshot(CFG)
        s = snap["summary"]
        self.assertEqual(s["n"], 2)  # "late" belongs to the day before
        self.assertEqual({b["key"]: b["n"] for b in s["buckets"] if b["n"]}, {"00": 1, "12": 1})
        self.assertEqual({r["tool"] for r in s["by_tool"]}, {"claude", "codex"})
        self.assertEqual(snap["view"]["prev"], "day:2026-10-04")

    def test_grouped_totals_equal_per_record_sums(self):
        s = View(self.store, "week:2026-W40").snapshot(CFG)["summary"]  # Sep 28 – Oct 4: only "late"
        self.assertEqual(s["n"], 1)
        s = View(self.store, "year:2026").snapshot(CFG)["summary"]
        want = [0.0, 0.0]
        for r in self.store.records():
            c = display(model.compute(r["model"], r["tokens"], CFG))["carbon"]
            want = [want[0] + c[0], want[1] + c[1]]
        for g, w in zip(s["totals"]["carbon"], want):
            self.assertAlmostEqual(g, w, places=9)
        self.assertAlmostEqual(sum(sum(r["carbon"]) for r in s["by_session"]), sum(s["totals"]["carbon"]), places=9)
        self.assertEqual({r["id"]: r["title"] for r in s["by_session"]}["claude:a"], "First session")

    def test_live_updates_replace_records(self):
        view = View(self.store, "year:2026")
        before = view.summary.to_json(CFG, {})["totals"]["carbon"][0]
        self.store.upsert("claude", "claude:a", [_rec("early", "claude:a", local_iso(2026, 10, 5, 0, 10), 2000)])
        self.store.commit()
        changed = view.poll()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0][1]["output"], 20)  # previous version, for the ticker delta
        after = view.summary.to_json(CFG, {})
        self.assertEqual(after["n"], 3)
        self.assertGreater(after["totals"]["carbon"][0], before)
        self.assertGreater(Summary.delta(changed, CFG)["carbon"][0], 0)

    def test_session_view_has_series(self):
        s = View(self.store, "claude:a").snapshot(CFG)["summary"]
        self.assertEqual(len(s["series"]), 2)
        self.assertLessEqual(s["series"][0][1], s["series"][1][1])
        self.assertEqual(View(self.store, "a").session["id"], "claude:a")  # native id works too

    def test_unknown_session(self):
        with self.assertRaises(LookupError):
            View(self.store, "nope")


if __name__ == "__main__":
    unittest.main()
