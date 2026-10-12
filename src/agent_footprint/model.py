"""Life-cycle impacts of one LLM request, computed with the EcoLogits package.

EcoLogits supplies the methodology, model size estimates, provider datacenter defaults and
electricity mixes. This module adds two things on top:

- model-id resolution for the ids agents write to their logs (Bedrock/OpenRouter prefixes,
  dated snapshots, unknown future versions fall back to the closest known model);
- one documented extension: input tokens count as weighted "output-token equivalents".

Per request, every EcoLogits impact is affine in the token count (latency = tokens / tps + ttft,
and energy, water and embodied impacts are linear in latency and tokens). So for each model and
setting we evaluate EcoLogits twice, keep the slope and intercept, and work out each request with
plain arithmetic; a year of history stays fast. tests/ checks this against direct calls.

Units follow EcoLogits: energy kWh, gwp kgCO2eq, adpe kgSbeq, pe MJ, wcf (water) L.
Every impact is a [low, high] pair.
"""
import json
import os
import re
from functools import lru_cache

from ecologits.electricity_mix_repository import electricity_mixes
from ecologits.impacts.llm import compute_llm_impacts
from ecologits.model_repository import ParametersMoE, models as _repo
from ecologits.tracers.utils import PROVIDER_CONFIG_MAP
from ecologits.utils.range_value import RangeValue

from .config import TOKEN_KINDS, load_config  # noqa: F401  (load_config re-exported for callers)

HERE = os.path.dirname(os.path.abspath(__file__))
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
PHASES = {"usage": ("gwp", "adpe", "pe", "wcf"), "embodied": ("gwp", "adpe", "pe")}
_PROBE = 1000.0  # second evaluation point for the affine fit

with open(os.path.join(HERE, "factors.json"), encoding="utf-8") as f:
    FACTORS = json.load(f)
with open(os.path.join(HERE, "data", "countries.json"), encoding="utf-8") as f:
    COUNTRIES = json.load(f)

PROVIDERS = tuple(PROVIDER_CONFIG_MAP)
_BY_PROVIDER = {}
for _m in _repo.list_models():
    _BY_PROVIDER.setdefault(_m.provider.value, []).append(_m)

# Provider names agents use -> EcoLogits provider. None: guess from the model name.
PROVIDER_ALIASES = {
    "anthropic": "anthropic", "openai": "openai", "azure": "openai", "google": "google_genai",
    "google_genai": "google_genai", "gemini": "google_genai", "google-vertex": "google_genai",
    "vertex": "google_genai", "mistral": "mistralai", "mistralai": "mistralai", "cohere": "cohere",
    "huggingface": "huggingface_hub", "huggingface_hub": "huggingface_hub",
}
_NAME_HINTS = (
    (r"claude|fable", "anthropic"), (r"^(gpt|o\d|codex|chatgpt)", "openai"),
    (r"gemini|gemma", "google_genai"), (r"mistral|codestral|devstral|magistral|ministral", "mistralai"),
    (r"^command", "cohere"),
    (r"^mai-", "openai"),  # Microsoft's own models (e.g. Copilot's "Auto"), served from Azure like OpenAI's
)


# Used when an agent's log has no model for a request.
DEFAULT_MODEL = {"openai": "gpt-5", "google_genai": "gemini-flash-latest"}
# Providers that run the model on your own machine: still estimated as if served from a datacenter.
LOCAL_PROVIDERS = {"ollama", "lmstudio", "llama.cpp", "llamacpp"}


def guess_provider(model_id, hint=None):
    """EcoLogits provider for a model id, from the agent's provider name or else the model name."""
    p = PROVIDER_ALIASES.get((hint or "").lower())
    if p:
        return p
    name = (model_id or "").lower().split("/")[-1]
    for pattern, provider in _NAME_HINTS:
        if re.search(pattern, name):
            return provider
    return "huggingface_hub" if "/" in (model_id or "") else "anthropic"


