"""Security alerts — restricted areas, closed hours, break-ins and people hiding inside.

The cameras (floorwatch-coverage) only report facts: "a person is / is no longer in zone X". This module
applies the policy that turns some of those facts into alerts, so what counts as suspicious can be changed
from the dashboard (zone settings, opening hours, disarm) without touching a camera.

Each zone has two security settings (see zone_directory.py):

  access        open          people may be here any time — never alerts (the default; nothing changes
                              for a deployment until someone opts a zone in)
                closed_hours  nobody should be here while the venue is closed
                restricted    nobody should be here, ever (a safe, a server room, a cash office)
  entry_point   door | window | "" — a way into the building. Presence here while the venue is closed
                is treated as a possible break-in, whatever the zone's `access` is.

and the venue has opening hours (security_schedule.py). While it is *armed* — closed, and past the
closing grace period — these alerts exist:

  break_in_door / break_in_window   a person appears at a door or window zone            critical
  after_hours_presence              a person appears in a closed_hours zone              high
  restricted_zone_entry             a person appears in a restricted zone (any time)     high
  hiding_after_close                someone was already inside when the venue armed and  high
                                    is still there — likely hiding, or locked in

Anything else that happens while an unresolved break-in is open is raised to critical: an intruder who got
in through a door is the same incident as the person now in the stockroom.

Alerts are de-duplicated per (zone, kind), repeated while unresolved (so a missed push is not the end of
it), and stay open until a human acknowledges or resolves them — they are never auto-dismissed as "probably
fine". A supervisor can *disarm* for a bounded time (a cleaning crew, an early delivery) and everything is
audited as events.

What this is not: it cannot tell who the person is (no faces, by design — see the privacy notice), and a
detection is a prompt for a person to look at the cameras, not proof of anything. Messages say "possible".
Nothing here takes any action against anyone; it notifies, and humans decide.

State (open alerts, disarm) lives on the leader only, like the other engines, and is mirrored to a shared
snapshot so a new leader picks the open alerts back up (see load_state)."""

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from config import SOURCE_MODEL_VERSION
from security_schedule import OpenHours

ACCESS_VALUES = ("open", "closed_hours", "restricted")
ENTRY_POINT_VALUES = ("", "door", "window")
OUTCOMES = ("false_alarm", "authorised", "handled")

