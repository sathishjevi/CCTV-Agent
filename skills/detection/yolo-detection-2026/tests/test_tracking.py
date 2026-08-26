"""Unit tests for detect.py's TrackerRegistry (Phase 2 of the `supervision`/
`trackers` adoption — see zone_utils.py's Phase 1 sibling in
floorwatch-coverage). Run with: python -m pytest tests/ -v

These exercise the REAL supervision/trackers packages (not mocks) — the
whole point of this class is correctly wiring our detection dicts into
ByteTrackTracker's actual API, so a mock would validate nothing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

from detect import TrackerRegistry, _TRACKING_AVAILABLE  # noqa: E402

pytestmark = pytest.mark.skipif(
    not _TRACKING_AVAILABLE,
    reason="supervision/trackers not installed in this environment",
)


def _person(x1=100, y1=100, x2=200, y2=300, confidence=0.9):
    return {"class": "person", "confidence": confidence, "bbox": [x1, y1, x2, y2]}


def test_track_id_activates_on_second_consecutive_sighting():
    """ByteTrackTracker's default minimum_consecutive_frames=2 — a track
    isn't "confirmed" (real id) until the 2nd consecutive frame it appears
    in; the 1st sighting gets -1. This is real upstream behavior, not a
    bug in our wiring — documented here so a future reader isn't surprised
    by it."""
    reg = TrackerRegistry(fps=1.0)
    class_ids = {}

    frame1 = [_person()]
    reg.track("cam1", frame1, "2026-08-26T10:00:00Z", class_ids)
    assert frame1[0]["track_id"] == -1

    frame2 = [_person(105, 102, 205, 301)]  # same person, slightly moved
    reg.track("cam1", frame2, "2026-08-26T10:00:01Z", class_ids)
    assert frame2[0]["track_id"] == 0


def test_cameras_get_independent_track_id_spaces():
    """A track_id must never be shared/confused across two different
    camera streams — each camera gets its own ByteTrackTracker instance."""
    reg = TrackerRegistry(fps=1.0)
    class_ids = {}

    cam1_frame1 = [_person()]
    reg.track("cam1", cam1_frame1, "2026-08-26T10:00:00Z", class_ids)
    cam1_frame2 = [_person(105, 102, 205, 301)]
    reg.track("cam1", cam1_frame2, "2026-08-26T10:00:01Z", class_ids)
    assert cam1_frame2[0]["track_id"] == 0

    # cam2 has never been seen before — starts its own fresh track space,
    # unaffected by cam1 already having an activated track_id=0
    cam2_frame1 = [_person()]
    reg.track("cam2", cam2_frame1, "2026-08-26T10:00:00Z", class_ids)
    assert cam2_frame1[0]["track_id"] == -1


def test_empty_objects_is_a_no_op():
    reg = TrackerRegistry(fps=1.0)
    objects = []
    reg.track("cam1", objects, "2026-08-26T10:00:00Z", {})
    assert objects == []


def test_missing_timestamp_does_not_crash():
    """timestamp is best-effort — a blank/unparseable one must degrade to
    frame-count-based aging, never raise."""
    reg = TrackerRegistry(fps=1.0)
    objects = [_person()]
    reg.track("cam1", objects, "", {})
    assert objects[0]["track_id"] == -1


def test_malformed_timestamp_does_not_crash():
    reg = TrackerRegistry(fps=1.0)
    objects = [_person()]
    reg.track("cam1", objects, "not-a-timestamp", {})
    assert objects[0]["track_id"] == -1


def test_class_ids_mapping_is_stable_across_calls():
    """The same class name must always map to the same int class_id within
    a run — a shared, caller-owned dict, not per-call state."""
    reg = TrackerRegistry(fps=1.0)
    class_ids = {}
    reg.track("cam1", [_person()], "2026-08-26T10:00:00Z", class_ids)
    assert class_ids == {"person": 0}
    reg.track("cam1", [{"class": "car", "confidence": 0.8, "bbox": [0, 0, 50, 50]}],
              "2026-08-26T10:00:01Z", class_ids)
    assert class_ids == {"person": 0, "car": 1}


def test_tracking_unavailable_is_a_clean_no_op(monkeypatch):
    """If supervision/trackers failed to import, track() must leave objects
    untouched (no track_id key at all) rather than raising."""
    import detect
    monkeypatch.setattr(detect, "_TRACKING_AVAILABLE", False)
    reg = TrackerRegistry(fps=1.0)
    objects = [_person()]
    reg.track("cam1", objects, "2026-08-26T10:00:00Z", {})
    assert "track_id" not in objects[0]
