"""Unit tests for the Tier 1/2/3 escalation state machine (engine.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from digest_store import DigestStore  # noqa: E402
from engine import RulesEngine  # noqa: E402
from roster import Roster  # noqa: E402


class FakeRoster:
    def __init__(self, staffed: dict):
        self.staffed = staffed

    def is_zone_staffed(self, zone_id: str) -> bool:
        return self.staffed.get(zone_id, False)


class FakeDigest:
    def __init__(self):
        self.entries = []

    def append(self, event):
        self.entries.append(event)

    def read_all(self):
        return self.entries


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


ZONES_META = {"concession": {"name": "Concession Counter", "role_tag": "concession"}}


def make_engine(staffed=True, shadow_mode=True, **overrides):
    clock = FakeClock()
    engine = RulesEngine(
        roster=FakeRoster({"concession": staffed}),
        digest=FakeDigest(),
        zones_meta=ZONES_META,
        shadow_mode=shadow_mode,
        nudge_timer_seconds=180,
        nudge_cap_per_shift=3,
        command_timer_seconds=300,
        command_throttle_per_hour=5,
        resolve_cooldown_seconds=900,
        clock=clock,
        **overrides,
    )
    return engine, clock


def gap_event(zone_id="concession", camera_id="lobby_cam_1", confidence=1.0):
    return {
        "camera_id": camera_id, "zone_id": zone_id, "role_tag": "concession",
        "entity_ref": None, "event_type": "zone_gap", "confidence": confidence,
    }


def covered_event(zone_id="concession", camera_id="lobby_cam_1", entity_ref="track_0"):
    return {
        "camera_id": camera_id, "zone_id": zone_id, "role_tag": "concession",
        "entity_ref": entity_ref, "event_type": "zone_covered", "confidence": 0.9,
    }


# ── roster gate ──────────────────────────────────────────────────────────

def test_gap_on_unstaffed_zone_never_nudges():
    engine, _ = make_engine(staffed=False)
    out = engine.process_detection_event(gap_event())
    assert out == []
    assert engine.zones["concession"].status == "covered"  # never even entered gap state
    assert engine.digest.entries[0]["action_type"] == "ignored_unstaffed"


# ── tier 1: nudge ────────────────────────────────────────────────────────

def test_gap_on_staffed_zone_issues_tier1_nudge():
    engine, _ = make_engine(staffed=True)
    out = engine.process_detection_event(gap_event())
    assert len(out) == 1
    assert out[0]["event_type"] == "zone_nudge_sent"
    assert out[0]["tier"] == 1
    assert engine.zones["concession"].status == "nudge"
    assert engine.zones["concession"].nudge_count_shift == 1


def test_shadow_mode_suppresses_real_send_but_still_emits_event():
    engine, _ = make_engine(staffed=True)
    out = engine.process_detection_event(gap_event())
    assert out[0]["shadow_mode_suppressed"] is True


def test_shadow_mode_never_calls_on_notify_even_with_notifier_configured():
    """Safety property: a configured real notification channel must never
    actually fire while shadow_mode is True — this is the 'instantly
    re-enable shadow mode' guarantee from the Phase 4 brief."""
    calls = []
    engine, _ = make_engine(staffed=True, shadow_mode=True, on_notify=lambda ch, evt: calls.append((ch, evt)))
    engine.process_detection_event(gap_event())
    assert calls == []


def test_live_mode_calls_on_notify_when_not_shadow():
    calls = []
    engine, _ = make_engine(staffed=True, shadow_mode=False, on_notify=lambda ch, evt: calls.append((ch, evt)))
    out = engine.process_detection_event(gap_event())
    assert out[0]["shadow_mode_suppressed"] is False
    assert len(calls) == 1
    assert calls[0][0] == "employee_nudge"


# ── tier 1 -> tier 2 on timeout ──────────────────────────────────────────

def test_nudge_timeout_escalates_to_tier2_command():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    assert engine.tick() == []  # not yet expired
    clock.advance(181)
    out = engine.tick()
    assert len(out) == 1
    assert out[0]["event_type"] == "zone_supervisor_command"
    assert out[0]["tier"] == 2
    assert engine.zones["concession"].status == "command"


