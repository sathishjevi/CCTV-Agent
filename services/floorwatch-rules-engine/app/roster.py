"""Roster cross-check — Global Constraint 5: "Never let a zone/task
nudge/flag fire if the roster shows nobody was actually assigned there —
treat this as a hard precondition, not a nice-to-have."

Pilot implementation: a manually maintained JSON file (roster.json),
reloaded on every check so a supervisor can update staffing mid-shift
without restarting the service.

Zones added later via the dashboard's Manage Zones UI (zone_directory.py)
were never in roster.json at all — since that file predates zone_directory
and nothing writes to it automatically, every such zone silently failed
this check forever (reported directly: "Failed to assign task: zone ...
is not staffed per the roster"). `zone_directory` here is the fallback
for exactly that case: checked only when roster.json has no opinion on
this zone_id, using the zone's own `staffed` flag (defaults true — a
supervisor adding a zone is asserting it's real and currently covered;
they can flip it off in Manage Zones for a zone that exists but isn't
staffed this shift).
"""

import json
from pathlib import Path
from typing import Optional


class Roster:
    def __init__(self, roster_path: Path, zone_directory=None):
        self.roster_path = roster_path
        self.zone_directory = zone_directory

    def is_zone_staffed(self, zone_id: str) -> bool:
        """Hard precondition — defaults to False (unstaffed) if neither
        source has an opinion, never the other way around. An unlisted
        zone must never silently pass this gate."""
        data = {}
        if self.roster_path.exists():
            try:
                data = json.loads(self.roster_path.read_text())
            except json.JSONDecodeError:
                data = {}
        if zone_id in data:
            return bool(data[zone_id])
        if self.zone_directory is not None:
            z = self.zone_directory.get(zone_id)
            if z is not None:
                return bool(z.get("active", True)) and bool(z.get("staffed", True))
        return False
