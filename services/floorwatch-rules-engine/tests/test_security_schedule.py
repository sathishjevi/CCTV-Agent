"""Opening hours: parsing, overnight spans, and what counts as "armed" (closed, past the grace periods)."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from security_schedule import OpenHours, ScheduleError  # noqa: E402

# 2026-11-02 is a Monday.
def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


MON, TUE, WED, SAT, SUN = 2, 3, 4, 7, 8


def hours(spec="mon-fri 09:00-23:00; sat 10:00-02:00; sun closed", tz="UTC", closing=30, opening=30):
    return OpenHours(spec, tz, closing, opening)


def test_open_during_the_day_and_armed_in_the_small_hours():
    h = hours()
    assert h.phase(at(MON, 12)) == "open" and h.is_open(at(MON, 12)) and not h.is_armed(at(MON, 12))
    assert h.phase(at(TUE, 3)) == "armed" and h.is_armed(at(TUE, 3)) and not h.is_open(at(TUE, 3))


def test_grace_periods_after_closing_and_before_opening():
    h = hours(closing=30, opening=30)
    assert h.phase(at(MON, 23, 10)) == "closing_grace" and not h.is_armed(at(MON, 23, 10))
    assert h.phase(at(MON, 23, 29)) == "closing_grace"
    assert h.phase(at(MON, 23, 30)) == "armed"
    assert h.phase(at(TUE, 8, 29)) == "armed"
    assert h.phase(at(TUE, 8, 30)) == "opening_grace" and not h.is_armed(at(TUE, 8, 45))
    assert h.phase(at(TUE, 9)) == "open"


def test_a_closing_time_before_the_opening_time_runs_past_midnight():
    h = hours()
    assert h.phase(at(SUN, 1)) == "open"            # Saturday's 10:00-02:00 still running
    assert h.phase(at(SUN, 2, 10)) == "closing_grace"
    assert h.phase(at(SUN, 3)) == "armed"


def test_a_day_that_is_not_listed_is_closed_all_day():
    h = hours()
    assert h.phase(at(SUN, 12)) == "armed"          # "sun closed"
    h2 = hours("mon-fri 09:00-17:00")
    assert h2.phase(at(SAT, 12)) == "armed"         # weekend not mentioned


def test_no_schedule_means_never_closed_and_never_armed():
    h = OpenHours("")
    assert h.configured is False
    assert h.is_open(at(MON, 3)) and not h.is_armed(at(MON, 3)) and h.phase(at(MON, 3)) == "unscheduled"


def test_open_all_day():
    h = hours("daily 24h")
    assert all(h.phase(at(d, hr)) == "open" for d in (MON, SAT, SUN) for hr in (0, 3, 12, 23))


def test_day_names_ranges_lists_and_a_range_that_wraps_the_week():
    assert hours("mon,wed 09:00-17:00").phase(at(TUE, 12)) == "armed"
    assert hours("mon,wed 09:00-17:00").phase(at(WED, 12)) == "open"
    wrap = hours("fri-mon 09:00-17:00")             # fri, sat, sun, mon
    assert wrap.phase(at(SUN, 12)) == "open" and wrap.phase(at(MON, 12)) == "open"
    assert wrap.phase(at(TUE, 12)) == "armed"
    assert hours("Daily 09:00-17:00").phase(at(WED, 12)) == "open"


def test_spaces_around_dashes_are_forgiven():
    h = hours("mon - fri 09:00 - 17:00")
    assert h.phase(at(MON, 12)) == "open"


def test_the_schedule_is_read_in_the_venues_own_timezone():
    h = hours("daily 09:00-17:00", tz="Asia/Kolkata", closing=0, opening=0)     # UTC+5:30
    assert h.phase(at(MON, 4, 0)) == "open"          # 09:30 local
    assert h.phase(at(MON, 12, 0)) == "armed"        # 17:30 local


@pytest.mark.parametrize("bad", ["mon 9-5", "funday 09:00-10:00", "mon-fri", "mon 25:00-26:00", "mon 09:00"])
def test_a_misunderstood_setting_is_a_clear_error_not_a_silent_pass(bad):
    with pytest.raises(ScheduleError):
        OpenHours(bad)


def test_an_unknown_timezone_is_a_clear_error():
    with pytest.raises(ScheduleError):
        OpenHours("daily 09:00-17:00", "Mars/Olympus_Mons")


def test_closing_and_opening_grace_overlap_never_arms_a_short_closure():
    h = hours("daily 09:00-17:00", closing=30, opening=30)
    # closed 17:00-09:00 is long, but a two-slot day with a 40-minute break is never armed:
    split = OpenHours("daily 09:00-12:00; daily 12:40-17:00", "UTC", 30, 30)
    assert not split.is_armed(at(MON, 12, 20))
    assert h.is_armed(at(MON, 3))
