"""Calendar periods in local time: day, ISO week (Monday first), month and year.

View ids: "day:2026-10-10", "week:2026-W41", "month:2026-10", "year:2026"; "<kind>:current"
(and "day:today") follow the clock and roll over at midnight. Each period has histogram
buckets: hours for a day, days for a week or month, months for a year.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

KINDS = ("year", "month", "week", "day")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Period:
    kind: str
    start: date
    end: date        # exclusive
    current: bool    # follows the clock

    @property
    def id(self):
        if self.kind == "day":
            return "day:" + self.start.isoformat()
        if self.kind == "week":
            y, w, _ = self.start.isocalendar()
            return "week:%d-W%02d" % (y, w)
        if self.kind == "month":
            return "month:%d-%02d" % (self.start.year, self.start.month)
        return "year:%d" % self.start.year

    @property
    def view_id(self):
        return "%s:%s" % (self.kind, "today" if self.kind == "day" else "current") if self.current else self.id

    def epochs(self):
        """[start, end) as POSIX seconds, local midnights."""
        return (datetime.combine(self.start, time()).timestamp(), datetime.combine(self.end, time()).timestamp())

    def is_now(self):
        return self.start <= date.today() < self.end

    # ---------- histogram buckets ----------
    def buckets(self):
        """[(key, short label, long label)] in order."""
        if self.kind == "day":
            return [("%02d" % h, "%02d" % h, "%02d:00–%02d:00" % (h, (h + 1) % 24)) for h in range(24)]
        if self.kind == "year":
            return [("%02d" % m, MONTHS[m - 1], "%s %d" % (MONTHS[m - 1], self.start.year)) for m in range(1, 13)]
        out, d = [], self.start
        while d < self.end:
            short = d.strftime("%a") if self.kind == "week" else str(d.day)
            out.append((d.isoformat(), short, d.strftime("%a %d %b")))
            d += timedelta(days=1)
        return out

    def bucket_of(self, epoch):
        t = datetime.fromtimestamp(epoch)
        if self.kind == "day":
            return "%02d" % t.hour
        if self.kind == "year":
            return "%02d" % t.month
        return t.date().isoformat()

    def current_bucket(self):
        return self.bucket_of(datetime.now().timestamp()) if self.is_now() else None

    # ---------- labels ----------
    def label(self):
        today = date.today()
        if self.kind == "day":
            diff = (today - self.start).days
            return "today" if diff == 0 else "yesterday" if diff == 1 else self.start.strftime("%a %d %b %Y")
        if self.kind == "week":
            if self.is_now():
                return "this week"
            last = self.end - timedelta(days=1)
            return "week %d (%s – %s)" % (self.start.isocalendar()[1], self.start.strftime("%d %b"), last.strftime("%d %b %Y"))
        if self.kind == "month":
            return "this month" if self.is_now() else "%s %d" % (MONTHS[self.start.month - 1], self.start.year)
        return "this year" if self.is_now() else str(self.start.year)

    def shift(self, n):
        """The period n steps later (negative: earlier), as a fixed period."""
        if self.kind == "day":
            return containing("day", self.start + timedelta(days=n))
        if self.kind == "week":
            return containing("week", self.start + timedelta(weeks=n))
        if self.kind == "month":
            m = self.start.year * 12 + self.start.month - 1 + n
            return containing("month", date(m // 12, m % 12 + 1, 1))
        return containing("year", date(self.start.year + n, 1, 1))


def containing(kind, d, current=False):
    if kind == "day":
        return Period("day", d, d + timedelta(days=1), current)
    if kind == "week":
        start = d - timedelta(days=d.weekday())
        return Period("week", start, start + timedelta(days=7), current)
    if kind == "month":
        start = d.replace(day=1)
        nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        return Period("month", start, nxt, current)
    if kind == "year":
        return Period("year", date(d.year, 1, 1), date(d.year + 1, 1, 1), current)
    raise ValueError(kind)


def parse(view):
    """A period view id -> Period, or None if it isn't one. Raises ValueError on a bad date."""
    kind, _, arg = (view or "").partition(":")
    if kind not in KINDS:
        return None
    if arg in ("current", "today", ""):
        return containing(kind, date.today(), current=True)
    if kind == "day":
        return containing("day", date.fromisoformat(arg))
    if kind == "week":
        y, w = arg.upper().split("-W")
        return containing("week", date.fromisocalendar(int(y), int(w), 1))
    if kind == "month":
        y, m = arg.split("-")
        return containing("month", date(int(y), int(m), 1))
    return containing("year", date(int(arg), 1, 1))


def refresh(p):
    """A clock-following period re-anchored on today (for midnight rollover)."""
    return containing(p.kind, date.today(), current=True) if p.current and not p.is_now() else p


def available(days):
    """Local ISO dates with usage -> {kind: [period ids, newest first]}."""
    out = {k: [] for k in KINDS}
    for iso in days:
        d = date.fromisoformat(iso)
        for k in KINDS:
            pid = containing(k, d).id
            if not out[k] or out[k][-1] != pid:
                if pid not in out[k]:
                    out[k].append(pid)
    return out
