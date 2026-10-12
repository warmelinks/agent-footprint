"""Keeping the figures current.

Two things carry figures: the ecologits package (models, datacenters, electricity grids) and
agent-footprint itself (per-person baselines and everyday comparisons, refreshed in the
repository and shipped in releases). check() asks PyPI, at most once a day and cached,
whether a newer release of either is available; `agent-footprint update` upgrades both in this
tool's own environment. Nothing is installed without the user running that command.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .config import cache_dir

PYPI = "https://pypi.org/pypi/%s/json"
CHECK_EVERY_S = 24 * 3600
UPPER = {"ecologits": (1,)}  # matches "ecologits>=0.11,<1" in pyproject.toml
SELF = "agent-footprint"


def _version(dist):
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return None


def own_version():
    return _version(SELF) or "dev"


def installed():
    return _version("ecologits")


def editable():
    """True when running from a source checkout (`uv tool install --editable .`, `pip install -e .`)."""
    try:
        from importlib.metadata import distribution
        url = json.loads(distribution(SELF).read_text("direct_url.json") or "{}")
        return bool(url.get("dir_info", {}).get("editable"))
    except Exception:
        return False


def _parse(v):
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def _newest_on_pypi(dist, timeout):
    try:
        with urllib.request.urlopen(PYPI % dist, timeout=timeout) as r:
            releases = json.load(r)["releases"]
    except urllib.error.HTTPError as e:
        if e.code == 404:  # not published (yet)
            return None
        raise
    upper = UPPER.get(dist)
    ok = [v for v, files in releases.items()
          if files and v.replace(".", "").isdigit() and (upper is None or _parse(v) < upper)]
    return max(ok, key=_parse) if ok else None


def latest(timeout=5):
    """{dist: newest stable version on PyPI (within the supported range)}, cached for a day."""
    path = os.path.join(cache_dir(), "update-check.json")
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - cached["at"] < CHECK_EVERY_S and isinstance(cached.get("latest"), dict):
            return cached["latest"]
    except (OSError, ValueError, KeyError):
        pass
    found = {dist: _newest_on_pypi(dist, timeout) for dist in ("ecologits", SELF)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "latest": found}, f)
    except OSError:
        pass
    return found


def check():
    """A one-line notice if newer figures are available, else None. Never raises."""
    try:
        new = latest()
    except Exception:
        return None
    parts = []
    have = installed()
    if have and new.get("ecologits") and _parse(new["ecologits"]) > _parse(have):
        parts.append("EcoLogits %s → %s (models, datacenters, grids)" % (have, new["ecologits"]))
    mine = _version(SELF)
    if mine and new.get(SELF) and _parse(new[SELF]) > _parse(mine) and not editable():
        parts.append("agent-footprint %s → %s (baselines, comparisons)" % (mine, new[SELF]))
    return ("Newer figures available: %s. Run `agent-footprint update`." % "; ".join(parts)) if parts else None


def upgrade_commands():
    """How to upgrade agent-footprint and ecologits for this installation, as a list of argv."""
    prefix = sys.prefix.replace("\\", "/")
    if editable():  # a source checkout: the code comes from git; only refresh the dependency
        if "/uv/tools/" in prefix:
            return [["uv", "tool", "upgrade", SELF]]
        return [[sys.executable, "-m", "pip", "install", "--upgrade", "ecologits<1"]]
    if "/uv/tools/" in prefix:
        return [["uv", "tool", "upgrade", SELF]]  # upgrades the package and its dependencies
    if "/pipx/venvs/" in prefix:
        return [["pipx", "upgrade", SELF], ["pipx", "runpip", SELF, "install", "--upgrade", "ecologits<1"]]
    return [[sys.executable, "-m", "pip", "install", "--upgrade", SELF, "ecologits<1"]]


def update():
    for cmd in upgrade_commands():
        print("Running: %s" % " ".join(cmd))
        code = subprocess.call(cmd)
        if code:
            return code
    try:
        os.remove(os.path.join(cache_dir(), "update-check.json"))
    except OSError:
        pass
    if editable():
        print("This is a source checkout: `git pull` in it for new baselines and comparisons.")
    print("Done. Restart `agent-footprint serve` / `watch` to use the new figures.")
    return 0
