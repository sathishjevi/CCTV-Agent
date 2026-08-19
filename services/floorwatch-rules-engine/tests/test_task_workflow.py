"""Unit tests for the task-workflow dimension added to EffortEngine —
assignment to a directory employee, notification outcomes, employee
status replies (START/MORE/REVIEW), the budget-expiry status nudge
("Pending — waiting for update from employee"), supervisor extension/
reassignment, and the SMS reply-to-task resolution helpers.

The EFFORT dimension (active-time accrual, flag-on-completion) is
covered by test_effort_engine.py and deliberately untouched — these two
dimensions are orthogonal by design (see WORKFLOW_STATUSES in
effort_engine.py)."""

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

from effort_engine import EffortEngine  # noqa: E402


class AllStaffedRoster:
    def is_zone_staffed(self, zone_id):
        return True


class NullDigest:
    def append(self, evt):
        pass


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


ZONES_META = {"theatre3": {"name": "Theatre 3", "role_tag": "janitorial", "camera_id": "cam3"}}


def make_engine():
    clock = FakeClock()
    engine = EffortEngine(
        roster=AllStaffedRoster(), digest=NullDigest(), zones_meta=ZONES_META,
        task_type_thresholds={"_default": {"expected_active_ratio": 0.5}},
        update_interval_seconds=99999,  # silence periodic active-time events in these tests
        clock=clock,
    )
    return engine, clock


def assign(engine, assigned_to="101", minutes=30.0):
    evt = engine.assign_task("Clean Door", "theatre3", minutes,
                              assigned_to=assigned_to, assigned_by="user:admin")
    return evt["task_id"]


# ── assignment carries the workflow fields ──────────────────────────────

def test_assign_task_records_assignee_and_starts_unassigned():
    engine, _ = make_engine()
    evt = engine.assign_task("Clean Door", "theatre3", 30, assigned_to="101", assigned_by="user:admin")
    assert evt["assigned_to"] == "101"
    assert evt["assigned_by"] == "user:admin"
    t = engine.tasks[evt["task_id"]]
    assert t.workflow_status == "unassigned"  # notified only after the send actually succeeds


def test_assign_task_without_assignee_still_works():
    engine, _ = make_engine()
    evt = engine.assign_task("Clean Door", "theatre3", 30)
    assert evt["assigned_to"] is None
    assert engine.tasks[evt["task_id"]].workflow_status == "unassigned"


# ── notification outcomes ───────────────────────────────────────────────

def test_mark_notified_transitions_and_reports():
    engine, _ = make_engine()
    task_id = assign(engine)
    evt = engine.mark_notified(task_id)
    assert evt["workflow_status"] == "notified"
    assert "101" in evt["message"]
    assert "30 min" in evt["message"]  # the allocated time is in the message
    assert engine.tasks[task_id].workflow_status == "notified"


def test_mark_notify_failed_is_visible_state_not_silence():
    engine, _ = make_engine()
    task_id = assign(engine)
    evt = engine.mark_notify_failed(task_id, reason="no phone on file")
    assert engine.tasks[task_id].workflow_status == "notify_failed"
    assert "does not know about this task" in evt["message"]


def test_mark_notified_twice_is_a_noop_second_time():
    engine, _ = make_engine()
    task_id = assign(engine)
    assert engine.mark_notified(task_id) is not None
    assert engine.mark_notified(task_id) is None  # duplicate delivery callback


# ── employee replies ────────────────────────────────────────────────────

def test_started_reply_moves_to_in_progress():
    engine, _ = make_engine()
    task_id = assign(engine)
    engine.mark_notified(task_id)
    evt = engine.mark_started(task_id)
    assert engine.tasks[task_id].workflow_status == "in_progress"
    assert "in progress" in evt["message"]


def test_started_reply_before_notification_is_rejected():
    engine, _ = make_engine()
    task_id = assign(engine)  # still unassigned — no message ever sent
    assert engine.mark_started(task_id) is None


def test_extension_request_needs_supervisor_decision():
    engine, _ = make_engine()
    task_id = assign(engine)
    engine.mark_notified(task_id)
    engine.mark_started(task_id)
    evt = engine.request_extension(task_id)
    assert engine.tasks[task_id].workflow_status == "extension_requested"
    assert "awaiting supervisor decision" in evt["message"]


def test_review_request_flows():
    engine, _ = make_engine()
    task_id = assign(engine)
    engine.mark_notified(task_id)
    evt = engine.request_review(task_id)
    assert engine.tasks[task_id].workflow_status == "review_requested"
    assert "supervisor review" in evt["message"]


def test_replies_on_completed_task_are_rejected():
    engine, _ = make_engine()
    task_id = assign(engine)
    engine.mark_notified(task_id)
    engine.complete_task(task_id)
    assert engine.mark_started(task_id) is None
    assert engine.request_extension(task_id) is None
    assert engine.request_review(task_id) is None


# ── budget-expiry status nudge ──────────────────────────────────────────

def test_budget_expiry_emits_status_nudge_and_awaiting_update():
    engine, clock = make_engine()
    task_id = assign(engine, minutes=30)
    engine.mark_notified(task_id)
    engine.mark_started(task_id)

    clock.advance(31 * 60)  # past the 30-min budget
    events = engine.tick()

    nudges = [e for e in events if e["event_type"] == "task_status_nudge"]
    assert len(nudges) == 1
    assert "waiting for update from employee" in nudges[0]["message"].lower()
    assert engine.tasks[task_id].workflow_status == "awaiting_update"


