"""Unit tests for Floorwatch coverage zone-mapping + debounce logic. Run with: python -m pytest tests/ -v"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "lib"))

from zone_utils import point_in_polygon, bbox_anchor, Zone  # noqa: E402
from debounce import ZoneDebouncer, DebouncerRegistry  # noqa: E402
from main import process_detections, compute_zone_occupancy  # noqa: E402

SQUARE = [[100, 100], [300, 100], [300, 300], [100, 300]]

T0 = "2026-07-24T10:00:00Z"


def _t(seconds_offset):
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 7, 24, 10, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds_offset)).isoformat().replace("+00:00", "Z")


# ── zone_utils ──────────────────────────────────────────────────────────

def test_point_in_polygon_inside():
    assert point_in_polygon((200, 200), SQUARE) is True


def test_point_in_polygon_outside():
    assert point_in_polygon((50, 50), SQUARE) is False


def test_bbox_anchor_bottom_center():
    assert bbox_anchor([100, 100, 200, 300], "bottom_center") == (150, 300)


# ── debounce ────────────────────────────────────────────────────────────

def test_debouncer_emits_zone_covered_immediately():
    d = ZoneDebouncer(debounce_seconds=60, min_confidence=0.8)
    evt = d.update(occupied=True, confidence=0.9, entity_ref="track_0", timestamp=_t(0))
    assert evt == {"event_type": "zone_covered", "entity_ref": "track_0", "confidence": 0.9}


def test_debouncer_no_repeat_zone_covered_while_still_covered():
    d = ZoneDebouncer(debounce_seconds=60, min_confidence=0.8)
    d.update(occupied=True, confidence=0.9, entity_ref="track_0", timestamp=_t(0))
    evt = d.update(occupied=True, confidence=0.95, entity_ref="track_0", timestamp=_t(5))
    assert evt is None


def test_debouncer_zone_gap_requires_full_debounce_window():
    d = ZoneDebouncer(debounce_seconds=60, min_confidence=0.8)
    d.update(occupied=True, confidence=0.9, entity_ref="track_0", timestamp=_t(0))
    # unoccupied starting at t=10; only 30s elapsed by t=40 — should not fire yet
    d.update(occupied=False, confidence=0.0, entity_ref=None, timestamp=_t(10))
    evt = d.update(occupied=False, confidence=0.0, entity_ref=None, timestamp=_t(40))
    assert evt is None
    # by t=71 (61s after unoccupied started at t=10), debounce window has elapsed
    evt = d.update(occupied=False, confidence=0.0, entity_ref=None, timestamp=_t(71))
    assert evt == {"event_type": "zone_gap", "entity_ref": None, "confidence": 1.0}


def test_debouncer_low_confidence_detection_does_not_clear_gap():
    d = ZoneDebouncer(debounce_seconds=60, min_confidence=0.8)
    d.update(occupied=True, confidence=0.9, entity_ref="track_0", timestamp=_t(0))
    d.update(occupied=False, confidence=0.0, entity_ref=None, timestamp=_t(10))
    # a sub-threshold "maybe person" sighting shouldn't reset the debounce clock's origin
    # (still not confidently occupied), so gap still fires at t=71
    d.update(occupied=True, confidence=0.4, entity_ref="track_0", timestamp=_t(40))
    evt = d.update(occupied=False, confidence=0.0, entity_ref=None, timestamp=_t(71))
    assert evt is not None
    assert evt["event_type"] == "zone_gap"


def test_debouncer_recovers_to_covered_after_gap():
    d = ZoneDebouncer(debounce_seconds=60, min_confidence=0.8)
    d.update(occupied=True, confidence=0.9, entity_ref="track_0", timestamp=_t(0))
    d.update(occupied=False, confidence=0.0, entity_ref=None, timestamp=_t(10))
    d.update(occupied=False, confidence=0.0, entity_ref=None, timestamp=_t(71))  # -> gap
    evt = d.update(occupied=True, confidence=0.85, entity_ref="track_1", timestamp=_t(80))
    assert evt == {"event_type": "zone_covered", "entity_ref": "track_1", "confidence": 0.85}


def test_registry_isolates_state_per_camera_zone():
    reg = DebouncerRegistry(debounce_seconds=60, min_confidence=0.8)
    a = reg.get("cam1", "zoneA")
    b = reg.get("cam1", "zoneB")
    c = reg.get("cam2", "zoneA")
    assert a is not b
    assert a is not c
    assert reg.get("cam1", "zoneA") is a


# ── main.py pipeline ────────────────────────────────────────────────────

def _zones():
    return {"cam1": [Zone(zone_id="z1", role_tag="concession", polygon=SQUARE)]}


def test_compute_zone_occupancy_occupied():
    msg = {
        "event": "detections", "camera_id": "cam1", "timestamp": T0,
        "objects": [{"class": "person", "confidence": 0.9, "bbox": [150, 150, 250, 280]}],
    }
    obs = compute_zone_occupancy(msg, _zones(), "person", 0.5)
    assert len(obs) == 1
    assert obs[0]["occupied"] is True
    assert obs[0]["entity_ref"] == "track_0"


def test_compute_zone_occupancy_no_calibration_returns_empty():
    obs = compute_zone_occupancy(
        {"event": "detections", "camera_id": "uncalibrated", "timestamp": T0, "objects": []},
        {}, "person", 0.5,
    )
    assert obs == []


def test_process_detections_emits_zone_covered_on_first_occupied_frame():
    registry = DebouncerRegistry(debounce_seconds=60, min_confidence=0.8)
    msg = {
        "event": "detections", "camera_id": "cam1", "timestamp": T0,
        "objects": [{"class": "person", "confidence": 0.9, "bbox": [150, 150, 250, 280]}],
    }
    events = process_detections(msg, _zones(), registry, "person", 0.5)
    assert len(events) == 1
    assert events[0]["event_type"] == "zone_covered"
    assert events[0]["zone_id"] == "z1"
    assert events[0]["camera_id"] == "cam1"
    assert events[0]["role_tag"] == "concession"
    assert events[0]["source_model_version"].startswith("floorwatch-coverage/")


def test_process_detections_no_event_on_steady_state():
    registry = DebouncerRegistry(debounce_seconds=60, min_confidence=0.8)
    msg = {
        "event": "detections", "camera_id": "cam1", "timestamp": T0,
        "objects": [{"class": "person", "confidence": 0.9, "bbox": [150, 150, 250, 280]}],
    }
    process_detections(msg, _zones(), registry, "person", 0.5)  # first frame -> zone_covered
    events = process_detections(msg, _zones(), registry, "person", 0.5)  # still occupied -> nothing
    assert events == []


def test_process_detections_zone_gap_after_debounce_window():
    registry = DebouncerRegistry(debounce_seconds=60, min_confidence=0.8)
    occupied_msg = {
        "event": "detections", "camera_id": "cam1", "timestamp": _t(0),
        "objects": [{"class": "person", "confidence": 0.9, "bbox": [150, 150, 250, 280]}],
    }
    empty_msg_early = {"event": "detections", "camera_id": "cam1", "timestamp": _t(10), "objects": []}
    empty_msg_late = {"event": "detections", "camera_id": "cam1", "timestamp": _t(71), "objects": []}

    process_detections(occupied_msg, _zones(), registry, "person", 0.5)
    assert process_detections(empty_msg_early, _zones(), registry, "person", 0.5) == []
    events = process_detections(empty_msg_late, _zones(), registry, "person", 0.5)
    assert len(events) == 1
    assert events[0]["event_type"] == "zone_gap"
    assert events[0]["entity_ref"] is None
