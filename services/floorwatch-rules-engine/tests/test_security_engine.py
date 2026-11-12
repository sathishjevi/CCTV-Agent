"""The security rules: restricted areas, closed hours, door/window break-ins, hiding after close.

The engine is driven with an injected wall clock so every time-dependent rule is deterministic."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from security_engine import SecurityEngine  # noqa: E402
from security_schedule import OpenHours  # noqa: E402


def ts(day, hour, minute=0):
    """Epoch seconds for 2026-11-<day> <hour>:<minute> UTC (the 2nd is a Monday)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).timestamp()


MON, TUE = 2, 3
DAYTIME = ts(MON, 12)             # open
CLOSING_GRACE = ts(MON, 23, 10)   # closed, but people are still locking up
NIGHT = ts(TUE, 2)                # armed


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def to(self, now):
        self.now = now

    def advance(self, seconds):
        self.now += seconds


ZONES = {
    "back_door": {"name": "Back Door", "role_tag": "security", "camera_id": "cam1", "access": "open", "entry_point": "door"},
    "side_window": {"name": "Side Window", "role_tag": "security", "camera_id": "cam2", "access": "open", "entry_point": "window"},
    "stockroom": {"name": "Stockroom", "role_tag": "janitor", "camera_id": "cam3", "access": "closed_hours", "entry_point": ""},
    "safe": {"name": "Cash Office", "role_tag": "security", "camera_id": "cam4", "access": "restricted", "entry_point": ""},
    "lobby": {"name": "Lobby", "role_tag": "usher", "camera_id": "cam5", "access": "open", "entry_point": ""},
}


@pytest.fixture
def clock():
    return Clock(NIGHT)


@pytest.fixture
def engine(clock):
    schedule = OpenHours("daily 09:00-23:00", "UTC", 30, 30)
    return SecurityEngine({k: dict(v) for k, v in ZONES.items()}, schedule, clock=clock,
                          repeat_seconds=600, max_repeats=3)


def present(zone_id, action="arrived", camera="cam1", confidence=0.9):
    return {"event_type": "zone_person_present", "action_type": action, "zone_id": zone_id,
            "camera_id": camera, "role_tag": "security", "confidence": confidence}


def cleared(zone_id):
    return {"event_type": "zone_person_cleared", "action_type": "cleared", "zone_id": zone_id,
            "camera_id": "cam1", "role_tag": "security", "confidence": 1.0}


def kinds(events):
    return [(e["event_type"], e.get("alert_kind")) for e in events]


# ── the rules ────────────────────────────────────────────────────────────────

def test_a_person_at_a_door_while_the_venue_is_armed_is_a_critical_possible_break_in(engine):
    out = engine.process_presence_event(present("back_door"))
    assert kinds(out) == [("security_alert", "break_in_door")]
    evt = out[0]
    assert evt["severity"] == "critical" and evt["zone_name"] == "Back Door"
    assert "possible break-in" in evt["message"].lower() and "Back Door" in evt["message"]
    assert evt["_notify"] is True and evt["alert_id"]


def test_a_window_is_a_break_in_too(engine):
    out = engine.process_presence_event(present("side_window", camera="cam2"))
    assert kinds(out) == [("security_alert", "break_in_window")]
    assert "(window)" in out[0]["message"]


def test_nothing_fires_at_a_door_while_the_venue_is_open(engine, clock):
    clock.to(DAYTIME)
    assert engine.process_presence_event(present("back_door")) == []


def test_nothing_fires_during_the_closing_grace_period(engine, clock):
    clock.to(CLOSING_GRACE)
    assert engine.process_presence_event(present("back_door")) == []
    assert engine.process_presence_event(present("stockroom", camera="cam3")) == []


def test_a_person_in_a_closed_hours_zone_is_after_hours_presence(engine):
    out = engine.process_presence_event(present("stockroom", camera="cam3"))
    assert kinds(out) == [("security_alert", "after_hours_presence")]
    assert out[0]["severity"] == "high"


def test_a_closed_hours_zone_is_fine_during_the_day(engine, clock):
    clock.to(DAYTIME)
    assert engine.process_presence_event(present("stockroom", camera="cam3")) == []


def test_a_zone_with_no_rule_never_alerts_even_at_3am(engine):
    assert engine.process_presence_event(present("lobby", camera="cam5")) == []


