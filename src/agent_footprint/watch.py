"""Terminal view of your coding agents' footprint. No server needed.

    agent-footprint watch                 # today, all sessions and agents, live
    agent-footprint watch --week          # this week (also --month, --year; optional value, e.g. --month 2026-09)
    agent-footprint watch latest          # follow the newest session
    agent-footprint watch <session-id>    # pin one session
    agent-footprint watch --once          # print a snapshot and exit

Run it in a split pane next to your agent. Keys: ←/→ (or p/n) step through Year, Month, Week,
Today and then the active sessions; ↓/↑ (or [ / ]) go to the previous / next period; y m w d jump to a
period; 1-9 jump to Today (1) or an active session; f follows the newest session; q quits.
"""
import math
import os
import re
import select
import shutil
import signal
import sys
import time

if os.name == "nt":
    import msvcrt
else:
    import termios
    import tty

from . import config, launch, periods, sources
from .store import Store
from .summary import Summary
from .views import View

FRAME_S = 1 / 20       # redraw rate while animating
POLL_S = 0.5           # log poll rate
TWEEN_S = 0.9
ACTIVE_S = 30 * 60     # a session counts as active if it had a response this recently
KINDS = ("energy", "water", "carbon")
LABEL = {"energy": "Electricity", "water": "Water", "carbon": "Carbon"}
COLOR = {"energy": (27, 175, 122), "water": (42, 120, 214), "carbon": (235, 104, 52)}
CARBON_UNITS = [(1e-3, "mg CO₂e"), (1, "g CO₂e"), (1e3, "kg CO₂e"), (1e6, "t CO₂e")]
ENERGY_UNITS = [(1e-3, "mWh"), (1, "Wh"), (1e3, "kWh"), (1e6, "MWh")]
WATER_UNITS = [(1e-3, "mL"), (1, "L"), (1e3, "m³")]
BASE_UNITS = {"energy": ENERGY_UNITS, "water": WATER_UNITS, "carbon": CARBON_UNITS}
SPARK = "▁▂▃▄▅▆▇█"
EIGHTHS = " ▏▎▍▌▋▊▉"
PERIOD_VIEWS = ["year:current", "month:current", "week:current", "day:today"]
HISTO_ROWS = 3


class Style:
    def __init__(self, enabled):
        self.on = enabled

    def _w(self, code, s):
        return "\x1b[%sm%s\x1b[0m" % (code, s) if self.on else s

    def fg(self, rgb, s):
        return self._w("38;2;%d;%d;%d" % rgb, s)

    def bold(self, s):
        return self._w("1", s)

    def dim(self, s):
        return self._w("2", s)

    def inverse(self, s):
        return self._w("7", s)


# ---------- formatting ----------

def fmt(n):
    a = abs(n)
    if a == 0:
        return "0"
    if a < 10:
        return "%.2f" % n
    if a < 100:
        return "%.1f" % n
    return "{:,}".format(int(round(n)))


def fmt_si(v, units):
    pick = units[0]
    for u in units:
        if abs(v) >= u[0]:
            pick = u
    return "%s %s" % (fmt(v / pick[0]), pick[1])


def ladder(steps, base):
    """(level, value text, unit text, amount in that unit)."""
    i = 0
    while i + 1 < len(steps) and base / steps[i + 1]["size"] >= 1:
        i += 1
    s = steps[i]
    n = base / s["size"]
    if s.get("percent") and n < 1:
        value, unit = fmt(n * 100) + "%", "of a " + s["one"]
    else:
        value, unit = fmt(n), s["one"] if abs(n - 1) < 1e-9 else s["many"]
    return i, value, unit, n


def nice_ceil(n):
    """Smallest of 1, 2, 5, 10, 20, 50, ... that is >= n."""
    k = math.floor(math.log10(n))
    for f in (1, 2, 5, 10):
        if f * 10 ** k >= n * (1 - 1e-9):
            return f * 10 ** k
    return 10 ** (k + 1)


def scale(n):
    """Gauge axis for n units: (axis max, segment count, end label).

    Up to one unit the axis is 0-1 in tenths. Above it the axis is the next round number
    and each segment is one unit (or a round multiple of one), so 0.6 and 11.6 look different.
    """
    if n <= 1:
        return 1.0, 10, "1"
    top = nice_ceil(n)
    segs = int(top) if top <= 20 else (20 if str(int(top))[0] == "2" else 10)
    return top, segs, "{:,}".format(int(top))


