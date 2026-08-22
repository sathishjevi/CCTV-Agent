"""Floorwatch shared event schema — the ONE source of truth for the event
shape used across every phase (coverage, effort, rules engine, dashboard).
Never fork a parallel schema; extend this with optional fields instead.

Validated with Pydantic per the build brief ("Validate every event against
this schema at the point it's emitted... Reject and log malformed events
rather than passing them downstream.").
"""

import sys
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

RoleTag = Literal["concession", "usher", "janitor", "maintenance", "ticketing", "security"]

EventType = Literal[
    "zone_covered",
    "zone_gap",
    # Part B escalation tiers — emitted by the rules engine, not the coverage
    # skill itself. Extending the one shared schema per Global Constraint 6
    # ("never fork a parallel schema") rather than inventing a separate
    # dashboard-facing event shape.
    "zone_nudge_sent",
    "zone_supervisor_command",
    "zone_escalated",
    "zone_resolved",
    "task_assigned",
    "task_active_time_update",
    "task_low_effort_nudge",  # Part A mid-task nudge — reuses the Part B nudge delivery pattern
    "task_flag",
    "task_resolved",
    # A scene-condition detector's judgment call (e.g. "this display looks
    # messy") — distinct from zone_gap/zone_covered (occupancy readings):
    # routes straight to the existing primary-contact auto-assign path
    # rather than through the Part B occupancy state machine. task_name/
    # task_type carry the detector's suggestion; both fields already
    # existed on this schema for task_assigned, reused rather than adding
    # new ones.
    "scene_task_suggested",
]

Tier = Literal[1, 2, 3]


class FloorwatchEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    camera_id: str
    zone_id: str
    role_tag: RoleTag
    entity_ref: Optional[str] = None  # anonymous_track_id (default) | employee_id (only if escalated)
    event_type: EventType
    task_id: Optional[str] = None
    active_minutes: Optional[float] = None
    assigned_minutes: Optional[float] = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_model_version: str

    # Optional fields for the rules-engine / dashboard layer (Phase 2+).
    # Never required so Part B/A detection-layer producers are unaffected.
    tier: Optional[Tier] = None
    action_type: Optional[str] = None   # "nudge" | "supervisor_command" | "escalated" | "resolved"
    message: Optional[str] = None       # drafted nudge/supervisor directive text
    zone_name: Optional[str] = None     # human-readable label for dashboard display
    resolved_by: Optional[str] = None   # "auto" | "supervisor:<id>" — never an HR/employment action
    task_name: Optional[str] = None     # human-readable task label for dashboard display
    task_type: Optional[str] = None     # key into per-task-type effort thresholds (Phase 3)


def validate_event(data: dict, log=lambda msg: print(msg, file=sys.stderr, flush=True)) -> Optional[FloorwatchEvent]:
    """Validate a raw dict against the shared schema.

    Returns the validated FloorwatchEvent, or None (and logs) if malformed —
    callers must not pass malformed events downstream.
    """
    try:
        return FloorwatchEvent(**data)
    except ValidationError as e:
        log(f"[floorwatch_schema] REJECTED malformed event: {e}")
        return None
