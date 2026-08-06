"""Zone loading and point-in-polygon helpers for Floorwatch coverage.

Pure stdlib — no numpy/shapely dependency needed for this pilot's polygon sizes.
"""

import json
from pathlib import Path
from typing import NamedTuple


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


def bbox_anchor(bbox: list, mode: str = "bottom_center") -> tuple:
    """Reduce an [x1,y1,x2,y2] bbox to a single (x,y) point for zone testing."""
    x1, y1, x2, y2 = bbox
    if mode == "center":
        return ((x1 + x2) / 2, (y1 + y2) / 2)
    # bottom_center: approximates where the person is standing (feet)
    return ((x1 + x2) / 2, y2)


def point_in_polygon(point: tuple, polygon: list) -> bool:
    """Ray-casting point-in-polygon test. polygon is [[x,y], ...]."""
    x, y = point
    n = len(polygon)
    inside = False
    x1, y1 = polygon[0]
    for i in range(1, n + 1):
        x2, y2 = polygon[i % n]
        if y > min(y1, y2):
            if y <= max(y1, y2):
                if x <= max(x1, x2):
                    if y1 != y2:
                        x_intersect = (y - y1) * (x2 - x1) / (y2 - y1) + x1
                    else:
                        x_intersect = x1
                    if x1 == x2 or x <= x_intersect:
                        inside = not inside
        x1, y1 = x2, y2
    return inside