def test_a_restricted_zone_alerts_at_any_time_including_the_middle_of_the_day(engine, clock):
    clock.to(DAYTIME)
    out = engine.process_presence_event(present("safe", camera="cam4"))
    assert kinds(out) == [("security_alert", "restricted_zone_entry")]
    assert out[0]["severity"] == "high" and "restricted" in out[0]["message"].lower()


def test_unknown_or_deactivated_zones_are_ignored(engine):
    assert engine.process_presence_event(present("not_a_zone")) == []


def test_a_disabled_engine_does_nothing(clock):
    e = SecurityEngine(dict(ZONES), OpenHours("daily 09:00-23:00"), enabled=False, clock=clock)
    assert e.process_presence_event(present("safe")) == [] and e.tick() == []
    assert e.phase() == "off"


def test_no_opening_hours_configured_means_only_restricted_zones_can_alert():
    clock = Clock(NIGHT)
    e = SecurityEngine({k: dict(v) for k, v in ZONES.items()}, OpenHours(""), clock=clock)
    assert e.process_presence_event(present("back_door")) == []
    assert e.process_presence_event(present("stockroom", camera="cam3")) == []
    assert kinds(e.process_presence_event(present("safe", camera="cam4"))) == [("security_alert", "restricted_zone_entry")]


# ── de-duplication, "still there", "gone" ────────────────────────────────────

def test_heartbeats_for_the_same_stay_do_not_raise_more_alerts(engine, clock):
    engine.process_presence_event(present("back_door"))
    for _ in range(5):
        clock.advance(30)
        assert engine.process_presence_event(present("back_door", action="heartbeat")) == []
    assert len(engine.alerts) == 1


def test_when_they_leave_the_alert_says_so_but_stays_open_until_a_human_closes_it(engine):
    engine.process_presence_event(present("back_door"))
    out = engine.process_presence_event(cleared("back_door"))
    assert kinds(out) == [("security_alert_update", "break_in_door")]
    assert "no one is detected" in out[0]["message"].lower() and "_notify" not in out[0]
    (alert,) = engine.active_alerts()
    assert alert["status"] == "open" and alert["still_present"] is False


def test_coming_back_after_leaving_updates_the_same_alert_and_notifies_again(engine, clock):
    first = engine.process_presence_event(present("back_door"))[0]
    engine.process_presence_event(cleared("back_door"))
    clock.advance(120)
    out = engine.process_presence_event(present("back_door"))
    assert kinds(out) == [("security_alert_update", "break_in_door")]
    assert out[0]["alert_id"] == first["alert_id"] and out[0]["_notify"] is True
    assert len(engine.alerts) == 1


def test_an_unresolved_alert_is_repeated_while_the_person_is_still_there_then_stops(engine, clock):
    engine.process_presence_event(present("back_door"))
    sent = []
    for _ in range(8):
        clock.advance(601)
        engine.process_presence_event(present("back_door", action="heartbeat"))
        sent += [e for e in engine.tick() if e["event_type"] == "security_alert_update"]
    assert len(sent) == 3                                  # max_repeats
    assert all(e["_notify"] and "REMINDER" not in e["message"] and "Still unresolved" in e["message"] for e in sent)


def test_no_repeat_once_the_person_has_gone(engine, clock):
    engine.process_presence_event(present("back_door"))
    engine.process_presence_event(cleared("back_door"))
    clock.advance(3600)
    assert [e for e in engine.tick() if e["event_type"] == "security_alert_update"] == []


def test_acknowledging_stops_the_repeats_but_leaves_the_alert_open(engine, clock):
    alert_id = engine.process_presence_event(present("back_door"))[0]["alert_id"]
    ack = engine.acknowledge(alert_id, "boss")
    assert ack["event_type"] == "security_alert_update" and "Acknowledged by boss" in ack["message"]
    assert "_notify" not in ack
    clock.advance(3000)
    engine.process_presence_event(present("back_door", action="heartbeat"))
    assert [e for e in engine.tick() if e["event_type"] == "security_alert_update"] == []
    assert engine.alerts[alert_id]["status"] == "acknowledged"


def test_resolving_closes_it_records_who_and_why_and_a_new_detection_is_a_new_alert(engine, clock):
    alert_id = engine.process_presence_event(present("back_door"))[0]["alert_id"]
    done = engine.resolve(alert_id, "boss", "false_alarm", "it was the cat")
    assert done["event_type"] == "security_alert_resolved" and done["resolved_by"] == "boss"
    assert "false alarm" in done["message"] and "it was the cat" in done["message"]
    assert engine.alerts == {} and engine.resolved[0]["outcome"] == "false_alarm"
    engine.process_presence_event(cleared("back_door"))
    clock.advance(60)
    again = engine.process_presence_event(present("back_door"))
    assert kinds(again) == [("security_alert", "break_in_door")] and again[0]["alert_id"] != alert_id


