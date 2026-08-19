"""Floorwatch effort engine — the Part A active-time tracking and
completion-vs-effort flagging described in the build brief's Phase 3
section:

  - A supervisor assigns a task (name, zone, assigned_minutes budget).
  - Active time accrues per open task window from the pose/motion signal
    (floorwatch-pose), but ONLY while the task's zone is currently
    occupied per the coverage engine — motion in an empty zone isn't this
    task's effort.
  - Mid-task: if active-time ratio falls significantly behind elapsed-time
    ratio, send a low-effort nudge (reusing the same shadow-mode-gated
    delivery/suppression pattern as Part B's RulesEngine._deliver).
  - At completion: if active_minutes is far below assigned_minutes for
    that task's TYPE (not a single global threshold — see
    task_type_thresholds.json), emit task_flag — a supervisor review
    item (confirm/dismiss), never an automatic action.

Global Constraint 5 (roster cross-check) applies here too: a task can't be
assigned to an unstaffed zone, and a task nudge is still gated on the zone
being staffed at nudge time.

Phase 3 known approximation: `record_motion` receives a single per-camera
motion score (floorwatch-pose doesn't do per-person pose tracking in this
pilot — see that skill's SKILL.md), so if a zone has a task open AND is
independently occupied (Part B), any motion on that camera counts toward
that task's active time. Fine for a single-occupant-zone pilot; would need
per-person association before scaling to multi-occupant zones.
"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from config import SOURCE_MODEL_VERSION


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Workflow statuses (the assignment/communication dimension — who the
# task is assigned to and where the conversation with them stands),
# ORTHOGONAL to `status` below (the effort dimension — open tasks accrue
# active time; flagged/resolved are terminal). Two fields, not one
# merged enum, deliberately: the effort state machine and its
# flag-on-completion logic predate the workflow and stay untouched,
# while the workflow can move independently (e.g. a task can be
# `awaiting_update` in workflow terms while still `open` in effort
# terms and accruing active time).
#   unassigned          — no assignee (auto-assignment found nobody, or
#                          assigned_to never provided); supervisor queue
#                          surfaces these
#   notified            — assignment message sent to the assignee
#   notify_failed       — has an assignee but the message couldn't be
#                          sent (no phone on file / sender error) — the
#                          assignee does NOT know about this task
#   in_progress         — assignee replied START
#   awaiting_update     — budget elapsed with no completion status from
#                          the assignee; status-request nudge sent
#                          ("Pending — waiting for update from employee")
#   extension_requested — assignee replied MORE (needs a supervisor
#                          decision: extend or complete as-is)
#   review_requested    — assignee replied REVIEW (asked for a
#                          supervisor to look)
#   completed           — assignee replied DONE (or the dashboard's
#                          mark-complete was used); effort-side flag
#                          logic has run at this point
WORKFLOW_STATUSES = {
    "unassigned", "notified", "notify_failed", "in_progress",
    "awaiting_update", "extension_requested", "review_requested", "completed",
}


@dataclass
class TaskRuntime:
    task_id: str
    task_name: str
    task_type: str
    zone_id: str
    camera_id: Optional[str]
    assigned_minutes: float
    start_monotonic: float
    active_seconds: float = 0.0
    last_motion_monotonic: Optional[float] = None
    last_update_emit_monotonic: float = 0.0
    status: str = "open"  # open | flagged | resolved (effort dimension)
    nudged: bool = False
    # ── workflow dimension (see WORKFLOW_STATUSES above) ──
    assigned_to: Optional[str] = None      # employee_number from the directory
    assigned_by: Optional[str] = None      # "auto:<event_type>" or "user:<username>"
    workflow_status: str = "unassigned"
    status_nudge_sent: bool = False        # budget-expiry "what's the status?" nudge — once only


class EffortEngine:
    def __init__(self, roster, digest, zones_meta: dict, task_type_thresholds: dict,
                 shadow_mode: bool = True,
                 update_interval_seconds: float = 30,
                 max_motion_gap_seconds: float = 15,
                 nudge_grace_ratio: float = 0.3,
                 nudge_margin: float = 0.3,
                 zone_is_covered: Callable[[str], bool] = lambda zone_id: True,
                 on_notify: Optional[Callable[[str, dict], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.roster = roster
        self.digest = digest
        self.zones_meta = zones_meta
        self.task_type_thresholds = task_type_thresholds
        self.shadow_mode = shadow_mode
        self.update_interval_seconds = update_interval_seconds
        self.max_motion_gap_seconds = max_motion_gap_seconds
        self.nudge_grace_ratio = nudge_grace_ratio
        self.nudge_margin = nudge_margin
        self.zone_is_covered = zone_is_covered
        self.on_notify = on_notify or (lambda channel, payload: None)
        self._clock = clock

        self.tasks: dict[str, TaskRuntime] = {}

    def _zone_label(self, zone_id: str) -> str:
        return self.zones_meta.get(zone_id, {}).get("name", zone_id)

    def _role_tag(self, zone_id: str) -> str:
        return self.zones_meta.get(zone_id, {}).get("role_tag", "concession")

    def _camera_for_zone(self, zone_id: str) -> Optional[str]:
        return self.zones_meta.get(zone_id, {}).get("camera_id")

    def _threshold_for(self, task_type: str) -> float:
        entry = self.task_type_thresholds.get(task_type) or self.task_type_thresholds.get("_default", {})
        return entry.get("expected_active_ratio", 0.5)

    def _base_event(self, t: TaskRuntime, event_type: str, active_minutes: float,
                     action_type: Optional[str] = None, message: Optional[str] = None) -> dict:
        return {
            "event_id": str(uuid.uuid4()),
            "timestamp": _now_iso(),
            "camera_id": t.camera_id or "unknown",
            "zone_id": t.zone_id,
            "role_tag": self._role_tag(t.zone_id),
            "entity_ref": None,
            "event_type": event_type,
            "task_id": t.task_id,
            "active_minutes": round(active_minutes, 2),
            "assigned_minutes": t.assigned_minutes,
            "confidence": 1.0,
            "source_model_version": SOURCE_MODEL_VERSION,
            "tier": None,
            "action_type": action_type,
            "message": message,
            "zone_name": self._zone_label(t.zone_id),
            "resolved_by": None,
            "task_name": t.task_name,
            "task_type": t.task_type,
        }

    # ── inbound: task-assignment input (Phase 3 task 1) ──────────────────

    def assign_task(self, task_name: str, zone_id: str, assigned_minutes: float,
                     task_type: Optional[str] = None, task_id: Optional[str] = None,
                     assigned_to: Optional[str] = None,
                     assigned_by: Optional[str] = None) -> Optional[dict]:
        """Returns None if the roster shows nobody assigned to this zone —
        Global Constraint 5 is a hard precondition, not just for nudges but
        for even opening a task window here.

        assigned_to/assigned_by are the workflow dimension (see
        WORKFLOW_STATUSES): who the task belongs to and who put it there.
        The outbound notification itself happens in main.py (it needs the
        directory + sender, which this engine deliberately doesn't hold) —
        this only records intent; main.py then calls mark_notified()/
        mark_notify_failed() with the send outcome."""
        if not self.roster.is_zone_staffed(zone_id):
            return None

        task_id = task_id or str(uuid.uuid4())
        t = TaskRuntime(
            task_id=task_id, task_name=task_name, task_type=task_type or task_name,
            zone_id=zone_id, camera_id=self._camera_for_zone(zone_id),
            assigned_minutes=assigned_minutes, start_monotonic=self._clock(),
            assigned_to=assigned_to, assigned_by=assigned_by,
            workflow_status="unassigned",
        )
        self.tasks[task_id] = t
        evt = self._base_event(t, "task_assigned", active_minutes=0.0)
        evt["assigned_to"] = assigned_to
        evt["assigned_by"] = assigned_by
        return evt

    # ── workflow transitions (assignment/communication dimension) ────────
    #
    # Each returns an event dict (for _emit -> broadcast + durable
    # history) or None if the transition doesn't apply. All are
    # deliberately idempotent-ish: calling one twice, or on a task in the
    # wrong state, returns None instead of corrupting state — inbound SMS
    # replies can arrive duplicated or out of order.

    def _workflow_event(self, t, action_type: str, message: str) -> dict:
        evt = self._base_event(t, "task_workflow_update", t.active_seconds / 60.0,
                                action_type=action_type, message=message)
        evt["assigned_to"] = t.assigned_to
        evt["workflow_status"] = t.workflow_status
        return evt

    def mark_notified(self, task_id: str) -> Optional[dict]:
        t = self.tasks.get(task_id)
        if t is None or t.status != "open" or t.workflow_status not in ("unassigned", "notify_failed"):
            return None
        t.workflow_status = "notified"
        return self._workflow_event(t, "notified",
            f'Assignment message sent to employee {t.assigned_to} for "{t.task_name}" '
            f"({t.assigned_minutes:.0f} min allocated).")

    def mark_notify_failed(self, task_id: str, reason: str = "") -> Optional[dict]:
        t = self.tasks.get(task_id)
        if t is None or t.status != "open" or t.workflow_status != "unassigned":
            return None
        t.workflow_status = "notify_failed"
        return self._workflow_event(t, "notify_failed",
            f'Could not send assignment message for "{t.task_name}"'
            + (f" — {reason}" if reason else "") + ". The assignee does not know about this task.")

    def mark_started(self, task_id: str) -> Optional[dict]:
        """Assignee replied START (or equivalent acknowledgment)."""
        t = self.tasks.get(task_id)
        if t is None or t.status != "open" or t.workflow_status not in ("notified", "awaiting_update"):
            return None
        t.workflow_status = "in_progress"
        return self._workflow_event(t, "started",
            f'Employee {t.assigned_to} confirmed "{t.task_name}" is in progress.')

    def request_extension(self, task_id: str) -> Optional[dict]:
        """Assignee replied MORE — needs supervisor decision (extend or not)."""
        t = self.tasks.get(task_id)
        if t is None or t.status != "open":
            return None
        t.workflow_status = "extension_requested"
        return self._workflow_event(t, "extension_requested",
            f'Employee {t.assigned_to} needs more time on "{t.task_name}" — awaiting supervisor decision.')

    def request_review(self, task_id: str) -> Optional[dict]:
        """Assignee replied REVIEW — asked for a supervisor to look."""
        t = self.tasks.get(task_id)
        if t is None or t.status != "open":
            return None
        t.workflow_status = "review_requested"
        return self._workflow_event(t, "review_requested",
            f'Employee {t.assigned_to} requested supervisor review on "{t.task_name}".')

    def extend_task(self, task_id: str, extra_minutes: float,
                     supervisor_id: str = "supervisor") -> Optional[dict]:
        """Supervisor grants an extension — budget grows, workflow returns
        to in_progress, and the budget-expiry status nudge re-arms so the
        NEW deadline gets its own nudge if it too passes silently."""
        t = self.tasks.get(task_id)
        if t is None or t.status != "open" or extra_minutes <= 0:
            return None
        t.assigned_minutes += extra_minutes
        t.workflow_status = "in_progress"
        t.status_nudge_sent = False
        return self._workflow_event(t, "extended",
            f'"{t.task_name}" extended by {extra_minutes:.0f} min by supervisor:{supervisor_id} — '
            f"new budget {t.assigned_minutes:.0f} min.")

    def reassign_task(self, task_id: str, new_assignee: str,
                       supervisor_id: str = "supervisor") -> Optional[dict]:
        """Supervisor/admin moves the task to a different employee. Resets
        the workflow to unassigned so main.py re-runs the notification
        path for the new assignee (mark_notified/mark_notify_failed)."""
        t = self.tasks.get(task_id)
        if t is None or t.status != "open":
            return None
        previous = t.assigned_to
        t.assigned_to = new_assignee
        t.assigned_by = f"user:{supervisor_id}"
        t.workflow_status = "unassigned"
        t.status_nudge_sent = False
        return self._workflow_event(t, "reassigned",
            f'"{t.task_name}" reassigned from employee {previous or "nobody"} to '
            f"employee {new_assignee} by supervisor:{supervisor_id}.")

    # ── inbound: pose/motion signal (floorwatch-pose) ────────────────────

    def record_motion(self, camera_id: str, active: bool):
        now = self._clock()
        for t in self.tasks.values():
            if t.status != "open" or t.camera_id != camera_id:
                continue
            if t.last_motion_monotonic is None:
                t.last_motion_monotonic = now
                continue
            dt = min(now - t.last_motion_monotonic, self.max_motion_gap_seconds)
            t.last_motion_monotonic = now
            if active and self.zone_is_covered(t.zone_id):
                t.active_seconds += dt

    # ── periodic: active-time updates + mid-task nudge ───────────────────

    def tick(self) -> list:
        out = []
        now = self._clock()
        for t in self.tasks.values():
            if t.status != "open":
                continue

            elapsed_minutes = (now - t.start_monotonic) / 60.0
            active_minutes = t.active_seconds / 60.0

            if now - t.last_update_emit_monotonic >= self.update_interval_seconds:
                t.last_update_emit_monotonic = now
                out.append(self._base_event(t, "task_active_time_update", active_minutes))

            if not t.nudged and t.assigned_minutes > 0:
                elapsed_ratio = min(1.0, elapsed_minutes / t.assigned_minutes)
                active_ratio = active_minutes / t.assigned_minutes
                behind_by = elapsed_ratio - active_ratio
                if elapsed_ratio >= self.nudge_grace_ratio and behind_by >= self.nudge_margin:
                    if self.roster.is_zone_staffed(t.zone_id):  # re-checked at nudge time too
                        t.nudged = True
                        remaining = max(0, t.assigned_minutes - elapsed_minutes)
                        message = (f"{t.task_name} — {remaining:.0f} minutes left on this task, "
                                   f"active time is behind schedule.")
                        evt = self._base_event(t, "task_low_effort_nudge", active_minutes,
                                                action_type="nudge", message=message)
                        if self.shadow_mode:
                            evt["shadow_mode_suppressed"] = True
                        else:
                            evt["shadow_mode_suppressed"] = False
                            self.on_notify("employee_low_effort_nudge", evt)
                        out.append(evt)

            # ── workflow dimension: budget elapsed with no completion
            # status from the assignee — one status-request nudge, then
            # the task sits at awaiting_update ("Pending — waiting for
            # update from employee") until a reply or supervisor action.
            # Distinct from the low-effort nudge above (that's about
            # active-time falling behind MID-task; this is about the
            # deadline itself passing with silence). main.py sends the
            # actual SMS off this event, same as the assignment message.
            if (not t.status_nudge_sent and t.assigned_minutes > 0
                    and elapsed_minutes >= t.assigned_minutes
                    and t.workflow_status in ("notified", "in_progress")):
                t.status_nudge_sent = True
                t.workflow_status = "awaiting_update"
                evt = self._workflow_event(t, "status_nudge",
                    f'"{t.task_name}" allocated time ({t.assigned_minutes:.0f} min) has elapsed '
                    f"with no status from employee {t.assigned_to} — status update requested. "
                    f"Pending — waiting for update from employee.")
                evt["event_type"] = "task_status_nudge"
                out.append(evt)
        return out

    # ── task completion (Phase 3 task 4) ──────────────────────────────────

    def complete_task(self, task_id: str) -> Optional[dict]:
        t = self.tasks.get(task_id)
        if t is None or t.status != "open":
            return None

        t.workflow_status = "completed"
        active_minutes = t.active_seconds / 60.0
        threshold_ratio = self._threshold_for(t.task_type)
        expected_minutes = threshold_ratio * t.assigned_minutes

        if active_minutes < expected_minutes:
            t.status = "flagged"
            message = (f'"{t.task_name}" marked complete — only {active_minutes:.0f} of '
                       f"{t.assigned_minutes:.0f} assigned minutes showed active work "
                       f"(expected ≥{expected_minutes:.0f} min for this task type).")
            evt = self._base_event(t, "task_flag", active_minutes, action_type="flag", message=message)
            self.digest.append(evt)
            return evt

        t.status = "resolved"
        evt = self._base_event(t, "task_resolved", active_minutes, action_type="resolved")
        evt["resolved_by"] = "auto"
        return evt

    # ── lookups for the inbound-SMS webhook (main.py) ────────────────────

    @staticmethod
    def short_code(task_id: str) -> str:
        """Human-typeable task reference included in the assignment SMS
        (task_ids are UUIDs — nobody's typing one back on a phone). Last
        6 hex chars is unique enough across a floor's concurrently-open
        tasks; resolve_task_reference() below handles the reply side."""
        return task_id.replace("-", "")[-6:].upper()

    def open_tasks_for(self, employee_number: str) -> list:
        return [t for t in self.tasks.values()
                if t.status == "open" and t.assigned_to == employee_number]

    def resolve_task_reference(self, employee_number: str, reference: Optional[str] = None):
        """Maps an inbound reply back to ONE of the sender's open tasks.
        With a reference (short code from the assignment message), matches
        that. Without one: unambiguous only if they have exactly one open
        task. Returns (task, None) on success, (None, reason) otherwise —
        reason is human-readable and safe to text back."""
        open_tasks = self.open_tasks_for(employee_number)
        if not open_tasks:
            return None, "You have no open tasks."
        if reference:
            ref = reference.strip().upper()
            matches = [t for t in open_tasks if self.short_code(t.task_id) == ref]
            if not matches:
                return None, f"No open task matches code {ref}."
            return matches[0], None
        if len(open_tasks) == 1:
            return open_tasks[0], None
        codes = ", ".join(f"{self.short_code(t.task_id)} ({t.task_name})" for t in open_tasks)
        return None, f"You have multiple open tasks — reply with the task code too: {codes}"

    def pending_flags(self) -> list:
        return [
            {
                "task_id": t.task_id,
                "task_name": t.task_name,
                "zone_id": t.zone_id,
                "zone_name": self._zone_label(t.zone_id),
                "active_minutes": round(t.active_seconds / 60.0, 2),
                "assigned_minutes": t.assigned_minutes,
            }
            for t in self.tasks.values() if t.status == "flagged"
        ]

    def confirm_flag(self, task_id: str, supervisor_id: str = "supervisor") -> Optional[dict]:
        t = self.tasks.get(task_id)
        if t is None or t.status != "flagged":
            return None
        t.status = "resolved"
        evt = self._base_event(t, "task_resolved", t.active_seconds / 60.0,
                                action_type="confirmed",
                                message=f"Supervisor confirmed the effort flag on \"{t.task_name}\" — following up with the employee.")
        evt["resolved_by"] = f"supervisor:{supervisor_id}"
        return evt

    def dismiss_flag(self, task_id: str, supervisor_id: str = "supervisor") -> Optional[dict]:
        t = self.tasks.get(task_id)
        if t is None or t.status != "flagged":
            return None
        t.status = "resolved"
        evt = self._base_event(t, "task_resolved", t.active_seconds / 60.0,
                                action_type="dismissed",
                                message=f"Supervisor dismissed the effort flag on \"{t.task_name}\" — false alarm (e.g. off-camera prep work).")
        evt["resolved_by"] = f"supervisor:{supervisor_id}"
        return evt
