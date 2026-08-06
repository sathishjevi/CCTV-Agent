"""Supervisor-written incident notes — a small append-only store this
phase owns and can freely write to. This is NOT the "no write path"
constraint's concern: Global Constraint 7 / Phase 5's "read-only" scope
is about never mutating zone state, task state, or the notification
system (owned by floorwatch-rules-engine). Incident notes are this
phase's own annotation layer on top of history, analogous to a
supervisor's shift-log entry — writing one has no effect on live
coverage/effort tracking."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class IncidentNoteStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, text: str, zone_id: Optional[str] = None, author: str = "supervisor") -> dict:
        note = {
            "note_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "zone_id": zone_id,
            "author": author,
            "text": text,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(note) + "\n")
        return note

    def read_all(self) -> list:
        if not self.path.exists():
            return []
        notes = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                notes.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return notes
