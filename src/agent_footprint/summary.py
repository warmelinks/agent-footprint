"""Aggregate usage records into what the views show: totals, top sessions/models/tools,
histogram buckets and (for a single session) the cumulative carbon curve.

Records are grouped by (session, model, provider, tool, source, bucket) with their token
counts summed; impacts are then computed once per group. That is exact because a request's
impact is affine in its tokens (see model.py), and it keeps a year of history cheap.
"""
from . import model
from .config import TOKEN_KINDS

KINDS = ("energy", "water", "carbon")
TOP_SESSIONS = 200
SERIES_POINTS = 1000


def display(imp):
    """Model impacts -> {energy Wh, water L, carbon g}, each [low, high]."""
    return {
        "energy": [x * 1000 for x in imp["energy_kwh"]],
        "water": list(imp["usage"]["wcf"]),
        "carbon": [(u + e) * 1000 for u, e in zip(imp["usage"]["gwp"], imp["embodied"]["gwp"])],
    }


def _add(a, b, sign=1):
    return [x + sign * y for x, y in zip(a, b)]


def _blank():
    return {"n": 0, "energy": [0.0, 0.0], "water": [0.0, 0.0], "carbon": [0.0, 0.0]}


def _acc(table, key, d, n, **extra):
    row = table.get(key)
    if row is None:
        row = table[key] = dict(_blank(), **extra)
    row["n"] += n
    for k in KINDS:
        row[k] = _add(row[k], d[k])
    return row


