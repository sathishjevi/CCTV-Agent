"""Zone loading and zone-membership helpers for Floorwatch coverage.

Zone-membership testing (point-in-polygon) is delegated to `supervision`'s
`PolygonZone` (see build_polygon_zone() below) rather than a hand-rolled
ray-casting implementation — same math, but maintained/tested upstream
instead of by us. See the adoption analysis this replaced: the anchor
computation (bottom_center/center) that used to live here as bbox_anchor()
is now handled internally by PolygonZone's `triggering_anchors`, so it's
gone too rather than kept as dead code.
"""

import json
from pathlib import Path
from typing import NamedTuple

import numpy as np
import supervision as sv


class Zone(NamedTuple):
    zone_id: str
    role_tag: str
    polygon: list  # [[x, y], ...]


def load_zones_for_camera(zones_dir: Path, camera_id: str) -> list:
    """Load zone polygons for a camera from <zones_dir>/<camera_id>.json.

    Returns [] if no calibration file exists for this camera (never invents zones).
    """
    zone_file = zones_dir / f"{camera_id}.json"
    if not zone_file.exists():
        return []

    with open(zone_file) as f:
        data = json.load(f)

    zones = []
    for z in data.get("zones", []):
        zones.append(Zone(
            zone_id=z["zone_id"],
            role_tag=z.get("role_tag", "unknown"),
            polygon=z["polygon"],
        ))
    return zones


def load_all_zones(zones_dir: Path) -> dict:
    """Load every <camera_id>.json in zones_dir. Returns {camera_id: [Zone, ...]}."""
    if not zones_dir.exists():
        return {}
    result = {}
    for zone_file in zones_dir.glob("*.json"):
        camera_id = zone_file.stem
        result[camera_id] = load_zones_for_camera(zones_dir, camera_id)
    return result


_ANCHOR_POSITIONS = {
    "bottom_center": sv.Position.BOTTOM_CENTER,
    "center": sv.Position.CENTER,
}


def build_polygon_zone(zone: Zone, anchor: str = "bottom_center") -> sv.PolygonZone:
    """One sv.PolygonZone per calibrated zone — safe to build once and reuse
    across every frame for that zone's lifetime (it holds no per-frame state
    that would make reuse incorrect; `trigger()` is a pure function of the
    detections batch passed to it each call)."""
    position = _ANCHOR_POSITIONS.get(anchor, sv.Position.BOTTOM_CENTER)
    return sv.PolygonZone(polygon=np.array(zone.polygon, dtype=np.int64),
                           triggering_anchors=(position,))