def block_caption(top, segs, step):
    """What one block stands for, or "" when it is simply one unit."""
    per = top / segs
    if per == 1:
        return ""
    if per < 1:
        return "each block = %d%%" % round(per * 100) if step.get("percent") else \
            "each block = %g %s" % (per, step["one"])
    return "each block = %s %s" % ("{:,}".format(int(per)), step["many"])


def fmt_pct(p):
    """A percentage with at least two significant digits, so tiny shares don't round to 0."""
    if p == 0 or p >= 0.1:
        return fmt(p) + "%"
    return "%.*f%%" % (1 - math.floor(math.log10(p)), p)


def person_day(base, size):
    """base as a share of one person's day: a percentage, or person-days once past 100%."""
    n = base / size
    return fmt_pct(n * 100) if n < 1 else "%s person-day%s" % (fmt(n), "" if abs(n - 1) < 1e-9 else "s")


def person_lines(baselines, base, width):
    """"one person's day — world avg 0.48% · ...", split over lines if it doesn't fit."""
    parts = ["%s %s" % (b["label"], person_day(base, b["size"])) for b in baselines]
    lead = "one person's day — "
    one = lead + " · ".join(parts)
    if len(one) <= width:
        return [one]
    return [lead.rstrip(" —")] + ["  " + x for x in parts]


def gauge(frac, segs, width, st, rgb):
    """(segmented meter, segments drawn): one block per segment, 1/8-cell resolution inside.

    The meter is always exactly `width` columns; leftover columns are spread over the blocks."""
    if segs * 2 - 1 > width and segs % 2 == 0:
        segs //= 2  # not enough room for every block: merge pairs
    edges = [round(i * (width + 1) / segs) for i in range(segs + 1)]
    filled_cells = max(0.0, min(1.0, frac)) * segs
    out = []
    for i in range(segs):
        cell = max(1, edges[i + 1] - edges[i] - 1)
        c = max(0.0, min(1.0, filled_cells - i)) * cell
        full = int(c)
        part = EIGHTHS[int((c - full) * 8)].strip() if full < cell else ""
        fill = "█" * full + part
        out.append(st.fg(rgb, fill) + st.dim("─" * (cell - len(fill))))
    return " ".join(out), segs


def sparkline(values, width):
    if not values:
        return ""
    if len(values) > width:  # resample: keep the last value of each bucket (cumulative series)
        values = [values[min(len(values) - 1, int((i + 1) * len(values) / width) - 1)] for i in range(width)]
    top = values[-1] or 1
    return "".join(SPARK[min(7, int(v / top * 7.999))] for v in values)



