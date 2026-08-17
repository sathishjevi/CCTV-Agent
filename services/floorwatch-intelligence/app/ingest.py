"""
Embedding ingest pipeline — build brief Phase 5 task 2 (the other half of
embeddings.py): reads shift digests and supervisor incident notes,
renders each into a citable text summary, embeds anything not already
indexed, and upserts into the vector store alongside a reference to the
source record (shift/date/zone — what the LLM layer cites back to the
supervisor).

Idempotent: `vector_store.all_ids()` is checked before embedding, so
re-running against a digest file that already has some entries indexed
only embeds the new ones — safe to call on every poll/cron tick.
"""

import json
from pathlib import Path

from incident_notes import IncidentNoteStore
from floorwatch_logging import get_logger

log = get_logger("intelligence.ingest")


def load_digest(path: Path) -> list:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _digest_record_id(event: dict) -> str:
    event_id = event.get("event_id")
    if event_id:
        return f"digest:{event_id}"
    # Fallback for events without an event_id — deterministic composite key.
    return f"digest:{event.get('timestamp','')}:{event.get('zone_id','')}:{event.get('event_type','')}"


def _digest_to_text(event: dict) -> str:
    ts = event.get("timestamp", "unknown time")
    zone = event.get("zone_name") or event.get("zone_id", "unknown zone")
    event_type = event.get("event_type", "unknown_event")
    camera = event.get("camera_id", "unknown camera")
    confidence = event.get("confidence")
    message = event.get("message") or ""
    parts = [f"{ts} — {event_type} at {zone} (camera {camera})"]
    if message:
        parts.append(f": {message}")
    if confidence is not None:
        parts.append(f" [confidence={confidence}]")
    task_name = event.get("task_name")
    if task_name:
        parts.append(f" (task: {task_name})")
    return "".join(parts)


def _note_record_id(note: dict) -> str:
    return f"note:{note['note_id']}"


def _note_to_text(note: dict) -> str:
    zone = note.get("zone_id") or "unspecified zone"
    return f"{note['timestamp']} — supervisor note ({note.get('author','supervisor')}) on {zone}: {note['text']}"


def ingest_new_records(vector_store, embedding_provider, digest_path: Path, notes_store: IncidentNoteStore) -> int:
    """Embeds any digest events / incident notes not already in the
    vector store. Returns the count of newly embedded records."""
    existing_ids = vector_store.all_ids()
    to_embed = []  # list of (record_id, source_type, source_ref, text, metadata)

    for event in load_digest(digest_path):
        record_id = _digest_record_id(event)
        if record_id in existing_ids:
            continue
        source_ref = json.dumps({
            "kind": "shift_digest", "event_id": event.get("event_id"),
            "date": (event.get("timestamp") or "")[:10],
            "zone_id": event.get("zone_id"), "event_type": event.get("event_type"),
        })
        metadata = {
            "zone_id": event.get("zone_id"), "camera_id": event.get("camera_id"),
            "event_type": event.get("event_type"), "timestamp": event.get("timestamp"),
        }
        to_embed.append((record_id, "shift_digest", source_ref, _digest_to_text(event), metadata))

    for note in notes_store.read_all():
        record_id = _note_record_id(note)
        if record_id in existing_ids:
            continue
        source_ref = json.dumps({
            "kind": "incident_note", "note_id": note["note_id"],
            "date": note["timestamp"][:10], "zone_id": note.get("zone_id"),
        })
        metadata = {"zone_id": note.get("zone_id"), "timestamp": note["timestamp"], "author": note.get("author")}
        to_embed.append((record_id, "incident_note", source_ref, _note_to_text(note), metadata))

    if not to_embed:
        return 0

    texts = [item[3] for item in to_embed]
    vectors = embedding_provider.embed(texts)
    for (record_id, source_type, source_ref, text, metadata), vector in zip(to_embed, vectors):
        vector_store.upsert(record_id, source_type, source_ref, text, vector, metadata)

    log(f"Embedded {len(to_embed)} new record(s) "
        f"({sum(1 for t in to_embed if t[1]=='shift_digest')} digest, "
        f"{sum(1 for t in to_embed if t[1]=='incident_note')} note).")
    return len(to_embed)
