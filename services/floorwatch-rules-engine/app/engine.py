"""Floorwatch coverage rules engine — the Part B 3-tier escalation state
machine described in the build brief's Phase 2 section:

  Tier 1: on zone_gap, start a nudge timer (3 min), cap 3 nudges/shift.
  Tier 2: on nudge timeout, OR on the 3rd+ gap in a shift (bypass),
          issue a supervisor-command event (5 min timer, throttled).
  Tier 3: on command timeout, mark logged_escalated, write to shift digest.
  15-minute per-zone resolve cooldown after any resolution.

Global Constraint 1 (no automatic discipline/HR action) — this engine only
ever produces nudges, drafted supervisor messages, and logged flags. A
Tier 2 supervisor-command event is a DRAFT requiring human approval via
`approve()`/`reassign()`; nothing here sends anything to a real channel —
that's the Phase 4 notification integration, gated by shadow_mode here.

Global Constraint 5 (roster cross-check) — enforced as a hard precondition
before ANY nudge or command; see `Roster.is_zone_staffed`.

Phase 2 known approximation (documented, not silently faked): nudge caps
and command throttling are scoped per-ZONE, not per-employee/per-supervisor,
because Phase 1/2 detections only carry per-frame anonymous entity_refs,
not persistent cross-frame identity. Real per-employee/per-supervisor
scoping needs the re-ID work called out for Phase 3+ in PHASE_1_NOTES.md.
"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from config import (
    COMMAND_THROTTLE_PER_HOUR,
    COMMAND_TIMER_SECONDS,
    NUDGE_CAP_PER_SHIFT,
    NUDGE_TIMER_SECONDS,
    RESOLVE_COOLDOWN_SECONDS,
    SOURCE_MODEL_VERSION,
)
from digest_store import DigestStore
from roster import Roster


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ZoneRuntime:
    camera_id: str = ""
    role_tag: str = "concession"
    status: str = "covered"  # covered | gap | nudge | command | escalated
    nudge_deadline: Optional[float] = None
    command_deadline: Optional[float] = None
    nudge_count_shift: int = 0
    command_timestamps_hour: list = field(default_factory=list)
    resolve_cooldown_until: Optional[float] = None
    last_entity_ref: Optional[str] = None
    last_command_message: Optional[str] = None


class RulesEngine:
    def __init__(self, roster: Roster, digest: DigestStore, zones_meta: dict,
                 shadow_mode: bool = True,
                 nudge_timer_seconds: float = NUDGE_TIMER_SECONDS,
                 nudge_cap_per_shift: int = NUDGE_CAP_PER_SHIFT,
                 command_timer_seconds: float = COMMAND_TIMER_SECONDS,
                 command_throttle_per_hour: int = COMMAND_THROTTLE_PER_HOUR,
                 resolve_cooldown_seconds: float = RESOLVE_COOLDOWN_SECONDS,
                 on_notify: Optional[Callable[[str, dict], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.roster = roster
        self.digest = digest
        self.zones_meta = zones_meta
        self.shadow_mode = shadow_mode
        self.nudge_timer_seconds = nudge_timer_seconds
        self.nudge_cap_per_shift = nudge_cap_per_shift
        self.command_timer_seconds = command_timer_seconds
        self.command_throttle_per_hour = command_throttle_per_hour
        self.resolve_cooldown_seconds = resolve_cooldown_seconds
        self.on_notify = on_notify or (lambda channel, payload: None)
        self._clock = clock  # injectable for deterministic tests

        self.zones: dict[str, ZoneRuntime] = {}

    def _zone(self, zone_id: str) -> ZoneRuntime:
        if zone_id not in self.zones:
            self.zones[zone_id] = ZoneRuntime()
        return self.zones[zone_id]

    def _zone_label(self, zone_id: str) -> str:
        return self.zones_meta.get(zone_id, {}).get("name", zone_id)

    def _in_cooldown(self, z: ZoneRuntime) -> bool:
        return z.resolve_cooldown_until is not None and self._clock() < z.resolve_cooldown_until

    def _command_throttled(self, z: ZoneRuntime) -> bool:
        cutoff = self._clock() - 3600
        z.command_timestamps_hour = [t for t in z.command_timestamps_hour if t > cutoff]
        return len(z.command_timestamps_hour) >= self.command_throttle_per_hour

    def _base_event(self, camera_id: str, zone_id: str, role_tag: str, event_type: str,
                     tier: Optional[int] = None, entity_ref: Optional[str] = None,
                     action_type: Optional[str] = None, message: Optional[str] = None,
                     confidence: float = 1.0) -> dict:
        return {
            "event_id": str(uuid.uuid4()),
            "timestamp": _now_iso(),
            "camera_id": camera_id,
            "zone_id": zone_id,
            "role_tag": role_tag,
            "entity_ref": entity_ref,
            "event_type": event_type,
            "task_id": None,
            "active_minutes": None,
            "assigned_minutes": None,
            "confidence": confidence,
            "source_model_version": SOURCE_MODEL_VERSION,
            "tier": tier,
            "action_type": action_type,
            "message": message,
            "zone_name": self._zone_label(zone_id),
            "resolved_by": None,
        }

    # ── inbound: detection-layer events from the coverage skill ─────────

    def process_detection_event(self, evt: dict) -> list:
        """evt is a validated shared-schema event (zone_covered | zone_gap)."""
        zone_id = evt["zone_id"]
        camera_id = evt["camera_id"]
        role_tag = evt["role_tag"]
        z = self._zone(zone_id)
        z.camera_id = camera_id
        z.role_tag = role_tag

        out = []

        if evt["event_type"] == "zone_covered":
            z.last_entity_ref = evt.get("entity_ref")
            if z.status != "covered":
                was_escalated_tier = {"nudge": 1, "command": 2, "escalated": 3}.get(z.status)
                z.status = "covered"
                z.nudge_deadline = None
                z.command_deadline = None
                z.resolve_cooldown_until = self._clock() + self.resolve_cooldown_seconds
                out.append(self._base_event(
                    camera_id, zone_id, role_tag, "zone_resolved",
                    tier=was_escalated_tier, entity_ref=z.last_entity_ref,
                    action_type="resolved", message=None,
                ))
                out[-1]["resolved_by"] = "auto"
            # else: already covered, nothing to do — this is the common case
            # ("no supervisor ever sees this one" per the demo script).
            return out

        if evt["event_type"] == "zone_gap":
            if z.status != "covered":
                return out  # already mid-escalation or logged; debounce already happened upstream

            if not self.roster.is_zone_staffed(zone_id):
                # Hard precondition (Global Constraint 5) — log only, never nudge.
                self.digest.append(self._base_event(
                    camera_id, zone_id, role_tag, "zone_gap",
                    action_type="ignored_unstaffed", confidence=evt.get("confidence", 1.0),
                ))
                return out

            if self._in_cooldown(z):
                return out  # just resolved recently — avoid flapping

            z.status = "gap"
            if z.nudge_count_shift >= self.nudge_cap_per_shift:
                # Bypass straight to Tier 2 per brief: "on the 3rd+ gap in a
                # shift (bypass), issue a supervisor-command event."
                out.extend(self._issue_command(z, camera_id, zone_id, role_tag))
            else:
                out.extend(self._issue_nudge(z, camera_id, zone_id, role_tag))
            return out

        return out  # other event types (task_*) are Part A — out of scope here

    def _issue_nudge(self, z: ZoneRuntime, camera_id, zone_id, role_tag) -> list:
        z.status = "nudge"
        z.nudge_count_shift += 1
        z.nudge_deadline = self._clock() + self.nudge_timer_seconds
        message = f"{self._zone_label(zone_id)} needs coverage — please head back when you can."
        evt = self._base_event(camera_id, zone_id, role_tag, "zone_nudge_sent",
                                tier=1, entity_ref=z.last_entity_ref,
                                action_type="nudge", message=message)
        self._deliver(evt, channel="employee_nudge")
        return [evt]

    def _issue_command(self, z: ZoneRuntime, camera_id, zone_id, role_tag) -> list:
        z.status = "command"
        z.command_deadline = self._clock() + self.command_timer_seconds
        throttled = self._command_throttled(z)
        z.command_timestamps_hour.append(self._clock())
        message = (f"{self.zones_meta.get(zone_id, {}).get('role_tag', role_tag).title()} — "
                    f"{self._zone_label(zone_id)} needs coverage now. Confirm on arrival.")
        z.last_command_message = message
        evt = self._base_event(camera_id, zone_id, role_tag, "zone_supervisor_command",
                                tier=2, action_type="command", message=message)
        evt["throttled"] = throttled
        # A drafted command still requires human approval before it's ever
        # actually sent — see approve()/reassign(). We only broadcast the
        # draft to the supervisor queue here, never auto-send.
        return [evt]

    def pending_commands(self) -> list:
        """Zones currently sitting at Tier 2, awaiting human approve/reassign —
        used to rehydrate the dashboard's supervisor queue on load/reconnect."""
        return [
            {
                "zone_id": zone_id,
                "zone_name": self._zone_label(zone_id),
                "role_tag": z.role_tag,
                "message": z.last_command_message,
            }
            for zone_id, z in self.zones.items()
            if z.status in ("command", "escalated")
        ]

    # ── timer expiry — called from the engine's periodic tick ───────────

    def tick(self) -> list:
        out = []
        now = self._clock()
        for zone_id, z in self.zones.items():
            if z.status == "nudge" and z.nudge_deadline is not None and now >= z.nudge_deadline:
                out.extend(self._issue_command(z, z.camera_id, zone_id, z.role_tag))
            elif z.status == "command" and z.command_deadline is not None and now >= z.command_deadline:
                z.status = "escalated"
                z.command_deadline = None
                evt = self._base_event(z.camera_id, zone_id, z.role_tag, "zone_escalated",
                                        tier=3, action_type="escalated",
                                        message=f"{self._zone_label(zone_id)} — unresolved after supervisor command timeout.")
                self.digest.append(evt)
                out.append(evt)
        return out

    # ── inbound: human-approved supervisor actions (dashboard REST calls) ──

    def approve(self, zone_id: str, supervisor_id: str = "supervisor") -> Optional[dict]:
        """Human approves the drafted Tier 2 directive — THIS is what would
        actually send it in live mode; shadow_mode still suppresses the send."""
        z = self.zones.get(zone_id)
        if z is None or z.status not in ("command", "nudge", "gap", "escalated"):
            return None
        message = f"Directive approved and sent for {self._zone_label(zone_id)}."
        evt = self._base_event(z.camera_id, zone_id, z.role_tag, "zone_resolved",
                                tier=2, action_type="resolved", message=message)
        evt["resolved_by"] = f"supervisor:{supervisor_id}"
        self._deliver(evt, channel="supervisor_directive_send")
        z.status = "covered"
        z.command_deadline = None
        z.nudge_deadline = None
        z.resolve_cooldown_until = self._clock() + self.resolve_cooldown_seconds
        return evt

    def reassign(self, zone_id: str, supervisor_id: str = "supervisor") -> Optional[dict]:
        """Human resolves coverage manually (no directive sent)."""
        z = self.zones.get(zone_id)
        if z is None:
            return None
        evt = self._base_event(z.camera_id, zone_id, z.role_tag, "zone_resolved",
                                tier=None, action_type="resolved",
                                message=f"Supervisor manually reassigned coverage for {self._zone_label(zone_id)}.")
        evt["resolved_by"] = f"supervisor:{supervisor_id}"
        z.status = "covered"
        z.command_deadline = None
        z.nudge_deadline = None
        z.resolve_cooldown_until = self._clock() + self.resolve_cooldown_seconds
        return evt

    def _deliver(self, evt: dict, channel: str):
        """Global Constraint 4 — shadow mode suppresses real sends; always logs."""
        if self.shadow_mode:
            evt["shadow_mode_suppressed"] = True
        else:
            evt["shadow_mode_suppressed"] = False
            self.on_notify(channel, evt)
