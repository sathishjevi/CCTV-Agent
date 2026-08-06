"""Roster cross-check — Global Constraint 5: "Never let a zone/task
nudge/flag fire if the roster shows nobody was actually assigned there —
treat this as a hard precondition, not a nice-to-have."

Pilot implementation: a manually maintained JSON file (roster.json),
reloaded on every check so a supervisor can update staffing mid-shift
without restarting the service.
"""

import json
from pathlib import Path


class Roster:
    def __init__(self, roster_path: Path):
        self.roster_path = roster_path

    def is_zone_staffed(self, zone_id: str) -> bool:
        """Hard precondition — defaults to False (unstaffed) if the roster
        file is missing or the zone isn't listed, never the other way
        around. An unlisted zone must never silently pass this gate."""
        if not self.roster_path.exists():
            return False
        try:
            data = json.loads(self.roster_path.read_text())
        except json.JSONDecodeError:
            return False
        return bool(data.get(zone_id, False))
