"""Opening hours — what "closed" means for the security alerts.

Configured with one setting, FLOORWATCH_OPEN_HOURS, in plain words:

    mon-fri 09:00-23:00; sat 10:00-02:00; sun closed

  * Days are mon..sun, a single day (`sat`), a range (`mon-fri`, which may wrap: `fri-sun`), a list
    (`mon,wed,fri`) or `daily`. A day that is not mentioned is closed all day; `closed` says so out loud.
  * Times are 24-hour HH:MM in the venue's own timezone (FLOORWATCH_TIMEZONE). A closing time at or
    before the opening time runs past midnight, so `sat 10:00-02:00` is Saturday 10:00 to Sunday 02:00.
  * `24h` instead of a time range means open all day (`daily 24h` = never closed).
  * Several rules are separated by `;`. Empty = no schedule configured, and "closed hours" never happens.

People are always around a little before opening and after closing (staff arriving, last customers
leaving, locking up), so an alarm that armed on the dot would cry wolf every single day. The venue is
therefore "armed" only once it has been closed for `closing_grace_minutes`, and stops being armed
`opening_grace_minutes` before it opens. Those two numbers are the knobs to tune the false-alarm rate.

Wall-clock arithmetic is done in local time, so across a daylight-saving change the boundary can be an
hour off for that one night — which is why the grace periods exist, and why this is documented rather
than hidden."""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 is not supported by this service anyway
    ZoneInfo = None

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ScheduleError(ValueError):
    """The open-hours setting could not be understood. The message says exactly where."""


def _parse_days(text: str) -> List[int]:
    text = text.strip().lower()
    if text in ("daily", "every day", "all", "*"):
        return list(range(7))
    days: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a, b = a.strip()[:3], b.strip()[:3]
            if a not in _DAYS or b not in _DAYS:
                raise ScheduleError(f"unknown day in '{part}' (use mon, tue, wed, thu, fri, sat, sun)")
            i, j = _DAYS.index(a), _DAYS.index(b)
            while True:                       # a range may wrap the week: fri-mon
                days.append(i)
                if i == j:
                    break
                i = (i + 1) % 7
        else:
            d = part[:3]
            if d not in _DAYS:
                raise ScheduleError(f"unknown day '{part}' (use mon, tue, wed, thu, fri, sat, sun)")
            days.append(_DAYS.index(d))
    if not days:
        raise ScheduleError("a rule has no days")
    return sorted(set(days))


def _parse_clock(text: str) -> time:
    text = text.strip()
    try:
        hh, mm = text.split(":")
        h, m = int(hh), int(mm)
    except ValueError:
        raise ScheduleError(f"'{text}' is not a time (use 24-hour HH:MM, e.g. 09:00)") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ScheduleError(f"'{text}' is not a valid time")
    return time(h, m)


@dataclass(frozen=True)
class _Rule:
    days: Tuple[int, ...]
    start: time
    end: time
    all_day: bool = False


def _parse_rule(chunk: str) -> Optional[_Rule]:
    """One rule such as 'mon-fri 09:00-23:00'; None for an explicit 'closed'."""
    day_part, _, hours_part = chunk.rpartition(" ")
    # "mon-fri 09:00-23:00" splits cleanly on the LAST space; "daily 24h" and "sun closed" likewise.
    if not day_part:
        raise ScheduleError("it needs days and hours, e.g. 'mon-fri 09:00-23:00'")
    days = tuple(_parse_days(day_part))
    hours_part = hours_part.strip().lower()
    if hours_part == "closed":
        return None                               # same as not listing the day
    if hours_part in ("24h", "24/7", "all-day", "allday"):
        return _Rule(days, time(0, 0), time(0, 0), all_day=True)
    a, sep, b = hours_part.partition("-")
    if not sep:
        raise ScheduleError(f"'{hours_part}' is not a time range (use HH:MM-HH:MM)")
    return _Rule(days, _parse_clock(a), _parse_clock(b))


def parse_open_hours(spec: str) -> List[_Rule]:
    rules: List[_Rule] = []
    for chunk in (spec or "").split(";"):
        chunk = re.sub(r"\s*-\s*", "-", chunk.strip())      # "mon - fri 09:00 - 23:00" -> "mon-fri 09:00-23:00"
        if not chunk:
            continue
        try:
            rule = _parse_rule(chunk)
        except ScheduleError as e:
            raise ScheduleError(f"in the rule '{chunk}': {e}") from None      # say WHICH rule has the typo
        if rule is not None:
            rules.append(rule)
    return rules


class OpenHours:
    """When the venue is open, and from that when it is "armed" (closed, past the grace periods)."""

    def __init__(self, spec: str = "", timezone_name: str = "UTC",
                 closing_grace_minutes: float = 30, opening_grace_minutes: float = 30):
        self.rules = parse_open_hours(spec)
        self.configured = bool(self.rules)
        self.closing_grace = timedelta(minutes=max(0.0, closing_grace_minutes))
        self.opening_grace = timedelta(minutes=max(0.0, opening_grace_minutes))
        self.timezone_name = timezone_name or "UTC"
        try:
            self._tz = ZoneInfo(self.timezone_name)
        except Exception as e:  # unknown zone name, or no tz database installed
            raise ScheduleError(f"unknown timezone '{self.timezone_name}' ({e})") from None

    # ── local time ──
    def local_now(self, now: Optional[datetime] = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(self._tz).replace(tzinfo=None)

    def _intervals_around(self, local: datetime) -> List[Tuple[datetime, datetime]]:
        """Every open interval that starts on the day before, the day of, or the day after `local`."""
        out = []
        for offset in (-1, 0, 1):
            day = (local + timedelta(days=offset)).date()
            for rule in self.rules:
                if day.weekday() not in rule.days:
                    continue
                start = datetime.combine(day, rule.start)
                if rule.all_day:
                    end = start + timedelta(days=1)
                else:
                    end = datetime.combine(day, rule.end)
                    if end <= start:
                        end += timedelta(days=1)
                out.append((start, end))
        return out

    # ── questions the security engine asks ──
    def is_open(self, now: Optional[datetime] = None) -> bool:
        if not self.configured:
            return True                      # no schedule = never "closed"
        local = self.local_now(now)
        return any(s <= local < e for s, e in self._intervals_around(local))

    def is_armed(self, now: Optional[datetime] = None) -> bool:
        """Closed, and past the closing grace / before the opening grace. False when no schedule is set."""
        if not self.configured:
            return False
        local = self.local_now(now)
        for s, e in self._intervals_around(local):
            if s - self.opening_grace <= local < e + self.closing_grace:
                return False
        return True

    def phase(self, now: Optional[datetime] = None) -> str:
        """'open' | 'closing_grace' | 'opening_grace' | 'armed' | 'unscheduled'."""
        if not self.configured:
            return "unscheduled"
        local = self.local_now(now)
        intervals = self._intervals_around(local)
        if any(s <= local < e for s, e in intervals):
            return "open"
        if any(e <= local < e + self.closing_grace for _, e in intervals):
            return "closing_grace"
        if any(s - self.opening_grace <= local < s for s, _ in intervals):
            return "opening_grace"
        return "armed"

    def describe(self) -> dict:
        return {"configured": self.configured, "timezone": self.timezone_name,
                "closing_grace_minutes": self.closing_grace.total_seconds() / 60,
                "opening_grace_minutes": self.opening_grace.total_seconds() / 60}