def histogram(values, width, rows, st, rgb):
    """Column chart `rows` lines tall, one column per value, 1/8-cell vertical resolution.
    Returns (lines top to bottom, column width, gap)."""
    n = max(1, len(values))
    col = max(1, (width + 1) // n - 1)
    gap = 1 if (col + 1) * n <= width + 1 else 0
    if gap == 0:
        col = max(1, width // n)
    top = max(values) if values else 0
    levels = [round(v / top * rows * 8) if top else 0 for v in values]
    out = []
    for r in range(rows - 1, -1, -1):
        cells = []
        for lv in levels:
            fill = max(0, min(8, lv - r * 8))
            ch = "█" if fill == 8 else (SPARK[fill - 1] if fill else " ")
            cells.append(st.fg(rgb, ch * col) if fill else " " * col)
        out.append((" " * gap).join(cells))
    return out, col, gap


def axis_labels(buckets, col, gap, width, current):
    """One label row under a histogram, skipping labels that would collide."""
    row = [" "] * width
    stride = col + gap
    next_free = 0
    for i, (key, short, _) in enumerate(buckets):
        x = i * stride
        lab = short[:max(1, stride * 3)]
        if x >= next_free and x + len(lab) <= width:
            row[x:x + len(lab)] = lab
            next_free = x + len(lab) + 1
    return "".join(row)


def mid(p):
    return (p[0] + p[1]) / 2


# ---------- data ----------

class Watcher:
    def __init__(self, store, syncer, cfg, wanted):
        self.store, self.syncer, self.cfg = store, syncer, cfg
        self.view = View(store, wanted)
        self.snap = None
        self.active = []         # active sessions, in a stable order so switching doesn't jump around
        self.latest_delta = None
        self.flash_until = 0
        self.message = ("", 0)
        self.levels = {}         # ladder rung last drawn per indicator, to announce step-ups
        self.last_active_check = 0
        self.cfg_mtime = config.mtime()
        self.refresh()

    # ---------- navigation ----------
    def refresh_active(self):
        """Keep known sessions in place, drop ones gone quiet (except the one shown), append new ones."""
        found = {s["id"]: s for s in self.store.sessions(since=time.time() - ACTIVE_S)}
        shown = self.view.session
        if shown and not self.view.period:
            found.setdefault(shown["id"], shown)
        kept = [found.pop(s["id"]) for s in self.active if s["id"] in found]
        self.active = kept + list(found.values())

    def nav(self):
        """Year, Month, Week, Today, then each active session."""
        return PERIOD_VIEWS + [s["id"] for s in self.active]

    def position(self):
        """Index of the shown view in nav(), or None."""
        v = self.view
        if v.period:
            return PERIOD_VIEWS.index(v.period.view_id) if v.period.view_id in PERIOD_VIEWS else None
        ids = [s["id"] for s in self.active]
        return len(PERIOD_VIEWS) + ids.index(v.session["id"]) if v.session and not v.follow and v.session["id"] in ids else None

    def show(self, wanted):
        try:
            self.view = View(self.store, wanted)
        except (LookupError, ValueError) as e:
            self.message = (str(e), time.time() + 4)
            return
        self.levels, self.latest_delta = {}, None
        self.refresh()

    def step(self, delta):
        pos = self.position()
        nav = self.nav()
        if pos is None:  # not in the list: → goes to Today, ← to Week
            pos = PERIOD_VIEWS.index("day:today") - (1 if delta > 0 else -1)
        self.show(nav[(pos + delta) % len(nav)])

    def jump(self, n):
        """1 = Today, 2.. = active sessions."""
        targets = ["day:today"] + [s["id"] for s in self.active]
        if 1 <= n <= len(targets):
            self.show(targets[n - 1])

    def shift(self, delta):
        p = self.view.period
        if not p:
            return
        if delta > 0 and p.is_now():
            return
        n = p.shift(delta)
        self.show(periods.containing(n.kind, n.start, current=True).view_id if n.is_now() else n.id)

    # ---------- data ----------
    def refresh(self):
        self.snap = self.view.snapshot(self.cfg)

    def poll(self):
        """Returns True when the totals changed."""
        now = time.time()
        if config.mtime() != self.cfg_mtime:
            self.cfg_mtime, self.cfg = config.mtime(), config.load_config()
            self.syncer = sources.Syncer(self.store, self.cfg)
            self.syncer.sync()
            self.view.load()
            self.refresh()
            return True
        self.syncer.sync(quick=True)
        if now - self.last_active_check >= 3:
            self.last_active_check = now
            self.refresh_active()
        res = self.view.poll()
        if res == "reset":
            self.levels, self.latest_delta = {}, None
            self.refresh()
            return True
        if not res:
            return False
        self.latest_delta = Summary.delta(res, self.cfg)
        self.flash_until = now + 1.0
        self.refresh()
        return True

    def totals(self):
        return self.snap["summary"]["totals"]


# ---------- drawing ----------

TOOL_SHORT = {"claude": "claude", "codex": "codex", "gemini": "gemini", "opencode": "opencode", "vibe": "vibe",
              "copilot": "copilot"}


def _title_line(w, st, live):
    v, d = w.view, w.snap["view"]
    title = st.bold("Agent footprint")
    status = st.fg((12, 163, 12), "● live") if live else ""
    if v.period:
        mode = d["label"]
    else:
        mode = "following newest" if v.follow else "pinned"
    pos = w.position()
    if live and pos is not None:
        mode += " · %d of %d" % (pos + 1, len(w.nav()))
    return "%s  %s" % (title, status + st.dim("  " + mode))


def render(w, shown, st, width, now, live=True, compact=False):
    ladders = model_factors()["ladders"]
    width = max(44, min(width, 100))
    s, d = w.snap["summary"], w.snap["view"]
    t = s["totals"]
    lines = [_title_line(w, st, live)]
    problems = w.cfg["problems"]
    if problems:
        text = "config: " + problems[0] + (" (+%d more)" % (len(problems) - 1) if len(problems) > 1 else "")
        if len(text) > width:
            text = "config: %d setting%s can't be used; see `agent-footprint info`" % (
                len(problems), "" if len(problems) == 1 else "s")
        lines.append(st.fg(COLOR["carbon"], text))
    if w.view.period:
        n_s = s["session_count"]
        tools = ", ".join(sources.TOOL_LABEL.get(x["tool"], x["tool"]) for x in s["by_tool"]) or "no activity"
        lines.append(st.dim(("%d session%s · %s" % (n_s, "" if n_s == 1 else "s", tools))[:width]))
    elif w.view.session:
        sess = w.view.session
        head = "%s · %s" % (sources.TOOL_LABEL.get(sess["tool"], sess["tool"]), sess.get("title") or sess["id"])
        lines.append(st.dim(head[:width]))
    lines.append("")

    n = s["n"]
    if w.view.period:
        prep = "so far" if d.get("is_now") else "on" if w.view.period.kind == "day" else "in"
        note = "carbon %s %s" % (prep, d["label"])
    else:
        note = "carbon so far"
    lines.append("  " + st.bold(fmt_si(shown["hero"], CARBON_UNITS)) + st.dim("  " + note))
    if n:
        unit = next(u for u in reversed(CARBON_UNITS) if mid(t["carbon"]) >= u[0] or u is CARBON_UNITS[0])
        lines.append(st.dim("  likely %s–%s %s · %s responses" % (
            fmt(t["carbon"][0] / unit[0]), fmt(t["carbon"][1] / unit[0]), unit[1], "{:,}".format(n))))
    else:
        lines.append(st.dim("  no activity in this period" if w.view.period else "  waiting for the first response…"))
    lines.append("")

    flash = now < w.flash_until
    ends = {}
    for k in KINDS:
        level, _, _, n_units = ladder(ladders[k]["steps"], shown[k])
        top, segs, end = scale(n_units)
        if top == 1 and ladders[k]["steps"][level].get("percent"):
            end = "100%"
        ends[k] = (top, segs, end)
    end_w = max(len(e[2]) for e in ends.values())  # same bar length for every meter
    for k in KINDS:
        level, value, unit, n_units = ladder(ladders[k]["steps"], shown[k])
        prev = w.levels.get(k)
        if prev is not None and level > prev:
            w.message = ("↑ %s is now counted in %s" % (LABEL[k], ladders[k]["steps"][level]["many"]), now + 4)
        w.levels[k] = level
        label = st.fg(COLOR[k], "● ") + (st.inverse(" %s " % LABEL[k]) if flash else st.bold(LABEL[k]))
        lines.append("  %s  %s %s" % (label, st.bold(value), unit))
        top, segs, end = ends[k]
        meter, segs = gauge(n_units / top, segs, width - 6 - end_w, st, COLOR[k])
        caption = block_caption(top, segs, ladders[k]["steps"][level])
        lines.append("    " + meter + " " + st.dim(end))
        foot = {"energy": fmt_si(mid(t["energy"]), ENERGY_UNITS) + " of electricity",
                "water": fmt_si(mid(t["water"]), WATER_UNITS) + " of water consumed",
                "carbon": fmt_si(mid(t["carbon"]), CARBON_UNITS) + " incl. hardware manufacturing, excl. training"}[k]
        if len(foot) > width - 4 and k == "carbon":  # narrow window: keep the part that's easy to assume wrongly
            foot = fmt_si(mid(t["carbon"]), CARBON_UNITS) + " excl. model training"
        if caption:
            foot = (foot + " · " + caption) if len(foot) + 3 + len(caption) <= width - 4 else caption
        lines.append("    " + st.dim(foot))
        if not compact:
            lines.extend("    " + st.dim(x) for x in person_lines(baselines_for(w.cfg)[k], shown[k], width - 4))
        lines.append("")

    if w.view.period and s.get("buckets"):
        buckets = [(b["key"], b["short"], b["long"]) for b in s["buckets"]]
        word = {"day": "hour", "week": "day", "month": "day", "year": "month"}[w.view.period.kind]
        lines.append("  " + st.dim("per %s" % word))
        for k in KINDS:
            vals = [mid(b[k]) for b in s["buckets"]]
            peak = max(vals) if vals else 0
            u = pick_unit(peak, BASE_UNITS[k])
            peak_txt = "max %s %s" % (fmt(peak / u[0]), u[1]) if peak else "none"
            lines.append("  " + st.fg(COLOR[k], "● ") + LABEL[k] + "  " + st.dim(peak_txt))
            rows, col, gap = histogram(vals, width - 4, 1 if compact else HISTO_ROWS, st, COLOR[k])
            lines.extend("    " + r for r in rows)
        lines.append("    " + st.dim(axis_labels(buckets, col, gap, width - 4, s.get("current_bucket"))))
        lines.append("")
        if s["by_session"]:
            lines.append("  " + st.dim("top sessions"))
            for r in s["by_session"][:3]:
                share = mid(r["carbon"]) / mid(t["carbon"]) * 100 if mid(t["carbon"]) else 0
                name = r.get("title") or r["id"].split(":")[-1][:8]
                tail = "  %s  %s" % (fmt_si(mid(r["carbon"]), CARBON_UNITS), fmt_pct(share))
                head = "    %s %s " % (TOOL_SHORT.get(r["tool"], r["tool"]).ljust(8), "")
                lines.append(st.dim(head) + name[: max(8, width - len(head) - len(tail) - 2)] + st.dim(tail))
    if s["by_model"] and (w.view.period or len(s["by_model"]) > 1):
        lines.append("  " + st.dim("top models"))
        for m in s["by_model"][:3]:
            share = mid(m["carbon"]) / mid(t["carbon"]) * 100 if mid(t["carbon"]) else 0
            lines.append("    %s%s" % (m["model"][: width - 30].ljust(min(34, width - 30)),
                                       st.dim("  %s  %s" % (fmt_si(mid(m["carbon"]), CARBON_UNITS), fmt_pct(share)))))
        lines.append("")

    series = s.get("series")
    if series:
        lines.append("  " + st.dim("carbon over the session"))
        lines.append("  " + st.fg(COLOR["carbon"], sparkline([mid(p[1:3]) for p in series], width - 4)))
        lines.append("")

    dl = w.latest_delta
    if dl and mid(dl["energy"]) > 0:
        phone = mid(dl["energy"]) / ladders["energy"]["steps"][0]["size"] * 100
        text = "latest: +%s%% of a phone charge · +%s · +%s water · %s tokens" % (
            fmt(phone), fmt_si(mid(dl["carbon"]), CARBON_UNITS), fmt_si(mid(dl["water"]), WATER_UNITS),
            "{:,}".format(dl["tokens"]))
        lines.append("  " + (st.bold(text) if flash else st.dim(text)))
    msg, until = w.message
    if msg and now < until:
        lines.append("  " + st.fg(COLOR["water"], msg))
    elif live:
        keys = "←/→ switch · ↓/↑ earlier/later · y m w d period · "
        keys += ("" if w.view.follow else "f follow newest · ") + "q quit"
        lines.append("  " + st.dim(keys))
    else:
        lines.append("")
    return lines


def pick_unit(v, units):
    pick = units[0]
    for u in units:
        if abs(v) >= u[0]:
            pick = u
    return pick


def model_factors():
    from .model import FACTORS
    return FACTORS


def baselines_for(cfg):
    from .model import person_day as baselines
    return baselines(cfg)


def ease(p):
    return 1 - (1 - p) ** 3


def _interrupt(signum, frame):
    raise KeyboardInterrupt


KEYS = {"\x1b[C": "next", "\x1bOC": "next", "n": "next",
        "\x1b[D": "prev", "\x1bOD": "prev", "p": "prev",
        "\x1b[A": "later", "\x1bOA": "later", "\x1b[B": "earlier", "\x1bOB": "earlier",
        "[": "earlier", "]": "later",
        "y": "year:current", "m": "month:current", "w": "week:current", "d": "day:today",
        "f": "latest", "q": "quit"}


class Keyboard:
    """Unbuffered, unechoed key input for the live view (POSIX terminals and the Windows console).
    A context manager: the terminal is restored on exit."""

    # Windows consoles report special keys as a prefix plus a scan code; translate them to the
    # escape sequences POSIX terminals send, so one parser handles both.
    WINDOWS_KEYS = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D"}

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin is not None and sys.stdin.isatty() else None
        self.saved = None

    def __enter__(self):
        if self.fd is not None and os.name != "nt":
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)  # keys arrive one at a time, unechoed; Ctrl+C still works
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read(self, timeout):
        """Wait up to `timeout` for input; return the actions typed."""
        if self.fd is None:
            time.sleep(timeout)
            return []
        if os.name == "nt":
            return parse_keys(self._read_windows(timeout))
        if not select.select([self.fd], [], [], timeout)[0]:
            return []
        return parse_keys(os.read(self.fd, 64).decode(errors="ignore"))

    def _read_windows(self, timeout):
        end = time.time() + timeout
        while not msvcrt.kbhit():
            if time.time() >= end:
                return ""
            time.sleep(0.01)
        data = ""
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # special key: the scan code follows
                data += self.WINDOWS_KEYS.get(msvcrt.getwch(), "")
            else:
                data += ch
        return data


def enable_ansi():
    """Windows consoles interpret escape sequences only when asked (Windows Terminal always does)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (AttributeError, OSError):
        pass


def parse_keys(data):
    """Typed text -> actions (arrow keys arrive as escape sequences)."""
    actions = []
    while data:
        for seq, action in KEYS.items():
            if data.startswith(seq):
                actions.append(action)
                data = data[len(seq):]
                break
        else:
            esc = re.match(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.|.)?", data)
            if esc:  # any other escape sequence (F-keys, Home, ...): skip it whole, never as loose keys
                data = data[esc.end():]
                continue
            if data[0] in "123456789":
                actions.append(int(data[0]))
            data = data[1:]
    return actions


def frame_lines(w, shown, st, now, live=True):
    cols, rows = shutil.get_terminal_size((80, 24))
    lines = render(w, shown, st, cols, now, live)
    if live and len(lines) > rows - 1:  # too tall: drop the person-day lines and flatten histograms
        lines = render(w, shown, st, cols, now, live, compact=True)
    return lines[: rows - 1] if live else lines


def run_live(w, st):
    out = sys.stdout
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):  # restore the terminal when killed, too
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)
    enable_ansi()
    with Keyboard() as keyboard:
        _live_loop(w, st, out, keyboard)


def _live_loop(w, st, out, keyboard):
    launch.mark_watch(os.getpid())  # so `agent-footprint open` doesn't open a second one
    out.write("\x1b[?1049h\x1b[?25l")
    shown = {k: 0.0 for k in KINDS + ("hero",)}
    start, target, t0 = dict(shown), dict(shown), 0.0
    last_poll = 0.0
    retarget = True
    try:
        while True:
            now = time.time()
            if now - last_poll >= POLL_S:
                last_poll = now
                if w.poll() or retarget:
                    retarget = False
                    t = w.totals()
                    start = dict(shown)
                    target = {k: mid(t[k]) for k in KINDS}
                    target["hero"] = target["carbon"]
                    t0 = now
            p = min(1.0, (now - t0) / TWEEN_S) if t0 else 1.0
            for k in shown:
                shown[k] = start.get(k, 0) + (target.get(k, 0) - start.get(k, 0)) * ease(p)
            lines = frame_lines(w, shown, st, now)
            out.write("\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J")
            out.flush()
            for action in keyboard.read(FRAME_S if p < 1 or now < w.flash_until else POLL_S / 2):
                if action == "quit":
                    return
                if action in ("next", "prev"):
                    w.step(1 if action == "next" else -1)
                elif action in ("earlier", "later"):
                    w.shift(-1 if action == "earlier" else 1)
                elif isinstance(action, int):
                    w.jump(action)
                else:
                    w.show(action)
                last_poll, retarget = 0, True  # load the new view right away
    except KeyboardInterrupt:
        pass
    finally:
        launch.mark_watch(None)
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()


def watch(wanted="day:today", once=False, color=True, store_path=None):
    if os.name == "nt" and hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")  # e.g. `watch --once > file`: not the ANSI code page
    enable_ansi()
    st = Style(color and "NO_COLOR" not in os.environ and (sys.stdout.isatty() or not once))
    cfg = config.load_config()
    store = Store(store_path)
    syncer = sources.Syncer(store, cfg)
    syncer.sync()
    w = Watcher(store, syncer, cfg, wanted)
    w.refresh_active()
    if once:
        t = w.totals()
        shown = {k: mid(t[k]) for k in KINDS}
        shown["hero"] = shown["carbon"]
        print("\n".join(frame_lines(w, shown, st, time.time(), live=False)))
        return
    run_live(w, st)