def _variants(name):
    """Spellings to try, most specific first: as given, without a date stamp, dots as dashes."""
    out = [name]
    undated = re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2})$", "", name)
    for n in (undated, name.replace(".", "-"), undated.replace(".", "-")):
        if n not in out:
            out.append(n)
    return out


def _common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


@lru_cache(maxsize=None)
def resolve_model(model_id, provider=None):
    """Map a logged model id to an EcoLogits model entry.

    Returns (entry, warning-or-None). Unknown ids fall back to the known model of the same
    provider sharing the longest name prefix (the newest one on ties), e.g. a future
    'claude-opus-9' to the latest Opus. The warning also says when the model ran somewhere
    the datacenter defaults don't describe (a cloud platform, or locally).
    """
    entry, warning = _resolve(model_id, provider)
    notes = [warning] if warning else []
    raw = (model_id or "").strip()
    if re.match(r"^(?:[a-z]{2}\.)?anthropic\.", raw) or re.search(r"-v\d+:\d+$", raw):
        notes.append("'%s' ran on Amazon Bedrock; estimated with EcoLogits' anthropic datacenter defaults" % raw)
    elif re.search(r"@\d{8}$", raw):
        notes.append("'%s' ran on Google Vertex AI; estimated with EcoLogits' %s datacenter defaults"
                     % (raw, entry.provider.value))
    if (provider or "").lower() in LOCAL_PROVIDERS:
        notes.append("'%s' ran locally (%s); estimated as if served from a datacenter" % (raw, provider))
    return entry, "; ".join(notes) or None


def _resolve(model_id, provider):
    raw = (model_id or "").strip()
    name = re.sub(r"\[.*\]$", "", raw)                        # claude-opus-5-5[1m]
    name = re.sub(r"^(?:[a-z]{2}\.)?anthropic\.", "", name)    # Bedrock: us.anthropic.claude-...
    name = re.sub(r"-v\d+(?::\d+)?$", "", name)                # Bedrock: ...-v1:0
    name = re.sub(r"@\d{8}$", "", name)                         # Vertex: claude-...@20250514
    if "/" in name and provider not in ("huggingface_hub",):
        vendor, rest = name.split("/", 1)                      # OpenRouter: anthropic/claude-...
        if PROVIDER_ALIASES.get(vendor.lower()):
            provider, name = PROVIDER_ALIASES[vendor.lower()], rest
    known = provider in _BY_PROVIDER or PROVIDER_ALIASES.get((provider or "").lower())
    provider = provider if provider in _BY_PROVIDER else guess_provider(name, provider)
    if not name:
        pool = _BY_PROVIDER.get(provider) or _BY_PROVIDER["anthropic"]
        entry = _repo.find_model(provider, DEFAULT_MODEL.get(provider, "")) or max(pool, key=lambda m: _version(m.name))
        return entry, "no model logged for some %s requests; estimated as %s" % (provider, entry.name)
    for p in [provider] + [q for q in _BY_PROVIDER if q != provider]:
        for n in _variants(name):
            entry = _repo.find_model(p, n)
            if entry is not None:
                return entry, None
    # Closest known model: longest shared name prefix (case-insensitive, ignoring an org/ prefix),
    # then most shared name words (pro, mini, ...), then newest version. Unknown providers (e.g. a local Ollama model) search every provider.
    pool = _BY_PROVIDER.get(provider) if known or provider != "anthropic" else None
    pool = pool or _repo.list_models()
    want = name.lower().replace(".", "-").split("/")[-1]
    words = _words(want)
    score = lambda m: (_common_prefix(m.name.lower().replace(".", "-").split("/")[-1], want),
                       len(words & _words(m.name)), _version(m.name))
    best = max(pool, key=score)
    if score(best)[0] >= min(4, len(want)):
        return best, "'%s' not in EcoLogits data; using %s" % (raw, best.name)
    # Nothing close: the newest model of the provider (known, or guessed from the name), of the same
    # size class when the name has one ("flash", "mini", ...); with no clue at all, the newest Sonnet.
    if known or provider != "anthropic" or words & _SMALL:
        pool = _BY_PROVIDER.get(provider) or _BY_PROVIDER["anthropic"]
    else:
        pool = [m for m in _BY_PROVIDER["anthropic"] if "sonnet" in m.name]
    if words & _SMALL:
        pool = [m for m in pool if _words(m.name) & _SMALL] or pool
    fallback = max(pool, key=lambda m: _version(m.name))
    return fallback, "'%s' not recognised; using %s" % (raw, fallback.name)