class Summary:
    """Incremental: apply() records as they arrive (a record seen again replaces its old version)."""

    def __init__(self, period=None, keep_series=False):
        self.period = period
        self.keep_series = keep_series
        self.groups = {}   # key -> [requests, output, input, cache_creation, cache_read]
        self.seen = {}     # (tool, id) -> (key, token tuple, epoch, model, provider, requests)
        self.first = {}    # session -> earliest epoch

    def apply(self, records):
        """Add records; returns [(record, previous token dict or None)] for the ones that changed."""
        changed = []
        for r in records:
            rid = (r["tool"], r["id"])
            toks = tuple(int(r["tokens"].get(k) or 0) for k in TOKEN_KINDS)
            bucket = self.period.bucket_of(r["epoch"]) if self.period and r.get("epoch") is not None else None
            key = (r["session"], r["model"], r.get("provider"), r["tool"], r.get("source") or "main", bucket)
            n = int(r.get("requests") or 1)
            prev = self.seen.get(rid)
            if prev and prev[0] == key and prev[1] == toks and prev[5] == n:
                continue
            if prev:
                g = self.groups[prev[0]]
                g[0] -= prev[5]
                for i, t in enumerate(prev[1]):
                    g[i + 1] -= t
                if g[0] <= 0:
                    del self.groups[prev[0]]
            g = self.groups.setdefault(key, [0, 0, 0, 0, 0])
            g[0] += n
            for i, t in enumerate(toks):
                g[i + 1] += t
            self.seen[rid] = (key, toks, r.get("epoch"), r["model"], r.get("provider"), n)
            if r.get("epoch") is not None:
                s = r["session"]
                self.first[s] = min(self.first.get(s, r["epoch"]), r["epoch"])
            changed.append((r, dict(zip(TOKEN_KINDS, prev[1])) if prev else None))
        return changed

    @staticmethod
    def delta(changed, cfg):
        """Impact added by the latest changes (for the 'latest response' ticker)."""
        out = dict(_blank(), tokens=0, model="")
        for r, prev in changed:
            d = display(model.compute(r["model"], r["tokens"], cfg, r.get("provider"), int(r.get("requests") or 1)))
            p = display(model.compute(r["model"], prev, cfg, r.get("provider"))) if prev else None
            for k in KINDS:
                out[k] = _add(out[k], d[k] if p is None else _add(d[k], p[k], -1))
            out["tokens"] += sum(r["tokens"].values()) - (sum(prev.values()) if prev else 0)
            out["model"] = r["model"]
            out["n"] += 0 if prev else int(r.get("requests") or 1)
        return out

    def to_json(self, cfg, session_meta):
        """Everything the views render. session_meta: {session id: {title, project, tool}}."""
        tot = _blank()
        tokens_total = 0
        # The same totals without the input-token extension, i.e. EcoLogits' own method.
        cfg0 = dict(cfg, prefill_weight={k: 0.0 for k in cfg["prefill_weight"]})
        plain = {"energy": [0.0, 0.0], "carbon": [0.0, 0.0]}
        phases = {p: {k: [0.0, 0.0] for k in ks} for p, ks in model.PHASES.items()}
        by_token = {k: 0.0 for k in TOKEN_KINDS}
        by_model, by_tool, by_source, by_session, buckets = {}, {}, {}, {}, {}
        warnings = set()
        providers = set()
        for (session, mid, prov, tool, source, bucket), g in self.groups.items():
            count, toks = g[0], dict(zip(TOKEN_KINDS, g[1:]))
            imp = model.compute(mid, toks, cfg, prov, count)
            d = display(imp)
            d0 = display(model.compute(mid, toks, cfg0, prov, count))
            for k in plain:
                plain[k] = _add(plain[k], d0[k])
            if imp["warning"]:
                warnings.add(imp["warning"])
            providers.add(imp["provider"])
            tot["n"] += count
            for k in KINDS:
                tot[k] = _add(tot[k], d[k])
            tokens_total += sum(toks.values())
            for p, ks in model.PHASES.items():
                for k in ks:
                    phases[p][k] = _add(phases[p][k], imp[p][k])
            e_mid = (d["energy"][0] + d["energy"][1]) / 2
            for k in TOKEN_KINDS:
                by_token[k] += e_mid * imp["eff_share"][k]
            row = _acc(by_model, (mid, imp["provider"]), d, count, model=mid, provider=imp["provider"],
                       ecologits_model=imp["ecologits_model"], output=0, tools=set())
            row["output"] += toks["output"]
            row["tools"].add(tool)
            _acc(by_tool, tool, d, count, tool=tool)
            _acc(by_source, source, d, count, source=source)
            srow = _acc(by_session, session, d, count, id=session, tool=tool, models=set())
            srow["models"].add(mid)
            if bucket is not None:
                _acc(buckets, bucket, d, count)

        rank = lambda rows: sorted(rows, key=lambda r: -(r["carbon"][0] + r["carbon"][1]))
        sessions = rank(by_session.values())
        for s in sessions:
            meta = session_meta.get(s["id"]) or {}
            s.update(title=meta.get("title") or "", project=meta.get("project") or "",
                     first=self.first.get(s["id"]), models=sorted(s["models"]))
        models = rank(by_model.values())
        for m in models:
            m["tools"] = sorted(m["tools"])
        out = {
            "n": tot["n"], "tokens": tokens_total,
            "totals": {k: tot[k] for k in KINDS},
            "ecologits_only": plain,
            "phases": phases, "by_token": by_token,
            "by_model": models, "by_tool": rank(by_tool.values()), "by_source": rank(by_source.values()),
            "by_session": sessions[:TOP_SESSIONS], "session_count": len(sessions),
            "warnings": sorted(warnings), "providers": sorted(providers),
        }
        if self.period:
            out["buckets"] = [dict(buckets.get(key) or _blank(), key=key, short=short, long=long)
                              for key, short, long in self.period.buckets()]
            out["current_bucket"] = self.period.current_bucket()
        if self.keep_series:
            out["series"] = self.series(cfg)
        return out

    def series(self, cfg):
        """Cumulative carbon [[epoch, low, high, model], ...], thinned to SERIES_POINTS."""
        rows = sorted((v for v in self.seen.values() if v[2] is not None), key=lambda v: v[2])
        out, lo, hi = [], 0.0, 0.0
        for key, toks, epoch, mid, prov, n in rows:
            c = display(model.compute(mid, dict(zip(TOKEN_KINDS, toks)), cfg, prov, n))["carbon"]
            lo, hi = lo + c[0], hi + c[1]
            out.append([epoch, lo, hi, mid])
        if len(out) > SERIES_POINTS:
            step = len(out) / SERIES_POINTS
            out = [out[min(len(out) - 1, int((i + 1) * step) - 1)] for i in range(SERIES_POINTS)]
        return out
