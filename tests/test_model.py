import unittest

from ecologits.electricity_mix_repository import electricity_mixes
from ecologits.impacts.llm import compute_llm_impacts
from ecologits.model_repository import ParametersMoE, models
from ecologits.tracers.utils import llm_impacts
from ecologits.utils.range_value import RangeValue

from agent_footprint import config, model

CFG = config.load_config("/nonexistent")
ECOLOGITS_ONLY = dict(CFG, prefill_weight={"input": 0, "cache_creation": 0, "cache_read": 0})


def lohi(v):
    return (v.min, v.max) if isinstance(v, RangeValue) else (v, v)


class ModelTest(unittest.TestCase):
    def test_matches_ecologits_reference(self):
        # Compared live against EcoLogits, so its data updates don't break the test. No provider is
        # passed, so name -> provider resolution is covered too; the model must exist, not be skipped.
        self.assertIsNotNone(models.find_model("anthropic", "claude-opus-5-5"))
        ref = llm_impacts("anthropic", "claude-opus-5-5", 1000, request_latency=1e9)
        opus = model.compute("claude-opus-5-5", {"output": 1000}, ECOLOGITS_ONLY)
        for got, want in ((opus["energy_kwh"], ref.energy.value), (opus["usage"]["wcf"], ref.usage.wcf.value),
                          (opus["embodied"]["gwp"], ref.embodied.gwp.value)):
            for g, w in zip(got, lohi(want)):
                self.assertAlmostEqual(g, w, delta=abs(w) * 1e-9 + 1e-18)

    def test_matches_ecologits_high_level_api_for_every_provider(self):
        # With default settings, model.compute must equal EcoLogits' own llm_impacts().
        for provider, name in (("anthropic", "claude-haiku-4-5-20251001"), ("openai", "gpt-6.1-sol"),
                               ("google_genai", "gemini-3.7-flash"), ("mistralai", "mistral-large-latest")):
            if models.find_model(provider, name) is None:
                continue
            for tokens in (1, 537, 20000):
                ref = llm_impacts(provider, name, tokens, request_latency=1e9)
                ours = model.compute(name, {"output": tokens}, ECOLOGITS_ONLY, provider)
                for got, want in ((ours["energy_kwh"], ref.energy.value), (ours["usage"]["gwp"], ref.usage.gwp.value),
                                  (ours["usage"]["wcf"], ref.usage.wcf.value), (ours["embodied"]["pe"], ref.embodied.pe.value)):
                    for g, w in zip(got, lohi(want)):
                        self.assertAlmostEqual(g, w, delta=abs(w) * 1e-9 + 1e-18, msg="%s %s %d" % (provider, name, tokens))

    def test_affine_shortcut_matches_direct_calls_with_custom_settings(self):
        cfg = dict(ECOLOGITS_ONLY, batch_size=32.0, hardware_lifetime_years=5.0,
                   override={"electricity_zone": "FRA", "datacenter_pue": (1.1, 1.3), "datacenter_wue": None})
        for entry in [m for m in models.list_models() if m.provider.value in ("anthropic", "openai")][:12]:
            p = entry.architecture.parameters
            active, total = (p.active, p.total) if isinstance(p, ParametersMoE) else (p, p)
            zone, pue, wue = model.datacenter(entry.provider.value, cfg)
            mix = electricity_mixes.find_electricity_mix(zone=zone)
            ref = compute_llm_impacts(
                model_active_parameter_count=active, model_total_parameter_count=total, output_token_count=4321,
                if_electricity_mix_adpe=mix.adpe, if_electricity_mix_pe=mix.pe, if_electricity_mix_gwp=mix.gwp,
                if_electricity_mix_wue=mix.wue, datacenter_pue=RangeValue(min=1.1, max=1.3),
                datacenter_wue=model._ecologits_value(wue),
                tps=entry.deployment.tps if entry.deployment else None, ttft=entry.deployment.ttft if entry.deployment else None,
                batch_size=32.0, server_lifetime=5.0 * model.SECONDS_PER_YEAR)
            ours = model.compute(entry.name, {"output": 4321}, cfg, entry.provider.value)
            for g, w in zip(ours["energy_kwh"], lohi(ref.energy.value)):
                self.assertAlmostEqual(g, w, delta=abs(w) * 1e-9)
            for g, w in zip(ours["embodied"]["gwp"], lohi(ref.embodied.gwp.value)):
                self.assertAlmostEqual(g, w, delta=abs(w) * 1e-9)

    def test_count_sums_requests_exactly(self):
        a = model.compute("claude-opus-5-5", {"output": 100, "cache_read": 5000}, CFG)
        b = model.compute("claude-opus-5-5", {"output": 300, "input": 20}, CFG)
        both = model.compute("claude-opus-5-5", {"output": 400, "cache_read": 5000, "input": 20}, CFG, count=2)
        for i in (0, 1):
            self.assertAlmostEqual(both["energy_kwh"][i], a["energy_kwh"][i] + b["energy_kwh"][i], places=15)
            self.assertAlmostEqual(both["embodied"]["gwp"][i], a["embodied"]["gwp"][i] + b["embodied"]["gwp"][i], places=15)

    def test_low_le_high_and_grows_with_tokens(self):
        small = model.compute("claude-sonnet-5-5", {"output": 100, "cache_read": 10000}, CFG)
        big = model.compute("claude-sonnet-5-5", {"output": 1000, "cache_read": 10000}, CFG)
        for r in (small, big):
            self.assertLessEqual(r["energy_kwh"][0], r["energy_kwh"][1])
            for phase in ("usage", "embodied"):
                for lo, hi in r[phase].values():
                    self.assertLessEqual(lo, hi)
        self.assertGreater(big["energy_kwh"][0], small["energy_kwh"][0])

    def test_prefill_weight_counts_input_tokens(self):
        with_input = model.compute("claude-opus-5-5", {"output": 100, "cache_read": 100000}, CFG)
        without = model.compute("claude-opus-5-5", {"output": 100, "cache_read": 100000}, ECOLOGITS_ONLY)
        self.assertGreater(with_input["energy_kwh"][0], without["energy_kwh"][0])
        self.assertAlmostEqual(sum(with_input["eff_share"].values()), 1.0)

    def test_provider_defaults_and_overrides(self):
        self.assertEqual(model.datacenter("mistralai", CFG)[0], "SWE")
        self.assertEqual(model.datacenter("openai", CFG)[1], (1.2, 1.2))
        cfg = dict(CFG, providers={"openai": {"electricity_zone": "FRA", "datacenter_pue": (1.1, 1.1), "datacenter_wue": None}})
        self.assertEqual(model.datacenter("openai", cfg)[:2], ("FRA", (1.1, 1.1)))
        self.assertEqual(model.datacenter("anthropic", cfg)[0], "USA")

    def test_resolve_model(self):
        name = lambda m, p=None: model.resolve_model(m, p)[0].name
        self.assertEqual(name("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(name("us.anthropic.claude-sonnet-5-5-v1:0", "amazon-bedrock"), "claude-sonnet-5-5")
        self.assertEqual(name("anthropic/claude-sonnet-5.5", "openrouter"), "claude-sonnet-5-5")
        self.assertEqual(model.resolve_model("gemini-3.7-flash")[0].provider.value, "google_genai")
        entry, warning = model.resolve_model("claude-opus-99")
        self.assertEqual(entry.name, "claude-opus-5-5")  # newest of the family
        self.assertIsNotNone(warning)
        self.assertIn("pro", name("gemini-9-pro"))
        self.assertIsNotNone(model.resolve_model("totally-unknown")[1])

    def test_says_where_assumptions_may_not_fit(self):
        self.assertIsNone(model.resolve_model("claude-opus-5-5", "anthropic")[1])
        self.assertEqual(model.resolve_model("", "openai")[0].name, "gpt-5")
        self.assertIn("no model logged", model.resolve_model("", "openai")[1])
        self.assertIn("Amazon Bedrock", model.resolve_model("us.anthropic.claude-opus-5-5-v1:0", "anthropic")[1])
        entry, warning = model.resolve_model("claude-opus-5-5@20260101", "anthropic")
        self.assertEqual((entry.name, "Vertex" in warning), ("claude-opus-5-5", True))
        self.assertIn("ran locally (ollama)", model.resolve_model("gemma-4-31b-it", "ollama")[1])

    def test_unknown_models_keep_provider_and_size(self):
        self.assertEqual(model.resolve_model("mai-code-1.1-flash")[0].provider.value, "openai")  # Microsoft, on Azure
        self.assertIn("mini", model.resolve_model("mai-code-1.1-flash")[0].name)
        self.assertIn("sonnet", model.resolve_model("totally-unknown")[0].name)


if __name__ == "__main__":
    unittest.main()