_SMALL = {"flash", "lite", "mini", "nano", "small", "haiku", "tiny"}  # names of small, fast models


def _words(name):
    """Non-numeric name parts: 'gemini-3.8-pro' -> {'gemini', 'pro'}."""
    return {w for w in re.split(r"[-_/.]", name.lower().split("/")[-1]) if w and not w.isdigit()}


def _version(name):
    """Version digits in a model name, for 'newest' tie-breaks (claude-opus-5-5 > claude-opus-4-5).
    Multi-digit numbers are dates, sizes (70b) or old Azure names (gpt-35), not versions."""
    return tuple(int(x) for x in re.findall(r"(?<![\d])\d(?![\d])", name))


def _lohi(v):
    return (float(v.min), float(v.max)) if isinstance(v, RangeValue) else (float(v), float(v))


def _ecologits_value(p):
    """(low, high) -> what EcoLogits accepts: a number or a RangeValue."""
    return p[0] if p[0] == p[1] else RangeValue(min=p[0], max=p[1])


def datacenter(provider, cfg):
    """(electricity zone, PUE pair, WUE pair) for a provider: user override, else EcoLogits default."""
    d = PROVIDER_CONFIG_MAP.get(provider) or PROVIDER_CONFIG_MAP["anthropic"]
    own = cfg["providers"].get(provider) or {}
    pick = lambda k, default: own.get(k) or cfg["override"].get(k) or default
    zone = pick("electricity_zone", d.datacenter_location or "WOR")
    if electricity_mixes.find_electricity_mix(zone=zone) is None:
        zone = "WOR"
    return zone, pick("datacenter_pue", _lohi(d.datacenter_pue)), pick("datacenter_wue", _lohi(d.datacenter_wue))


def _flatten(imp):
    out = {"energy": _lohi(imp.energy.value)}
    for phase, kinds in PHASES.items():
        for k in kinds:
            out[phase + "." + k] = _lohi(getattr(getattr(imp, phase), k).value)
    return out


@lru_cache(maxsize=4096)
def _affine(provider, name, zone, pue, wue, batch_size, lifetime_years):
    """{impact: ((slope_lo, slope_hi), (intercept_lo, intercept_hi))} per output-token equivalent."""
    entry = _repo.find_model(provider, name)
    params = entry.architecture.parameters
    if isinstance(params, ParametersMoE):
        active, total = params.active, params.total
    else:
        active = total = params
    mix = electricity_mixes.find_electricity_mix(zone=zone)
    dep = entry.deployment

    def run(tokens):
        return _flatten(compute_llm_impacts(
            model_active_parameter_count=active, model_total_parameter_count=total,
            output_token_count=tokens,
            if_electricity_mix_adpe=mix.adpe, if_electricity_mix_pe=mix.pe,
            if_electricity_mix_gwp=mix.gwp, if_electricity_mix_wue=mix.wue,
            datacenter_pue=_ecologits_value(pue), datacenter_wue=_ecologits_value(wue),
            tps=dep.tps if dep else None, ttft=dep.ttft if dep else None,
            batch_size=batch_size, server_lifetime=lifetime_years * SECONDS_PER_YEAR))

    zero, probe = run(0.0), run(_PROBE)
    return {k: (tuple((probe[k][i] - zero[k][i]) / _PROBE for i in (0, 1)), zero[k]) for k in zero}


def effective_tokens(tokens, cfg):
    """Per-kind output-token equivalents."""
    w = dict(cfg["prefill_weight"], output=1.0)
    return {k: tokens.get(k, 0) * w[k] for k in TOKEN_KINDS}


