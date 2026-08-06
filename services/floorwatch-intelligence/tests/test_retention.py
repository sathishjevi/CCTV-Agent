"""End-to-end CLI tests for retention.py — actually running main()
against a real SqliteVectorStore + incident_notes.jsonl file."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import retention  # noqa: E402
from vector_store import SqliteVectorStore  # noqa: E402

NOW = datetime.now(timezone.utc)


def test_retention_main_prunes_notes_and_vector_rows(tmp_path, monkeypatch, capsys):
    notes_path = tmp_path / "incident_notes.jsonl"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    new_ts = (NOW - timedelta(days=1)).isoformat()
    notes_path.write_text("\n".join(json.dumps(e) for e in [
        {"timestamp": old_ts, "text": "old note"},
        {"timestamp": new_ts, "text": "new note"},
    ]))

    db_path = tmp_path / "v.sqlite3"
    store = SqliteVectorStore(db_path)
    store.upsert("old_row", "incident_note", "old", "old note", [1.0, 0.0], metadata={"timestamp": old_ts})
    store.upsert("new_row", "incident_note", "new", "new note", [0.0, 1.0], metadata={"timestamp": new_ts})

    monkeypatch.setattr(retention.config, "INCIDENT_NOTES_PATH", notes_path)
    monkeypatch.setattr(retention.config, "SQLITE_VECTOR_DB_PATH", db_path)
    monkeypatch.setattr(retention.config, "POSTGRES_DSN", "")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--no-archive"])

    retention.main()

    out = capsys.readouterr().out
    assert "Pruned 1 incident note" in out
    assert "deleted 1 row" in out

    remaining_notes = [json.loads(line) for line in notes_path.read_text().splitlines()]
    assert len(remaining_notes) == 1 and remaining_notes[0]["text"] == "new note"

    assert store.get("old_row") is None
    assert store.get("new_row") is not None


def test_retention_main_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    notes_path = tmp_path / "incident_notes.jsonl"
    old_ts = (NOW - timedelta(days=200)).isoformat()
    original = json.dumps({"timestamp": old_ts, "text": "old note"})
    notes_path.write_text(original)

    db_path = tmp_path / "v.sqlite3"
    store = SqliteVectorStore(db_path)
    store.upsert("old_row", "incident_note", "old", "old note", [1.0, 0.0], metadata={"timestamp": old_ts})

    monkeypatch.setattr(retention.config, "INCIDENT_NOTES_PATH", notes_path)
    monkeypatch.setattr(retention.config, "SQLITE_VECTOR_DB_PATH", db_path)
    monkeypatch.setattr(retention.config, "POSTGRES_DSN", "")
    monkeypatch.setattr(retention.config, "RETENTION_DAYS", 90)
    monkeypatch.setattr(sys, "argv", ["retention.py", "--dry-run"])

    retention.main()

    assert notes_path.read_text() == original
    assert store.get("old_row") is not None  # vector store untouched in dry-run
