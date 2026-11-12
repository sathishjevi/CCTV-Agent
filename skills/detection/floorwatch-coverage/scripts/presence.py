"""Per-zone PRESENCE tracking — the fast half of the signal the security rules need.

Why this is separate from debounce.py: coverage debouncing is built to be slow and forgiving. It
waits 60 seconds before it will say a zone is empty, and it only emits on a change. Security needs
the opposite — "a person has just appeared at the back door" must be known within seconds — and it
also needs to hear, repeatedly, that someone is *still* there (to tell a person who walked through
from one who is hiding, and to recover after a service restart that wiped the rules engine's memory).

So this tracker emits three things per (camera, zone):

  zone_person_present  action_type="arrived"    a person has been confidently seen for a few seconds
  zone_person_present  action_type="heartbeat"  still there; repeated every `heartbeat_seconds`
  zone_person_cleared  action_type="cleared"    nobody seen for `clear_seconds`

These readings are anonymous (a zone, a time and a confidence — no identity, no image) and carry no
judgement. Whether presence is *allowed* (opening hours, restricted rooms, door/window zones) is
policy the rules engine applies, so operators can change it from the dashboard without recalibrating
cameras.

Timing uses the detections' own timestamps (like debounce.py), so replayed footage behaves the same
as live footage and tests can drive the clock directly."""

from datetime import datetime, timezone
from typing import Optional


def _parse_ts(ts: str) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


class ZonePresence:
    """State for one (camera_id, zone_id)."""

    def __init__(self, arrive_seconds: float, clear_seconds: float, heartbeat_seconds: float,
                 min_confidence: float):
        self.arrive_seconds = arrive_seconds
        self.clear_seconds = clear_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.min_confidence = min_confidence
        self.present = False
        self._seen_since: Optional[datetime] = None     # start of the current run of confident sightings
        self._unseen_since: Optional[datetime] = None   # start of the current run of empty frames
        self._last_emit: Optional[datetime] = None

    def update(self, occupied: bool, confidence: float, entity_ref: Optional[str],
               timestamp: str) -> Optional[dict]:
        """Feed one observation; returns {"event_type", "action_type", "entity_ref", "confidence"} or None."""
        ts = _parse_ts(timestamp)
        confident = occupied and confidence >= self.min_confidence

        if confident:
            self._unseen_since = None
            if self._seen_since is None:
                self._seen_since = ts
            if not self.present:
                if (ts - self._seen_since).total_seconds() >= self.arrive_seconds:
                    self.present = True
                    self._last_emit = ts
                    return {"event_type": "zone_person_present", "action_type": "arrived",
                            "entity_ref": entity_ref, "confidence": confidence}
                return None
            if self._last_emit is None or (ts - self._last_emit).total_seconds() >= self.heartbeat_seconds:
                self._last_emit = ts
                return {"event_type": "zone_person_present", "action_type": "heartbeat",
                        "entity_ref": entity_ref, "confidence": confidence}
            return None

        # An empty (or low-confidence) frame. One missed frame must not cancel a person who is really
        # there (occlusion, a blink of the detector), so both the "arriving" run and the "present"
        # state tolerate a short gap: an arrival run is dropped after `arrive_seconds` of emptiness
        # (so two stray false positives minutes apart never add up to an arrival) and a person is only
        # declared gone after `clear_seconds`.
        if self._unseen_since is None:
            self._unseen_since = ts
        empty_for = (ts - self._unseen_since).total_seconds()
        if not self.present:
            if empty_for >= self.arrive_seconds:
                self._seen_since = None
            return None
        if empty_for >= self.clear_seconds:
            self.present = False
            self._seen_since = None
            self._last_emit = None
            return {"event_type": "zone_person_cleared", "action_type": "cleared",
                    "entity_ref": None, "confidence": 1.0}
        return None


class PresenceRegistry:
    """One ZonePresence per (camera_id, zone_id)."""

    def __init__(self, arrive_seconds: float = 3.0, clear_seconds: float = 30.0,
                 heartbeat_seconds: float = 30.0, min_confidence: float = 0.6):
        self.arrive_seconds = arrive_seconds
        self.clear_seconds = clear_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.min_confidence = min_confidence
        self._trackers: dict = {}

    def get(self, camera_id: str, zone_id: str) -> ZonePresence:
        key = (camera_id, zone_id)
        if key not in self._trackers:
            self._trackers[key] = ZonePresence(self.arrive_seconds, self.clear_seconds,
                                               self.heartbeat_seconds, self.min_confidence)
        return self._trackers[key]
