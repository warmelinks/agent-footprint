import importlib.util
import os
import unittest
from datetime import date
from unittest import mock

from agent_footprint import config, model, updates
from agent_footprint.store import Store
from agent_footprint.views import View

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = config.load_config("/nonexistent")


def script(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BaselineTest(unittest.TestCase):
    def test_world_only_by_default(self):
        pd = model.person_day(CFG)
        self.assertEqual([b["label"] for b in pd["energy"]], ["world avg"])
        self.assertEqual([b["label"] for b in pd["carbon"]], ["world avg", "1.5 °C budget"])
        self.assertEqual([b["label"] for b in pd["water"]], ["world avg"])
        self.assertIn("ourworldindata.org", pd["energy"][0]["source"])

    def test_compare_country(self):
        pd = model.person_day(dict(CFG, compare_country="nld"))
        self.assertEqual([b["label"] for b in pd["energy"]], ["world avg", "Netherlands avg"])
        self.assertEqual(len(pd["water"]), 2)  # a sourced Dutch water footprint exists
        de = model.person_day(dict(CFG, compare_country="DEU"))
        self.assertEqual(len(de["energy"]), 2)
        self.assertEqual(len(de["water"]), 1)  # no German water footprint on file: world only
        self.assertEqual(len(model.person_day(dict(CFG, compare_country="XXX"))["energy"]), 1)

    def test_generated_data_is_plausible(self):
        c = model.COUNTRIES["countries"]
        self.assertGreater(len(c), 150)
        world = c["WLD"]
        self.assertTrue(5000 < world["energy"]["per_day"] < 20000)    # Wh per person per day
        self.assertTrue(10000 < world["carbon"]["per_day"] < 30000)   # g CO2e per person per day
        self.assertGreaterEqual(world["energy"]["year"], date.today().year - 3)


class ReviewTest(unittest.TestCase):
    def test_every_figure_has_review_metadata(self):
        stale = script("stale_factors")
        for name, e in stale.entries(model.FACTORS):
            self.assertIn("review_years", e, name)
            if e["review_years"] is not None:
                self.assertIsInstance(e.get("as_of"), int, name)

    def test_due(self):
        stale = script("stale_factors")
        f = {"ladders": {"carbon": {"steps": [
                {"one": "km driven", "size": 170, "source": "x", "as_of": 2024, "review_years": 1},
                {"one": "glass", "size": 0.25, "source": "y", "as_of": None, "review_years": None}]}},
             "person_day": {"water": {}, "carbon_budget": {"source": "z", "as_of": 2019, "review_years": 10}}}
        self.assertEqual([n for n, _ in stale.due(f, 2024)], [])
        self.assertEqual([n for n, _ in stale.due(f, 2025)], ["carbon: 1 km driven = 170"])
        self.assertEqual(len(stale.due(f, 2029)), 2)

    def test_update_summary_flags_big_changes(self):
        upd = script("update_factors")
        old = {"countries": {"WLD": {"name": "World", "energy": {"per_day": 10000, "year": 2024}}}}
        new = {"generated": "2026-10-10", "countries": {"WLD": {"name": "World", "energy": {"per_day": 13000, "year": 2025}}}}
        text, big = upd.summary(old, new)
        self.assertTrue(big)
        self.assertIn("+30.0%", text)


class UpdateCommandTest(unittest.TestCase):
    def test_commands_per_install_type(self):
        with mock.patch.object(updates, "editable", return_value=False):
            with mock.patch("sys.prefix", "/home/u/.local/share/uv/tools/agent-footprint"):
                self.assertEqual(updates.upgrade_commands(), [["uv", "tool", "upgrade", "agent-footprint"]])
            with mock.patch("sys.prefix", "/home/u/.local/pipx/venvs/agent-footprint"):
                self.assertEqual(updates.upgrade_commands()[0], ["pipx", "upgrade", "agent-footprint"])
            with mock.patch("sys.prefix", "/srv/venv"):
                self.assertIn("agent-footprint", updates.upgrade_commands()[0])
        with mock.patch.object(updates, "editable", return_value=True), mock.patch("sys.prefix", "/srv/venv"):
            self.assertNotIn("agent-footprint", updates.upgrade_commands()[0])  # source checkout: git pull instead

    def test_check_mentions_both(self):
        with mock.patch.object(updates, "latest", return_value={"ecologits": "0.99.0", "agent-footprint": "9.0.0"}), \
                mock.patch.object(updates, "_version", side_effect=lambda d: {"ecologits": "0.11.2", "agent-footprint": "0.1.0"}[d]), \
                mock.patch.object(updates, "editable", return_value=False):
            notice = updates.check()
        self.assertIn("EcoLogits 0.11.2 → 0.99.0", notice)
        self.assertIn("agent-footprint 0.1.0 → 9.0.0", notice)


class EcologitsOnlyTest(unittest.TestCase):
    def test_extension_effect_is_reported(self):
        store = Store(":memory:")
        store.upsert("claude", "claude:a", [{"id": "r", "ts": "2026-10-05T10:00:00Z", "model": "claude-opus-5-5",
                                             "tokens": {"output": 500, "input": 0, "cache_creation": 0, "cache_read": 200000}}])
        store.commit()
        s = View(store, "year:2026").snapshot(CFG)["summary"]
        plain = model.compute("claude-opus-5-5", {"output": 500}, dict(CFG, prefill_weight={"input": 0, "cache_creation": 0, "cache_read": 0}))
        self.assertAlmostEqual(s["ecologits_only"]["energy"][0], plain["energy_kwh"][0] * 1000, places=9)
        self.assertGreater(s["totals"]["carbon"][0], s["ecologits_only"]["carbon"][0])


if __name__ == "__main__":
    unittest.main()
