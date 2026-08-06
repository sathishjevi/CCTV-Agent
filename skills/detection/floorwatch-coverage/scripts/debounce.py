"""Per-zone debounce state machine — turns noisy frame-by-frame occupancy
into stable zone_covered / zone_gap transitions.

Phase 2 spec (build brief, Section "PHASE 2 — Part B: Coverage Engine",
task 1): "debounce (60s) and confidence threshold (>=0.8) applied before
emitting a zone_gap."

Design:
  - zone_covered fires immediately on any occupied=True frame that clears
    the confidence threshold, if the zone wasn't already covered — a real
    person showing up shouldn't wait 60s to register as coverage.
  - zone_gap only fires after the zone has been continuously unoccupied
    (or only seeing sub-threshold-confidence detections) for >= debounce
    window, to filter momentary detection misses/occlusion rather than a
    real gap.
  - Only ONE transition event is emitted per state change (not repeated
    every frame) — downstream (rules engine) tracks its own timers off
    that single event.
"""

from datetime import datetime, timezone
from typing import Optional


def _parse_ts(ts: str) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


class ZoneDebouncer:
    """Tracks debounce state for a single (camera_id, zone_id) pair."""

    def __init__(self, debounce_seconds: float = 60.0, min_confidence: float = 0.8):
        self.debounce_seconds = debounce_seconds
        self.min_confidence = min_confidence
        self.status: str = "unknown"  # "unknown" | "covered" | "gap"
        self._unoccupied_since: Optional[datetime] = None

    def update(self, occupied: bool, confidence: float, entity_ref: Optional[str], timestamp: str) -> Optional[dict]:
        """Feed one raw presence observation. Returns a transition dict
        {"event_type": "zone_covered"|"zone_gap", "entity_ref": ..., "confidence": ...}
        or None if no state transition occurred.
        """
        ts = _parse_ts(timestamp)
        confident_occupied = occupied and confidence >= self.min_confidence

        if confident_occupied:
            self._unoccupied_since = None
            if self.status != "covered":
                self.status = "covered"
                return {"event_type": "zone_covered", "entity_ref": entity_ref, "confidence": confidence}
            return None

        # Not confidently occupied this frame — start or continue the debounce clock.
        if self._unoccupied_since is None:
            self._unoccupied_since = ts

        elapsed = (ts - self._unoccupied_since).total_seconds()
        if elapsed >= self.debounce_seconds and self.status != "gap":
            self.status = "gap"
            return {"event_type": "zone_gap", "entity_ref": None, "confidence": 1.0 - confidence if occupied else 1.0}

        return None


class DebouncerRegistry:
    """One ZoneDebouncer per (camera_id, zone_id)."""

    def __init__(self, debounce_seconds: float = 60.0, min_confidence: float = 0.8):
        self.debounce_seconds = debounce_seconds
        self.min_confidence = min_confidence
        self._debouncers: dict = {}

    def get(self, camera_id: str, zone_id: str) -> ZoneDebouncer:
        key = (camera_id, zone_id)
        if key not in self._debouncers:
            self._debouncers[key] = ZoneDebouncer(self.debounce_seconds, self.min_confidence)
        return self._debouncers[key]
