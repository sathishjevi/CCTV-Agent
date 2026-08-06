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
    status: str = "open"  # open | flagged | resolved
    nudged: bool = False


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
                     task_type: Optional[str] = None, task_id: Optional[str] = None) -> Optional[dict]:
        """Returns None if the roster shows nobody assigned to this zone —
        Global Constraint 5 is a hard precondition, not just for nudges but
        for even opening a task window here."""
        if not self.roster.is_zone_staffed(zone_id):
            return None

        task_id = task_id or str(uuid.uuid4())
        t = TaskRuntime(
            task_id=task_id, task_name=task_name, task_type=task_type or task_name,
            zone_id=zone_id, camera_id=self._camera_for_zone(zone_id),
            assigned_minutes=assigned_minutes, start_monotonic=self._clock(),
        )
        self.tasks[task_id] = t
        return self._base_event(t, "task_assigned", active_minutes=0.0)

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
        return out

    # ── task completion (Phase 3 task 4) ──────────────────────────────────

    def complete_task(self, task_id: str) -> Optional[dict]:
        t = self.tasks.get(task_id)
        if t is None or t.status != "open":
            return None

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
