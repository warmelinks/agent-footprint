"""User configuration: the packaged defaults overlaid with ~/.config/agent-footprint/config.json."""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = os.path.join(HERE, "default_config.json")
TOKEN_KINDS = ("output", "input", "cache_creation", "cache_read")


def user_path():
    if os.environ.get("AGENT_FOOTPRINT_CONFIG"):
        return os.path.expanduser(os.environ["AGENT_FOOTPRINT_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser(os.path.join("~", ".config"))
    return os.path.join(base, "agent-footprint", "config.json")


def cache_dir():
    """Files that can be recreated at any time (pid files, logs, update checks)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser(os.path.join("~", ".cache"))
    return os.path.join(base, "agent-footprint")


def data_dir():
    """Files that can't be recreated: the usage history outlives the agents' own logs."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(os.path.join("~", ".local", "share"))
    return os.path.join(base, "agent-footprint")


def ensure_user_config():
    """Create the user config from the defaults if it doesn't exist yet; returns its path."""
    path = user_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(DEFAULTS, path)
    return path


def set_user_value(key, value):
    """Set one top-level key in the user config (creating it from the defaults if needed)."""
    path = ensure_user_config()
    try:
        data = _load(path)
    except ValueError as e:
        raise ValueError("%s isn't valid JSON (%s); set \"%s\" there by hand" % (path, e, key))
    data[key] = value
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def mtime(path=None):
    try:
        return os.path.getmtime(path or user_path())
    except OSError:
        return 0


def _load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def pair(value):
    """None, a number or [low, high] -> None or (low, high)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return float(value[0]), float(value[1])
    return float(value), float(value)


def _countries():
    try:
        return set(_load(os.path.join(HERE, "data", "countries.json"))["countries"])
    except (OSError, ValueError, KeyError):
        return None


def _check(user, defaults, problems):
    """Note settings that are misspelled or can't be used; they fall back to the defaults."""
    for k in sorted(user):
        if not k.startswith("_") and k not in defaults:
            problems.append("unknown setting \"%s\" (ignored)" % k)
    for section in ("sources", "launch", "prefill_weight"):
        for k in sorted(user.get(section) or {}):
            if not k.startswith("_") and k not in defaults[section]:
                problems.append("unknown setting \"%s.%s\" (ignored)" % (section, k))
    code = user.get("compare_country")
    known = _countries()
    if code and known is not None and str(code).upper() not in known:
        problems.append("compare_country \"%s\" isn't a known ISO 3166 alpha-3 code (ignored)" % code)
    terminal = (user.get("launch") or {}).get("terminal")
    if terminal not in (None, "auto", "none"):
        from .launch import TERMINALS
        if terminal not in TERMINALS:
            problems.append("launch.terminal \"%s\" isn't one of: auto, none, %s" % (terminal, ", ".join(TERMINALS)))


def load_config(path=None):
    """The effective settings. cfg["problems"] lists anything in the user config that couldn't be
    used (invalid JSON, unknown keys, values of the wrong type); those parts use the defaults."""
    defaults = _load(DEFAULTS)
    path = path or user_path()
    user, problems = {}, []
    if os.path.exists(path):
        try:
            user = _load(path)
            if not isinstance(user, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:  # e.g. a half-saved edit
            user = {}
            problems.append("%s can't be read (%s); using the defaults until it is fixed" % (path, e))
    if user:
        _check(user, defaults, problems)

    def get(k, kind=None):
        if user.get(k) is None:
            return defaults.get(k)
        if kind is None:
            return user[k]
        try:
            return kind(user[k])
        except (TypeError, ValueError):
            problems.append("\"%s\": %r isn't a %s; using %r" % (k, user[k], "number" if kind is not bool else "true/false", defaults.get(k)))
            return defaults.get(k)

    def number_or_pair(d, k, where):
        try:
            return pair(d.get(k))
        except (TypeError, ValueError, IndexError):
            problems.append("\"%s%s\": %r isn't a number or [low, high] (ignored)" % (where, k, d.get(k)))
            return None

    def datacenter(d, where=""):
        return {"electricity_zone": d.get("electricity_zone"),
                "datacenter_pue": number_or_pair(d, "datacenter_pue", where),
                "datacenter_wue": number_or_pair(d, "datacenter_wue", where)}

    pw = dict(defaults["prefill_weight"])
    for k, v in (user.get("prefill_weight") or {}).items():
        if k in pw:
            try:
                pw[k] = float(v)
            except (TypeError, ValueError):
                problems.append("\"prefill_weight.%s\": %r isn't a number; using %r" % (k, v, pw[k]))
    sources = {}
    for name, d in defaults["sources"].items():
        sources[name] = dict(d, **((user.get("sources") or {}).get(name) or {}))
    return {
        "override": datacenter(user),
        "providers": {p: datacenter(d, "providers.%s." % p) for p, d in (user.get("providers") or {}).items()
                      if isinstance(d, dict)},
        "batch_size": float(get("batch_size", float)),
        "hardware_lifetime_years": float(get("hardware_lifetime_years", float)),
        "prefill_weight": {k: float(pw.get(k, 0.0)) for k in TOKEN_KINDS if k != "output"},
        "sources": sources,
        "launch": dict(defaults["launch"], **(user.get("launch") or {})),
        "compare_country": (get("compare_country") or None),
        "store_titles": get("store_titles") is True,
        "auto_update_check": get("auto_update_check") is not False,
        "port": int(get("port", int)),
        "problems": problems,
    }