# ── tier 2 -> tier 3 on timeout ──────────────────────────────────────────

def test_command_timeout_escalates_to_tier3_and_writes_digest():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    clock.advance(181)
    engine.tick()  # -> command
    clock.advance(301)
    out = engine.tick()
    assert len(out) == 1
    assert out[0]["event_type"] == "zone_escalated"
    assert out[0]["tier"] == 3
    assert engine.zones["concession"].status == "escalated"
    assert any(e["event_type"] == "zone_escalated" for e in engine.digest.entries)


# ── nudge cap -> bypass straight to tier 2 ───────────────────────────────

def test_third_gap_in_shift_bypasses_nudge_straight_to_command():
    engine, clock = make_engine(staffed=True)
    for _ in range(3):
        engine.process_detection_event(gap_event())
        clock.advance(1000)  # clear resolve cooldown between cycles
        engine.process_detection_event(covered_event())
        clock.advance(1000)

    out = engine.process_detection_event(gap_event())
    assert len(out) == 1
    assert out[0]["event_type"] == "zone_supervisor_command"
    assert out[0]["tier"] == 2


# ── auto-resolve on zone_covered ─────────────────────────────────────────

def test_zone_covered_while_nudged_auto_resolves_silently_tracks_tier():
    engine, _ = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    out = engine.process_detection_event(covered_event())
    assert len(out) == 1
    assert out[0]["event_type"] == "zone_resolved"
    assert out[0]["tier"] == 1
    assert out[0]["resolved_by"] == "auto"
    assert engine.zones["concession"].status == "covered"


def test_zone_covered_while_already_covered_emits_nothing():
    engine, _ = make_engine(staffed=True)
    out = engine.process_detection_event(covered_event())
    assert out == []


# ── resolve cooldown ─────────────────────────────────────────────────────

def test_resolve_cooldown_suppresses_immediate_re_gap():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    engine.process_detection_event(covered_event())  # resolves, starts 900s cooldown
    clock.advance(10)
    out = engine.process_detection_event(gap_event())
    assert out == []
    assert engine.zones["concession"].status == "covered"


def test_gap_after_cooldown_expires_nudges_again():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    engine.process_detection_event(covered_event())
    clock.advance(901)
    out = engine.process_detection_event(gap_event())
    assert len(out) == 1
    assert out[0]["event_type"] == "zone_nudge_sent"


# ── human-approved supervisor actions ────────────────────────────────────

def test_approve_resolves_pending_command_and_marks_supervisor():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    clock.advance(181)
    engine.tick()  # -> command
    evt = engine.approve("concession", supervisor_id="alice")
    assert evt["event_type"] == "zone_resolved"
    assert evt["resolved_by"] == "supervisor:alice"
    assert engine.zones["concession"].status == "covered"


def test_reassign_resolves_without_sending_directive():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    evt = engine.reassign("concession", supervisor_id="bob")
    assert evt["action_type"] == "resolved"
    assert evt["resolved_by"] == "supervisor:bob"
    assert engine.zones["concession"].status == "covered"


def test_approve_on_unknown_zone_returns_none():
    engine, _ = make_engine(staffed=True)
    assert engine.approve("nonexistent") is None


def test_approve_still_works_after_tier3_escalation():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    clock.advance(181)
    engine.tick()  # -> command
    clock.advance(301)
    engine.tick()  # -> escalated
    assert engine.zones["concession"].status == "escalated"

    evt = engine.approve("concession", supervisor_id="carol")
    assert evt is not None
    assert evt["event_type"] == "zone_resolved"
    assert engine.zones["concession"].status == "covered"


def test_pending_commands_includes_escalated_zones():
    engine, clock = make_engine(staffed=True)
    engine.process_detection_event(gap_event())
    clock.advance(181)
    engine.tick()
    clock.advance(301)
    engine.tick()  # -> escalated
    pending = engine.pending_commands()
    assert any(p["zone_id"] == "concession" for p in pending)
