#!/usr/bin/env python3
"""List the hand-maintained figures in factors.json that are due for a review.

    python scripts/stale_factors.py              # Markdown list; exit 1 if anything is due
    python scripts/stale_factors.py --year 2030  # as if it were 2030

A figure is due when as_of + review_years <= this year. Figures with review_years null are
physical quantities (a glass of water, heating a litre of water) and never go stale. The
per-person electricity and greenhouse-gas baselines are not listed: they are refreshed from
Our World in Data by update_factors.py. The yearly workflow opens an issue with this list.
"""
import argparse
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACTORS = os.path.join(ROOT, "src", "agent_footprint", "factors.json")


def entries(f):
    for kind, ladder in f["ladders"].items():
        for s in ladder["steps"]:
            yield "%s: 1 %s = %s" % (kind, s["one"], s["size"]), s
    for code, w in f["person_day"]["water"].items():
        yield "water footprint per person (%s)" % code, w
    yield "1.5 °C budget per person", f["person_day"]["carbon_budget"]


def due(f, year):
    out = []
    for name, e in entries(f):
        if e.get("review_years") is None:
            continue
        if e.get("as_of") is None or e["as_of"] + e["review_years"] <= year:
            out.append((name, e))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--year", type=int, default=date.today().year)
    args = p.parse_args()
    with open(FACTORS, encoding="utf-8") as fh:
        f = json.load(fh)
    items = due(f, args.year)
    if not items:
        print("No figures due for review in %d." % args.year)
        return
    print("These figures in `src/agent_footprint/factors.json` are due for a review (%d). For each: check the "
          "source for a newer edition, update `size`, `source` and `as_of`, or bump `as_of` if it still holds.\n"
          % args.year)
    for name, e in items:
        print("- [ ] **%s** (as of %s, review every %s years): %s" % (name, e.get("as_of"), e["review_years"], e["source"]))
    sys.exit(1)


if __name__ == "__main__":
    main()
