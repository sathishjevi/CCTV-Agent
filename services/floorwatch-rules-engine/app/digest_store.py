"""Shift-digest store — append-only JSONL log of tier escalations, per
brief Phase 2 task 3 ("Tier 3: on command timeout, mark logged_escalated
and write to the shift-digest store"). Phase 4 builds the scheduled
summarization job on top of this; here we just guarantee every escalation
is durably recorded, structured, and never contains video/frame data —
only the same shared-schema event fields.
"""

import json
from pathlib import Path


class DigestStore:
    def __init__(self, digest_path: Path):
        self.digest_path = digest_path
        self.digest_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict):
        with open(self.digest_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def read_all(self) -> list:
        if not self.digest_path.exists():
            return []
        lines = self.digest_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