def test_status_nudge_fires_only_once():
    engine, clock = make_engine()
    task_id = assign(engine, minutes=30)
    engine.mark_notified(task_id)
    clock.advance(31 * 60)
    engine.tick()
    clock.advance(10 * 60)
    events = engine.tick()
    assert [e for e in events if e["event_type"] == "task_status_nudge"] == []


def test_no_status_nudge_before_budget_elapses():
    engine, clock = make_engine()
    task_id = assign(engine, minutes=30)
    engine.mark_notified(task_id)
    clock.advance(20 * 60)  # only 20 of 30 min
    events = engine.tick()
    assert [e for e in events if e["event_type"] == "task_status_nudge"] == []


def test_no_status_nudge_for_never_notified_task():
    """If the assignee never got the message (notify_failed/unassigned),
    nudging them for a status would be nonsense — they don't know the
    task exists. The visible notify_failed state is the alert instead."""
    engine, clock = make_engine()
    task_id = assign(engine, minutes=30)
    engine.mark_notify_failed(task_id, "no phone")
    clock.advance(31 * 60)
    events = engine.tick()
    assert [e for e in events if e["event_type"] == "task_status_nudge"] == []


def test_late_start_reply_recovers_from_awaiting_update():
    engine, clock = make_engine()
    task_id = assign(engine, minutes=30)
    engine.mark_notified(task_id)
    clock.advance(31 * 60)
    engine.tick()  # -> awaiting_update
    evt = engine.mark_started(task_id)  # employee finally replies
    assert evt is not None
    assert engine.tasks[task_id].workflow_status == "in_progress"


# ── supervisor actions ──────────────────────────────────────────────────

def test_extend_task_grows_budget_and_rearms_nudge():
    engine, clock = make_engine()
    task_id = assign(engine, minutes=30)
    engine.mark_notified(task_id)
    clock.advance(31 * 60)
    engine.tick()  # nudge fired, awaiting_update
    evt = engine.extend_task(task_id, 15, supervisor_id="super1")
    t = engine.tasks[task_id]
    assert t.assigned_minutes == 45
    assert t.workflow_status == "in_progress"
    assert t.status_nudge_sent is False  # re-armed for the new deadline
    assert "extended by 15" in evt["message"]

    clock.advance(15 * 60)  # now past the NEW 45-min deadline
    events = engine.tick()
    assert len([e for e in events if e["event_type"] == "task_status_nudge"]) == 1


def test_extend_task_rejects_nonpositive_minutes():
    engine, _ = make_engine()
    task_id = assign(engine)
    assert engine.extend_task(task_id, 0) is None
    assert engine.extend_task(task_id, -10) is None


def test_reassign_task_resets_workflow_for_new_assignee():
    engine, _ = make_engine()
    task_id = assign(engine, assigned_to="101")
    engine.mark_notified(task_id)
    evt = engine.reassign_task(task_id, "102", supervisor_id="super1")
    t = engine.tasks[task_id]
    assert t.assigned_to == "102"
    assert t.workflow_status == "unassigned"  # main.py re-runs the notification path
    assert "reassigned from employee 101 to employee 102" in evt["message"]


def test_complete_closes_workflow_dimension_too():
    engine, _ = make_engine()
    task_id = assign(engine)
    engine.mark_notified(task_id)
    engine.complete_task(task_id)
    assert engine.tasks[task_id].workflow_status == "completed"


# ── SMS reply-to-task resolution ────────────────────────────────────────

def test_resolve_single_open_task_needs_no_reference():
    engine, _ = make_engine()
    task_id = assign(engine, assigned_to="101")
    task, reason = engine.resolve_task_reference("101")
    assert task.task_id == task_id
    assert reason is None


def test_resolve_with_no_open_tasks_says_so():
    engine, _ = make_engine()
    task, reason = engine.resolve_task_reference("101")
    assert task is None
    assert "no open tasks" in reason.lower()


def test_resolve_multiple_open_tasks_requires_short_code():
    engine, _ = make_engine()
    id1 = assign(engine, assigned_to="101")
    id2 = assign(engine, assigned_to="101")

    task, reason = engine.resolve_task_reference("101")
    assert task is None
    assert engine.short_code(id1) in reason and engine.short_code(id2) in reason

    task, reason = engine.resolve_task_reference("101", engine.short_code(id2))
    assert task.task_id == id2
    assert reason is None


def test_resolve_short_code_is_case_insensitive():
    engine, _ = make_engine()
    task_id = assign(engine, assigned_to="101")
    code = engine.short_code(task_id).lower()
    task, _ = engine.resolve_task_reference("101", code)
    assert task.task_id == task_id


def test_resolve_wrong_code_gives_clear_error():
    engine, _ = make_engine()
    assign(engine, assigned_to="101")
    task, reason = engine.resolve_task_reference("101", "ZZZZZZ")
    assert task is None
    assert "no open task matches" in reason.lower()


def test_resolve_never_matches_another_employees_task():
    engine, _ = make_engine()
    other_task = assign(engine, assigned_to="102")
    task, reason = engine.resolve_task_reference("101", engine.short_code(other_task))
    assert task is None  # 101 can't act on 102's task even with the right code