def compute(model_id, tokens, cfg, provider=None, count=1):
    """Impacts of one request, or of `count` requests whose token counts are summed in `tokens`
    (exact, since each request's impact is affine in its tokens).

    tokens: {output, input, cache_creation, cache_read} counts; provider: the agent's provider
    name if it logs one (else guessed from the model id). Returns a JSON-ready dict; each impact
    value is [low, high].
    """
    entry, warning = resolve_model(model_id, provider)
    prov = entry.provider.value
    zone, pue, wue = datacenter(prov, cfg)
    fit = _affine(prov, entry.name, zone, tuple(pue), tuple(wue), cfg["batch_size"], cfg["hardware_lifetime_years"])
    eff = effective_tokens(tokens, cfg)
    n = sum(eff.values())
    v = {k: [a[0] * n + b[0] * count, a[1] * n + b[1] * count] for k, (a, b) in fit.items()}
    return {
        "model": model_id,
        "provider": prov,
        "ecologits_model": entry.name,
        "warning": warning,
        "tokens": {k: tokens.get(k, 0) for k in TOKEN_KINDS},
        "eff_share": {k: (eff[k] / n if n else 0) for k in TOKEN_KINDS},
        "energy_kwh": v["energy"],
        "usage": {k: v["usage." + k] for k in PHASES["usage"]},
        "embodied": {k: v["embodied." + k] for k in PHASES["embodied"]},
    }


def _owid_baseline(kind, code, label, where):
    c = COUNTRIES["countries"].get(code) or {}
    v = c.get(kind)
    if not v:
        return None
    src = COUNTRIES["sources"][kind]
    shown = "%.1f kWh" % (v["per_day"] / 1000) if kind == "energy" else "%.1f kg CO2e" % (v["per_day"] / 1000)
    return {"label": label, "long": FACTORS["person_day"]["long"][kind] % where, "size": v["per_day"],
            "source": "%s, %d: %s per person per day (%s; %s)" % (c["name"], v["year"], shown, src["citation"], src["url"])}


def person_day(cfg):
    """{indicator: [baseline]} for the world, the configured country (if any) and the 1.5 °C budget."""
    pd = FACTORS["person_day"]
    code = (cfg.get("compare_country") or "").upper() or None
    country = (COUNTRIES["countries"].get(code) or {}).get("name") if code else None
    out = {}
    for kind in ("energy", "carbon"):
        rows = [_owid_baseline(kind, "WLD", "world avg", "world-average")]
        if country:
            rows.append(_owid_baseline(kind, code, "%s avg" % country, "%s-average" % country))
        out[kind] = [r for r in rows if r]
    out["carbon"].append(dict(pd["carbon_budget"]))
    out["water"] = [dict(pd["water"]["WLD"], label="world avg")]
    if code and code in pd["water"]:
        out["water"].append(dict(pd["water"][code], label="%s avg" % country))
    return out


def describe(cfg):
    """Assumptions summary for the dashboard, per provider."""
    providers = {}
    for p in PROVIDERS:
        zone, pue, wue = datacenter(p, cfg)
        mix = electricity_mixes.find_electricity_mix(zone=zone)
        providers[p] = {"electricity_zone": zone, "datacenter_pue": list(pue), "datacenter_wue": list(wue),
                        "grid": {"gwp": mix.gwp, "adpe": mix.adpe, "pe": mix.pe, "wue": mix.wue}}
    return {
        "providers": providers,
        "batch_size": cfg["batch_size"],
        "hardware_lifetime_years": cfg["hardware_lifetime_years"],
        "prefill_weight": cfg["prefill_weight"],
        "ladders": FACTORS["ladders"],
        "person_day": person_day(cfg),
        "compare_country": cfg.get("compare_country"),
        "ecologits_version": ecologits_version(),
    }


def ecologits_version():
    try:
        from importlib.metadata import version
        return version("ecologits")
    except Exception:
        return "unknown"
