"""Tests for the fast presence readings that feed the rules engine's security alerts."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "lib"))

from debounce import DebouncerRegistry  # noqa: E402
from main import process_detections  # noqa: E402
from presence import PresenceRegistry, ZonePresence  # noqa: E402
from zone_utils import Zone  # noqa: E402

SQUARE = [[100, 100], [300, 100], [300, 300], [100, 300]]


def _t(seconds):
    base = datetime(2026, 7, 24, 22, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _tracker(**kw):
    args = dict(arrive_seconds=3, clear_seconds=30, heartbeat_seconds=30, min_confidence=0.6)
    args.update(kw)
    return ZonePresence(**args)


def test_arrival_needs_a_few_seconds_of_confident_sightings():
    p = _tracker()
    assert p.update(True, 0.9, "track_0", _t(0)) is None
    assert p.update(True, 0.9, "track_0", _t(2)) is None
    evt = p.update(True, 0.9, "track_0", _t(3))
    assert evt["event_type"] == "zone_person_present" and evt["action_type"] == "arrived"


def test_a_single_flicker_never_counts_as_a_person():
    p = _tracker()
    assert p.update(True, 0.95, "track_0", _t(0)) is None
    for s in range(1, 60):
        assert p.update(False, 0.0, None, _t(s)) is None
    assert p.present is False


def test_two_stray_detections_far_apart_do_not_add_up_to_an_arrival():
    p = _tracker()
    p.update(True, 0.9, "track_0", _t(0))
    for s in range(1, 20):
        p.update(False, 0.0, None, _t(s))      # gap longer than arrive_seconds drops the run
    assert p.update(True, 0.9, "track_0", _t(20)) is None
    assert p.present is False


def test_low_confidence_detections_are_ignored():
    p = _tracker()
    for s in range(0, 20):
        assert p.update(True, 0.4, "track_0", _t(s)) is None
    assert p.present is False


def test_heartbeats_repeat_while_the_person_stays_and_only_then():
    p = _tracker()
    events = []
    for s in range(0, 100):
        e = p.update(True, 0.9, "track_0", _t(s))
        if e:
            events.append((s, e["action_type"]))
    assert events[0] == (3, "arrived")
    assert [a for _, a in events[1:]] == ["heartbeat"] * 3          # at 33, 63, 93
    assert [s for s, _ in events[1:]] == [33, 63, 93]


def test_leaving_is_only_reported_after_the_clear_window():
    p = _tracker()
    for s in range(0, 5):
        p.update(True, 0.9, "track_0", _t(s))
    assert p.present
    assert p.update(False, 0.0, None, _t(10)) is None
    assert p.update(False, 0.0, None, _t(39)) is None                # 29 s empty
    evt = p.update(False, 0.0, None, _t(40))                          # 30 s empty
    assert evt["event_type"] == "zone_person_cleared" and evt["action_type"] == "cleared"
    assert p.present is False


def test_a_missed_frame_does_not_clear_a_person_who_is_still_there():
    p = _tracker()
    for s in range(0, 5):
        p.update(True, 0.9, "track_0", _t(s))
    assert p.update(False, 0.0, None, _t(6)) is None
    assert p.update(True, 0.9, "track_0", _t(7)) is None              # back; empty run reset, no event
    assert p.present


def test_a_person_can_arrive_again_after_leaving():
    p = _tracker()
    for s in range(0, 5):
        p.update(True, 0.9, "track_0", _t(s))
    for s in range(5, 40):
        p.update(False, 0.0, None, _t(s))
    assert p.present is False
    out = [p.update(True, 0.9, "track_1", _t(100 + s)) for s in range(0, 5)]
    assert [e["action_type"] for e in out if e] == ["arrived"]


def test_registry_keeps_one_tracker_per_camera_and_zone():
    r = PresenceRegistry()
    assert r.get("cam1", "a") is r.get("cam1", "a")
    assert r.get("cam1", "a") is not r.get("cam1", "b")
    assert r.get("cam1", "a") is not r.get("cam2", "a")


# ── through process_detections: real schema validation, both event families ──

def _zones():
    return {"cam1": [Zone(zone_id="back_door", role_tag="security", polygon=SQUARE)]}


def _msg(seconds, person=True):
    objects = [{"class": "person", "confidence": 0.9, "bbox": [150, 100, 250, 280]}] if person else []
    return {"event": "detections", "camera_id": "cam1", "timestamp": _t(seconds), "objects": objects}


def test_presence_events_come_out_alongside_coverage_events_and_validate():
    zones = _zones()
    coverage = DebouncerRegistry(debounce_seconds=60, min_confidence=0.8)
    presence = PresenceRegistry(arrive_seconds=3, clear_seconds=30, heartbeat_seconds=30, min_confidence=0.6)
    seen = []
    for s in range(0, 6):
        seen.extend(process_detections(_msg(s), zones, coverage, "person", 0.5, "bottom_center", presence))
    types = [e["event_type"] for e in seen]
    assert "zone_covered" in types                    # slow coverage signal still works
    assert types.count("zone_person_present") == 1    # and the fast one fired once
    present = next(e for e in seen if e["event_type"] == "zone_person_present")
    assert present["zone_id"] == "back_door" and present["action_type"] == "arrived"
    assert present["role_tag"] == "security"


def test_without_a_presence_registry_only_coverage_events_are_emitted():
    zones = _zones()
    coverage = DebouncerRegistry(debounce_seconds=60, min_confidence=0.8)
    seen = []
    for s in range(0, 10):
        seen.extend(process_detections(_msg(s), zones, coverage, "person", 0.5, "bottom_center"))
    assert [e["event_type"] for e in seen] == ["zone_covered"]