def test_acting_on_an_unknown_or_already_closed_alert_is_refused(engine):
    assert engine.acknowledge("nope", "boss") is None
    assert engine.resolve("nope", "boss", "handled") is None


def test_stale_alerts_are_closed_automatically_and_say_so(engine, clock):
    alert_id = engine.process_presence_event(present("back_door"))[0]["alert_id"]
    clock.advance(24 * 3600 + 1)
    out = engine.tick()
    assert ("security_alert_resolved", "break_in_door") in kinds(out)
    resolved = next(e for e in out if e["event_type"] == "security_alert_resolved")
    assert resolved["resolved_by"] == "auto" and "automatically" in resolved["message"]
    assert alert_id not in engine.alerts


# ── escalation: an intruder who got in is one incident ───────────────────────

def test_everything_after_a_break_in_is_critical_and_says_so(engine):
    engine.process_presence_event(present("back_door"))
    out = engine.process_presence_event(present("stockroom", camera="cam3"))
    assert out[0]["severity"] == "critical"
    assert out[0]["message"].startswith("After a possible break-in:")


def test_a_closed_hours_alert_alone_stays_high(engine):
    assert engine.process_presence_event(present("stockroom", camera="cam3"))[0]["severity"] == "high"


# ── hiding after close ───────────────────────────────────────────────────────

def test_someone_still_inside_a_monitored_zone_when_the_venue_arms_is_flagged_as_possibly_hiding(engine, clock):
    clock.to(DAYTIME)
    engine.tick()                                           # first look: venue is open
    assert engine.process_presence_event(present("stockroom", camera="cam3")) == []
    clock.to(ts(MON, 23, 20))
    assert engine.tick() == []                              # closing grace: still locking up
    clock.to(ts(MON, 23, 31))
    out = engine.tick()                                     # armed
    assert kinds(out) == [("security_alert", "hiding_after_close")]
    assert out[0]["severity"] == "high" and "hiding" in out[0]["message"].lower()
    assert "Stockroom" in out[0]["message"] and "since 12:00" in out[0]["message"]


def test_someone_who_leaves_during_the_grace_period_is_not_flagged(engine, clock):
    clock.to(DAYTIME)
    engine.tick()
    engine.process_presence_event(present("stockroom", camera="cam3"))
    clock.to(ts(MON, 23, 15))
    engine.process_presence_event(cleared("stockroom"))
    clock.to(ts(MON, 23, 31))
    assert engine.tick() == []


def test_a_zone_that_allows_people_any_time_is_never_flagged_for_hiding(engine, clock):
    clock.to(DAYTIME)
    engine.tick()
    engine.process_presence_event(present("lobby", camera="cam5"))
    clock.to(ts(MON, 23, 31))
    assert engine.tick() == []


def test_a_heartbeat_after_arming_for_someone_who_was_already_there_is_hiding_not_a_new_arrival(engine, clock):
    clock.to(DAYTIME)
    engine.tick()
    engine.process_presence_event(present("stockroom", camera="cam3"))
    clock.to(ts(MON, 23, 31))
    out = engine.tick()
    assert kinds(out) == [("security_alert", "hiding_after_close")]
    clock.advance(30)
    assert engine.process_presence_event(present("stockroom", action="heartbeat", camera="cam3")) == []
    assert len(engine.alerts) == 1


def test_after_a_restart_someone_first_seen_by_heartbeat_is_reported_as_already_there(engine):
    out = engine.process_presence_event(present("stockroom", action="heartbeat", camera="cam3"))
    assert out[0]["alert_kind"] == "after_hours_presence"
    assert "already there" in out[0]["message"]


# ── disarm / re-arm ──────────────────────────────────────────────────────────

def test_disarming_silences_closed_hours_and_door_alerts_but_not_restricted_areas(engine):
    engine.disarm(120, "boss", "cleaning crew")
    assert engine.is_disarmed() and engine.phase() == "disarmed"
    assert engine.process_presence_event(present("back_door")) == []
    assert engine.process_presence_event(present("stockroom", camera="cam3")) == []
    assert kinds(engine.process_presence_event(present("safe", camera="cam4"))) == [("security_alert", "restricted_zone_entry")]