KIND_LABELS = {
    "break_in_door": "Possible break-in (door)",
    "break_in_window": "Possible break-in (window)",
    "after_hours_presence": "Person in the venue while closed",
    "restricted_zone_entry": "Person in a restricted area",
    "hiding_after_close": "Person still inside after closing",
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _hhmm(ts: float, schedule: OpenHours) -> str:
    return schedule.local_now(datetime.fromtimestamp(ts, timezone.utc)).strftime("%H:%M")


@dataclass
class _Presence:
    occupied: bool = False
    since: float = 0.0          # when the CURRENT stay began, as far as we know
    last_seen: float = 0.0
    camera_id: str = ""
    role_tag: str = "security"
    confidence: float = 0.0
    arrival_missed: bool = False  # first heard of them via a heartbeat (e.g. after a restart)


class SecurityEngine:
    def __init__(self, zones_meta: dict, schedule: OpenHours, *, enabled: bool = True,
                 repeat_seconds: float = 600, max_repeats: int = 6, max_disarm_minutes: float = 720,
                 auto_close_hours: float = 24, resolved_kept: int = 50,
                 clock: Callable[[], float] = time.time):
        self.zones_meta = zones_meta
        self.schedule = schedule
        self.enabled = enabled
        self.repeat_seconds = repeat_seconds
        self.max_repeats = max_repeats
        self.max_disarm_minutes = max_disarm_minutes
        self.auto_close_seconds = auto_close_hours * 3600
        self.resolved_kept = resolved_kept
        self._clock = clock

        self.presence: dict[str, _Presence] = {}
        self.alerts: dict[str, dict] = {}        # open + acknowledged
        self.resolved: list[dict] = []           # most recent first, bounded
        self.disarmed_until: Optional[float] = None
        self.disarmed_by: Optional[str] = None
        self._was_armed: Optional[bool] = None
        self._armed_since: Optional[float] = None

    def set_zones(self, zones: dict):
        """Replace the zone settings this engine judges against (from the zone directory)."""
        self.zones_meta.clear()
        self.zones_meta.update(zones)

    def audit_event(self, zone_id: str, message: str) -> dict:
        """A durable record of a security-relevant configuration change (who weakened or tightened what)."""
        return self._event("security_mode_changed", zone_id, message=message)

    # ── armed? ───────────────────────────────────────────────────────────

    def _now_dt(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), timezone.utc)

    def is_disarmed(self) -> bool:
        return self.disarmed_until is not None and self._clock() < self.disarmed_until

    def is_armed(self) -> bool:
        """Closed hours, past the grace period, and nobody has disarmed it."""
        return self.enabled and not self.is_disarmed() and self.schedule.is_armed(self._now_dt())

    def phase(self) -> str:
        if not self.enabled:
            return "off"
        if self.is_disarmed():
            return "disarmed"
        return self.schedule.phase(self._now_dt())

    # ── event construction ───────────────────────────────────────────────

    def _zone_label(self, zone_id: str) -> str:
        return (self.zones_meta.get(zone_id) or {}).get("name", zone_id)

    def _event(self, event_type: str, zone_id: str, *, alert: Optional[dict] = None, message: str = "",
               notify: bool = False, resolved_by: Optional[str] = None, camera_id: str = "",
               role_tag: str = "security", confidence: float = 1.0) -> dict:
        evt = {
            "event_id": str(uuid.uuid4()),
            "timestamp": _iso(self._clock()),
            "camera_id": (alert or {}).get("camera_id") or camera_id,
            "zone_id": zone_id,
            "role_tag": (alert or {}).get("role_tag") or role_tag,
            "entity_ref": None,
            "event_type": event_type,
            "task_id": None, "active_minutes": None, "assigned_minutes": None,
            "confidence": confidence,
            "source_model_version": SOURCE_MODEL_VERSION,
            "tier": None,
            "action_type": {"security_alert": "alert", "security_alert_update": "update",
                            "security_alert_resolved": "resolved",
                            "security_mode_changed": "mode"}.get(event_type),
            "message": message,
            "zone_name": self._zone_label(zone_id),
            "resolved_by": resolved_by,
        }
        if alert is not None:
            evt.update({"alert_id": alert["alert_id"], "alert_kind": alert["kind"], "severity": alert["severity"]})
        if notify:
            evt["_notify"] = True       # main.py strips this and sends the real notification
        return evt

    # ── message wording (plain, hedged: a detection is a prompt to look, not proof) ──

    def _describe(self, kind: str, zone_id: str, since: float, *, already_there: bool,
                  after_break_in: bool) -> str:
        zone = self._zone_label(zone_id)
        if kind in ("break_in_door", "break_in_window"):
            what = "door" if kind.endswith("door") else "window"
            text = f"Possible break-in: a person was detected at {zone} ({what}) while the venue is closed."
        elif kind == "hiding_after_close":
            text = (f"Someone is still inside {zone} after closing (there since "
                    f"{_hhmm(since, self.schedule)}). They may be hiding.")
        elif kind == "restricted_zone_entry":
            text = f"A person was detected in {zone}, a restricted area."
        else:
            text = f"A person was detected in {zone} while the venue is closed."
        if already_there and kind != "hiding_after_close":
            text += " They were already there when this service started watching."
        if after_break_in and not kind.startswith("break_in"):
            text = "After a possible break-in: " + text
        return text

    # ── raising alerts ───────────────────────────────────────────────────

    def _active_alert_for(self, zone_id: str, kind: str) -> Optional[dict]:
        for a in self.alerts.values():
            if a["zone_id"] == zone_id and a["kind"] == kind:
                return a
        return None

    def _break_in_open(self) -> bool:
        return any(a["kind"].startswith("break_in") for a in self.alerts.values())

    def _raise(self, kind: str, severity: str, zone_id: str, pres: _Presence, meta: dict) -> dict:
        now = self._clock()
        after_break_in = self._break_in_open() and not kind.startswith("break_in")
        if after_break_in:
            severity = "critical"
        alert = {
            "alert_id": str(uuid.uuid4()), "kind": kind, "severity": severity, "zone_id": zone_id,
            "zone_name": self._zone_label(zone_id), "camera_id": pres.camera_id, "role_tag": pres.role_tag,
            "entry_point": meta.get("entry_point") or "", "access": meta.get("access") or "open",
            "status": "open", "created_at": _iso(now), "created_ts": now, "since": _iso(pres.since),
            "still_present": True, "last_notified_ts": now, "repeats": 0, "detections": 1,
            "acknowledged_by": None, "resolved_by": None, "outcome": None,
        }
        alert["message"] = self._describe(kind, zone_id, pres.since, already_there=pres.arrival_missed,
                                          after_break_in=after_break_in)
        self.alerts[alert["alert_id"]] = alert
        return self._event("security_alert", zone_id, alert=alert, message=alert["message"], notify=True,
                           confidence=pres.confidence or 1.0)

    def _classify(self, zone_id: str, meta: dict, pres: _Presence) -> Optional[tuple]:
        """(kind, severity) if this presence breaks a rule right now, else None."""
        access = meta.get("access") or "open"
        entry = meta.get("entry_point") or ""
        armed = self.is_armed()
        was_inside_at_arming = (armed and self._armed_since is not None and pres.since < self._armed_since)

        if armed and entry in ("door", "window") and not was_inside_at_arming:
            return f"break_in_{entry}", "critical"
        if access == "restricted":
            return "restricted_zone_entry", "high"
        if armed and (access == "closed_hours" or entry):
            if was_inside_at_arming:
                return "hiding_after_close", "high"
            if access == "closed_hours":
                return "after_hours_presence", "high"
        return None

    def process_presence_event(self, evt: dict) -> list:
        """evt: a validated zone_person_present / zone_person_cleared event from the coverage skill."""
        if not self.enabled:
            return []
        zone_id = evt.get("zone_id")
        meta = self.zones_meta.get(zone_id)
        if meta is None:
            return []                          # unknown or deactivated zone: nothing to judge it against
        now = self._clock()
        pres = self.presence.setdefault(zone_id, _Presence())

        if evt.get("event_type") == "zone_person_cleared":
            if not pres.occupied:
                return []
            pres.occupied = False
            out = []
            for a in self.alerts.values():
                if a["zone_id"] == zone_id and a["still_present"]:
                    a["still_present"] = False
                    out.append(self._event(
                        "security_alert_update", zone_id, alert=a, notify=False,
                        message=f"No one is detected in {a['zone_name']} any more. This alert stays open until "
                                f"someone confirms it."))
            return out

        # zone_person_present
        newly = not pres.occupied
        pres.occupied = True
        pres.last_seen = now
        pres.camera_id = evt.get("camera_id") or pres.camera_id
        pres.role_tag = evt.get("role_tag") or pres.role_tag
        pres.confidence = float(evt.get("confidence") or 0.0)
        if newly:
            pres.since = now
            pres.arrival_missed = (evt.get("action_type") == "heartbeat")

        verdict = self._classify(zone_id, meta, pres)
        if verdict is None:
            return []
        kind, severity = verdict
        existing = self._active_alert_for(zone_id, kind)
        if existing is None:
            return [self._raise(kind, severity, zone_id, pres, meta)]
        existing["detections"] = existing.get("detections", 1) + (1 if newly else 0)
        if newly and not existing["still_present"]:
            existing["still_present"] = True
            noisy = existing["status"] == "open"
            if noisy:
                existing["last_notified_ts"] = now
            return [self._event("security_alert_update", zone_id, alert=existing, notify=noisy,
                                message=f"A person was detected again in {existing['zone_name']}.")]
        return []

    # ── time-driven work: arming transition, repeats, auto-close, disarm expiry ──

    def tick(self) -> list:
        if not self.enabled:
            return []
        out = []
        now = self._clock()

        if self.disarmed_until is not None and now >= self.disarmed_until:
            who = self.disarmed_by or "someone"
            self.disarmed_until = None
            self.disarmed_by = None
            out.append(self._event("security_mode_changed", "venue", notify=True,
                                   message=f"Alarm re-armed automatically (the disarm set by {who} ran out)."))

        armed = self.is_armed()
        if self._was_armed is None:
            self._was_armed = armed                  # first look after a start: nothing to compare with
        elif armed and not self._was_armed:
            self._armed_since = now
            out.extend(self._check_for_people_left_inside())
        elif not armed and self._was_armed:
            self._armed_since = None
        self._was_armed = armed

        for a in list(self.alerts.values()):
            age = now - a["created_ts"]
            if age >= self.auto_close_seconds:
                self.alerts.pop(a["alert_id"], None)
                a.update(status="resolved", resolved_by="auto", outcome="expired", resolved_at=_iso(now))
                self._remember(a)
                out.append(self._event("security_alert_resolved", a["zone_id"], alert=a, resolved_by="auto",
                                       message=f"Closed automatically after {self.auto_close_seconds / 3600:.0f} "
                                               f"hours without a response."))
                continue
            if (a["status"] == "open" and a["still_present"] and a["repeats"] < self.max_repeats
                    and now - a["last_notified_ts"] >= self.repeat_seconds):
                a["repeats"] += 1
                a["last_notified_ts"] = now
                out.append(self._event(
                    "security_alert_update", a["zone_id"], alert=a, notify=True,
                    message=f"Still unresolved after {max(1, round(age / 60))} min and the person is still detected: "
                            f"{a['message']}"))
        return out

    def _check_for_people_left_inside(self) -> list:
        out = []
        for zone_id, pres in self.presence.items():
            if not pres.occupied:
                continue
            meta = self.zones_meta.get(zone_id)
            if meta is None:
                continue
            access = meta.get("access") or "open"
            if access == "open" and not meta.get("entry_point"):
                continue                       # people are allowed there at any time
            if access == "restricted":
                continue                       # already alerts the moment anyone is seen there
            if self._active_alert_for(zone_id, "hiding_after_close") is not None:
                continue
            out.append(self._raise("hiding_after_close", "high", zone_id, pres, meta))
        return out

    # ── human actions ────────────────────────────────────────────────────

    def acknowledge(self, alert_id: str, user: str) -> Optional[dict]:
        """Someone says "I'm on it". Stops the repeat notifications; the alert stays open."""
        a = self.alerts.get(alert_id)
        if a is None:
            return None
        if a["status"] == "open":
            a["status"] = "acknowledged"
            a["acknowledged_by"] = user
            a["acknowledged_at"] = _iso(self._clock())
        return self._event("security_alert_update", a["zone_id"], alert=a, resolved_by=None,
                           message=f"Acknowledged by {user}.")

    def resolve(self, alert_id: str, user: str, outcome: str, note: str = "") -> Optional[dict]:
        a = self.alerts.pop(alert_id, None)
        if a is None:
            return None
        a.update(status="resolved", resolved_by=user, outcome=outcome, note=note, resolved_at=_iso(self._clock()))
        self._remember(a)
        label = {"false_alarm": "false alarm", "authorised": "authorised presence",
                 "handled": "handled"}.get(outcome, outcome)
        return self._event("security_alert_resolved", a["zone_id"], alert=a, resolved_by=user,
                           message=f"Closed by {user}: {label}." + (f" {note}" if note else ""))

    def _remember(self, alert: dict):
        self.resolved.insert(0, alert)
        del self.resolved[self.resolved_kept:]

    def disarm(self, minutes: float, user: str, reason: str = "") -> Optional[dict]:
        minutes = max(1.0, min(float(minutes), self.max_disarm_minutes))
        self.disarmed_until = self._clock() + minutes * 60
        self.disarmed_by = user
        until = _hhmm(self.disarmed_until, self.schedule)
        # Disarming also drops the "already inside" baseline: whoever is in there now is who the
        # supervisor is vouching for; people still inside when the disarm ends are checked again then.
        self._armed_since = None
        return self._event("security_mode_changed", "venue", notify=True,
                           message=f"Alarm disarmed by {user} until {until} ({minutes:.0f} min)"
                                   + (f": {reason}" if reason else "") + ".")

    def arm(self, user: str) -> Optional[dict]:
        if self.disarmed_until is None:
            return None
        self.disarmed_until = None
        self.disarmed_by = None
        return self._event("security_mode_changed", "venue", notify=True,
                           message=f"Alarm re-armed by {user}.")

    # ── views and persistence ────────────────────────────────────────────

    def active_alerts(self) -> list:
        order = {"critical": 0, "high": 1, "medium": 2}
        return sorted((dict(a) for a in self.alerts.values()),
                      key=lambda a: (order.get(a["severity"], 9), -a["created_ts"]))

    def status(self) -> dict:
        occupied = [{"zone_id": z, "zone_name": self._zone_label(z), "since": _iso(p.since)}
                    for z, p in self.presence.items() if p.occupied and z in self.zones_meta]
        return {
            "enabled": self.enabled,
            "phase": self.phase(),
            "armed": self.is_armed(),
            "disarmed_until": _iso(self.disarmed_until) if self.is_disarmed() else None,
            "disarmed_by": self.disarmed_by if self.is_disarmed() else None,
            "schedule": self.schedule.describe(),
            "alerts": self.active_alerts(),
            "recently_resolved": [dict(a) for a in self.resolved[:10]],
            "occupied_zones": occupied,
        }

    def snapshot(self) -> dict:
        return {"status": self.status(), "state": {
            "alerts": [dict(a) for a in self.alerts.values()],
            "disarmed_until": self.disarmed_until, "disarmed_by": self.disarmed_by,
        }}

    def load_state(self, snapshot: dict):
        """A new leader picks up where the last one left off, so a redeploy does not make open alerts vanish."""
        state = (snapshot or {}).get("state") or {}
        for a in state.get("alerts", []):
            if isinstance(a, dict) and a.get("alert_id") and a.get("created_ts") is not None:
                self.alerts.setdefault(a["alert_id"], a)
        du = state.get("disarmed_until")
        if isinstance(du, (int, float)) and du > self._clock():
            self.disarmed_until, self.disarmed_by = du, state.get("disarmed_by")
