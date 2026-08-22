"""Unit tests for Part A active-time tracking / effort flagging (effort_engine.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from effort_engine import EffortEngine  # noqa: E402


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


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


ZONES_META = {"theatre3": {"name": "Theatre 3 (post-show)", "role_tag": "janitor", "camera_id": "theatre3_cam_1"}}
THRESHOLDS = {"_default": {"expected_active_ratio": 0.5}, "clean_door": {"expected_active_ratio": 0.5}}


def simulate_active_motion(engine, clock, camera_id, duration_seconds, step=10):
    """Feed periodic active motion samples (like a real ~0.1Hz pose skill
    would), since record_motion caps a single gap at max_motion_gap_seconds
    — a single before/after pair can't represent long stretches of
    continuous activity."""
    engine.record_motion(camera_id, active=True)
    elapsed = 0
    while elapsed < duration_seconds:
        clock.advance(step)
        elapsed += step
        engine.record_motion(camera_id, active=True)


def make_engine(staffed=True, zone_covered=True, **overrides):
    clock = FakeClock()
    engine = EffortEngine(
        roster=FakeRoster({"theatre3": staffed}),
        digest=FakeDigest(),
        zones_meta=ZONES_META,
        task_type_thresholds=THRESHOLDS,
        shadow_mode=True,
        update_interval_seconds=30,
        max_motion_gap_seconds=15,
        nudge_grace_ratio=0.3,
        nudge_margin=0.3,
        zone_is_covered=lambda zone_id: zone_covered,
        clock=clock,
        **overrides,
    )
    return engine, clock


# ── task assignment ──────────────────────────────────────────────────────

def test_assign_task_on_unstaffed_zone_returns_none():
    engine, _ = make_engine(staffed=False)
    assert engine.assign_task("Clean Door — Zone 4", "theatre3", 60) is None
    assert engine.tasks == {}


def test_assign_task_on_staffed_zone_creates_open_task():
    engine, _ = make_engine(staffed=True)
    evt = engine.assign_task("Clean Door — Zone 4", "theatre3", 60, task_type="clean_door")
    assert evt["event_type"] == "task_assigned"
    assert evt["assigned_minutes"] == 60
    assert evt["task_name"] == "Clean Door — Zone 4"
    assert len(engine.tasks) == 1


# ── motion accumulation ──────────────────────────────────────────────────

def test_record_motion_accumulates_active_seconds_only_when_zone_covered():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    engine.record_motion("theatre3_cam_1", active=True)  # baseline, no dt yet
    clock.advance(10)
    engine.record_motion("theatre3_cam_1", active=True)
    clock.advance(10)
    engine.record_motion("theatre3_cam_1", active=True)

    t = engine.tasks[task_id]
    assert t.active_seconds == 20


def test_record_motion_does_not_accumulate_when_inactive():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    engine.record_motion("theatre3_cam_1", active=False)
    clock.advance(10)
    engine.record_motion("theatre3_cam_1", active=False)

    assert engine.tasks[task_id].active_seconds == 0


def test_record_motion_does_not_accumulate_when_zone_not_covered():
    engine, clock = make_engine(staffed=True, zone_covered=False)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    engine.record_motion("theatre3_cam_1", active=True)
    clock.advance(10)
    engine.record_motion("theatre3_cam_1", active=True)

    assert engine.tasks[task_id].active_seconds == 0


def test_record_motion_caps_large_gap_at_max_gap_seconds():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    engine.record_motion("theatre3_cam_1", active=True)
    clock.advance(300)  # a 5-minute gap in samples
    engine.record_motion("theatre3_cam_1", active=True)

    assert engine.tasks[task_id].active_seconds == 15  # capped at max_motion_gap_seconds


def test_record_motion_ignores_other_cameras():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    engine.record_motion("some_other_cam", active=True)
    clock.advance(10)
    engine.record_motion("some_other_cam", active=True)

    assert engine.tasks[task_id].active_seconds == 0


# ── periodic updates + mid-task nudge ────────────────────────────────────

def test_tick_emits_periodic_active_time_update():
    engine, clock = make_engine(staffed=True)
    engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    assert engine.tick() == []  # not yet due
    clock.advance(31)
    out = engine.tick()
    assert any(e["event_type"] == "task_active_time_update" for e in out)


def test_mid_task_nudge_fires_when_active_ratio_falls_behind():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    # 25 minutes elapsed (well past the 30% grace), only ~5 min active — well behind schedule
    simulate_active_motion(engine, clock, "theatre3_cam_1", 5 * 60)
    clock.advance(20 * 60)  # now 25 min elapsed total, no more active motion

    out = engine.tick()
    nudges = [e for e in out if e["event_type"] == "task_low_effort_nudge"]
    assert len(nudges) == 1
    assert nudges[0]["shadow_mode_suppressed"] is True
    assert engine.tasks[task_id].nudged is True


def test_mid_task_nudge_does_not_fire_within_grace_period():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    clock.advance(5 * 60)  # only 8% elapsed — within grace
    out = engine.tick()
    assert not any(e["event_type"] == "task_low_effort_nudge" for e in out)


def test_mid_task_nudge_fires_only_once():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    clock.advance(25 * 60)
    out1 = engine.tick()
    assert any(e["event_type"] == "task_low_effort_nudge" for e in out1)
    clock.advance(60)
    out2 = engine.tick()
    assert not any(e["event_type"] == "task_low_effort_nudge" for e in out2)


# ── completion + flagging ────────────────────────────────────────────────

def test_complete_task_flags_when_active_time_far_below_threshold():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    simulate_active_motion(engine, clock, "theatre3_cam_1", 22 * 60)  # 22 of 60 min active — mirrors demo script

    evt = engine.complete_task(task_id)
    assert evt["event_type"] == "task_flag"
    assert engine.tasks[task_id].status == "flagged"
    assert any(e["event_type"] == "task_flag" for e in engine.digest.entries)


def test_complete_task_resolves_auto_when_active_time_meets_threshold():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    simulate_active_motion(engine, clock, "theatre3_cam_1", 40 * 60)  # 40 of 60 min — well above 50% threshold

    evt = engine.complete_task(task_id)
    assert evt["event_type"] == "task_resolved"
    assert evt["resolved_by"] == "auto"
    assert engine.tasks[task_id].status == "resolved"


def test_complete_unknown_task_returns_none():
    engine, _ = make_engine(staffed=True)
    assert engine.complete_task("nonexistent") is None


def test_per_task_type_threshold_used_not_global_default():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    engine.task_type_thresholds = {"_default": {"expected_active_ratio": 0.9}, "clean_door": {"expected_active_ratio": 0.3}}
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]

    simulate_active_motion(engine, clock, "theatre3_cam_1", 20 * 60)  # 20/60 = 33% >= clean_door's 30% threshold

    evt = engine.complete_task(task_id)
    assert evt["event_type"] == "task_resolved"  # would have been flagged under the 90% default


# ── supervisor confirm/dismiss ───────────────────────────────────────────

def test_confirm_flag_reopens_task_with_supervisor_attribution():
    """confirm_flag() no longer terminally resolves — it REOPENS the
    task so the employee can actually be notified and given a genuine
    second attempt (previously the dashboard claimed "following up with
    the employee" but nothing was ever sent, and the task just vanished
    to history)."""
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door",
                                   assigned_to="101", assigned_by="user:admin")
    task_id = task_evt["task_id"]
    engine.mark_notified(task_id)
    engine.complete_task(task_id)  # active_seconds=0 -> flagged
    assert engine.pending_flags()[0]["task_id"] == task_id

    evt = engine.confirm_flag(task_id, supervisor_id="alice")
    assert evt["event_type"] == "task_flag_confirmed"
    assert evt["resolved_by"] == "supervisor:alice"
    assert evt["assigned_to"] == "101"
    assert evt["workflow_status"] == "unassigned"  # main.py re-notifies from here

    t = engine.tasks[task_id]
    assert t.status == "open"  # reopened, not resolved
    assert t.active_seconds == 0.0
    assert t.nudged is False
    assert t.status_nudge_sent is False
    assert t.reopened_for_review is True  # unlocks resolve_after_review()
    assert engine.pending_flags() == []  # no longer flagged


def test_confirm_flag_resets_the_elapsed_time_clock():
    """The employee gets a genuinely fresh window — elapsed time
    computed from start_monotonic must restart from zero, not continue
    counting from the original (already-blown) assignment."""
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    clock.advance(3600)  # a full hour passes before it's even flagged
    engine.complete_task(task_id)
    assert engine.tasks[task_id].status == "flagged"

    engine.confirm_flag(task_id, supervisor_id="alice")
    t = engine.tasks[task_id]
    elapsed_minutes = (clock() - t.start_monotonic) / 60.0
    assert elapsed_minutes == 0.0


def test_confirm_flag_can_be_flagged_and_confirmed_a_second_time():
    """The reopened task goes through the normal effort lifecycle again
    on its own merits — if it gets flagged a second time, confirm_flag
    must still work exactly the same way, not error out or double up
    stale state from the first pass."""
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    engine.complete_task(task_id)
    engine.confirm_flag(task_id, supervisor_id="alice")
    assert engine.tasks[task_id].status == "open"

    second_flag = engine.complete_task(task_id)  # still no active time -> flagged again
    assert second_flag["event_type"] == "task_flag"
    assert engine.tasks[task_id].status == "flagged"

    second_confirm = engine.confirm_flag(task_id, supervisor_id="bob")
    assert second_confirm["event_type"] == "task_flag_confirmed"
    assert engine.tasks[task_id].status == "open"


# ── resolve_after_review — the direct exit from a reopened task,
# bypassing complete_task()'s effort-flag check (see its docstring:
# without this, a reopened task with no new motion signal would just
# re-flag every time "Mark complete" is clicked, an unbreakable loop
# short of Dismiss) ───────────────────────────────────────────────────

def test_resolve_after_review_closes_a_reopened_task_without_reflagging():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    engine.complete_task(task_id)  # -> flagged
    engine.confirm_flag(task_id, supervisor_id="alice")  # -> reopened

    evt = engine.resolve_after_review(task_id, supervisor_id="alice")
    assert evt["event_type"] == "task_resolved"
    assert evt["action_type"] == "reviewed"
    assert evt["resolved_by"] == "supervisor:alice"

    t = engine.tasks[task_id]
    assert t.status == "resolved"
    assert t.workflow_status == "completed"
    assert t.reopened_for_review is False
    assert engine.pending_flags() == []


def test_resolve_after_review_rejects_a_task_never_reopened():
    """Not a general-purpose "resolve anything" bypass — only usable on
    a task that actually went through confirm_flag()."""
    engine, _ = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    assert engine.resolve_after_review(task_id, supervisor_id="alice") is None


def test_resolve_after_review_rejects_a_still_flagged_task():
    """Must go through confirm_flag() (reopen) first — can't skip
    straight from "flagged" to "resolved" via this path."""
    engine, _ = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    engine.complete_task(task_id)  # -> flagged, not reopened yet
    assert engine.resolve_after_review(task_id, supervisor_id="alice") is None


def test_resolve_after_review_rejects_after_already_resolved():
    engine, _ = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    engine.complete_task(task_id)
    engine.confirm_flag(task_id, supervisor_id="alice")
    engine.resolve_after_review(task_id, supervisor_id="alice")
    assert engine.resolve_after_review(task_id, supervisor_id="alice") is None  # already resolved


def test_resolve_after_review_rejects_unknown_task():
    engine, _ = make_engine(staffed=True, zone_covered=True)
    assert engine.resolve_after_review("nonexistent-task-id", supervisor_id="alice") is None


def test_complete_task_still_works_normally_after_reopen_if_active_time_now_sufficient():
    """The parallel path stays available — Mark complete still runs the
    normal check, and if the employee genuinely did the work this time
    (active time now sufficient), it resolves cleanly without ever
    needing resolve_after_review at all."""
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    engine.complete_task(task_id)  # -> flagged
    engine.confirm_flag(task_id, supervisor_id="alice")  # -> reopened

    t = engine.tasks[task_id]
    t.active_seconds = 40 * 60  # this time, plenty of active work happened
    evt = engine.complete_task(task_id)
    assert evt["event_type"] == "task_resolved"
    assert evt["resolved_by"] == "auto"
    assert t.reopened_for_review is False  # cleared on real resolution too


def test_dismiss_flag_clears_reopened_for_review():
    engine, _ = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    engine.complete_task(task_id)
    engine.confirm_flag(task_id, supervisor_id="alice")  # reopened_for_review=True
    engine.complete_task(task_id)  # flagged again
    engine.dismiss_flag(task_id, supervisor_id="bob")
    assert engine.tasks[task_id].reopened_for_review is False


def test_dismiss_flag_resolves_with_supervisor_attribution():
    engine, clock = make_engine(staffed=True, zone_covered=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    task_id = task_evt["task_id"]
    engine.complete_task(task_id)

    evt = engine.dismiss_flag(task_id, supervisor_id="bob")
    assert evt["action_type"] == "dismissed"
    assert evt["resolved_by"] == "supervisor:bob"


def test_confirm_flag_on_non_flagged_task_returns_none():
    engine, _ = make_engine(staffed=True)
    task_evt = engine.assign_task("Clean Door", "theatre3", 60, task_type="clean_door")
    assert engine.confirm_flag(task_evt["task_id"]) is None