def test_disarming_is_announced_audited_and_capped(engine):
    evt = engine.disarm(99999, "boss", "delivery")
    assert evt["event_type"] == "security_mode_changed" and evt["_notify"] is True
    assert "boss" in evt["message"] and "delivery" in evt["message"]
    assert engine.disarmed_until - engine._clock() == pytest.approx(720 * 60)     # clamped to the configured maximum


def test_the_disarm_runs_out_on_its_own_and_people_still_inside_are_then_checked(engine, clock):
    clock.to(ts(MON, 23, 40))                               # armed
    engine.tick()
    engine.disarm(60, "boss", "cleaners")
    clock.advance(10)
    engine.tick()
    engine.process_presence_event(present("stockroom", camera="cam3"))      # the cleaners, vouched for
    clock.advance(61 * 60)
    out = engine.tick()
    assert ("security_mode_changed", None) in kinds(out) and any("re-armed automatically" in e["message"] for e in out)
    assert ("security_alert", "hiding_after_close") in kinds(out)           # still there after the window closed
    assert not engine.is_disarmed()


def test_re_arming_by_hand_ends_the_disarm_and_is_announced(engine):
    engine.disarm(60, "boss")
    evt = engine.arm("boss")
    assert evt["event_type"] == "security_mode_changed" and "re-armed by boss" in evt["message"]
    assert engine.arm("boss") is None                       # nothing to undo any more
    assert kinds(engine.process_presence_event(present("back_door"))) == [("security_alert", "break_in_door")]


# ── status, snapshot, restart ────────────────────────────────────────────────

def test_status_reports_phase_alerts_and_who_is_where(engine):
    engine.process_presence_event(present("back_door"))
    s = engine.status()
    assert s["enabled"] and s["armed"] and s["phase"] == "armed"
    assert [a["kind"] for a in s["alerts"]] == ["break_in_door"]
    assert s["occupied_zones"][0]["zone_id"] == "back_door"
    assert s["schedule"]["configured"] is True


def test_alerts_are_listed_most_severe_first(engine):
    engine.process_presence_event(present("stockroom", camera="cam3"))       # high
    engine.process_presence_event(present("back_door"))                       # critical
    assert [a["severity"] for a in engine.active_alerts()] == ["critical", "high"]


def test_the_snapshot_is_plain_json_and_a_new_leader_picks_open_alerts_back_up(engine, clock):
    alert_id = engine.process_presence_event(present("back_door"))[0]["alert_id"]
    engine.disarm(60, "boss")
    snap = json.loads(json.dumps(engine.snapshot()))                          # must survive Redis
    fresh = SecurityEngine({k: dict(v) for k, v in ZONES.items()}, OpenHours("daily 09:00-23:00"), clock=clock)
    fresh.load_state(snap)
    assert alert_id in fresh.alerts and fresh.is_disarmed() and fresh.disarmed_by == "boss"


def test_a_stale_disarm_is_not_restored(engine, clock):
    engine.disarm(10, "boss")
    snap = json.loads(json.dumps(engine.snapshot()))
    clock.advance(3600)
    fresh = SecurityEngine({k: dict(v) for k, v in ZONES.items()}, OpenHours("daily 09:00-23:00"), clock=clock)
    fresh.load_state(snap)
    assert not fresh.is_disarmed()


def test_load_state_ignores_garbage(engine):
    engine.load_state({"state": {"alerts": [{"nonsense": 1}, "x", None], "disarmed_until": "soon"}})
    assert engine.alerts == {} and not engine.is_disarmed()


def test_zone_settings_can_be_replaced_live(engine):
    engine.set_zones({"lobby": {"name": "Lobby", "role_tag": "usher", "camera_id": "c", "access": "restricted", "entry_point": ""}})
    assert kinds(engine.process_presence_event(present("lobby", camera="cam5"))) == [("security_alert", "restricted_zone_entry")]
    assert engine.process_presence_event(present("back_door")) == []        # no longer a known zone


def test_events_carry_the_fields_every_consumer_needs(engine):
    evt = engine.process_presence_event(present("back_door"))[0]
    for key in ("event_id", "timestamp", "camera_id", "zone_id", "role_tag", "event_type", "message", "zone_name",
                "alert_id", "alert_kind", "severity", "source_model_version"):
        assert evt.get(key) not in (None, ""), key
    assert evt["camera_id"] == "cam1" and evt["action_type"] == "alert"
