"""Unit tests for embeddings.py, incident_notes.py, and ingest.py."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from embeddings import TfidfEmbeddingProvider, build_embedding_provider  # noqa: E402
from incident_notes import IncidentNoteStore  # noqa: E402
from ingest import ingest_new_records  # noqa: E402
from vector_store import SqliteVectorStore  # noqa: E402


# ── TfidfEmbeddingProvider ────────────────────────────────────────────

def test_tfidf_embed_returns_fixed_dimension_vectors():
    provider = TfidfEmbeddingProvider(dim=128)
    vectors = provider.embed(["hello world", "a completely different sentence"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 128
    assert len(vectors[1]) == 128


def test_tfidf_embed_one_matches_embed():
    provider = TfidfEmbeddingProvider(dim=64)
    assert provider.embed_one("test text") == provider.embed(["test text"])[0]


def test_tfidf_similar_texts_score_higher_than_dissimilar():
    import numpy as np
    provider = TfidfEmbeddingProvider(dim=256)
    a = np.array(provider.embed_one("concession counter unstaffed gap"))
    b = np.array(provider.embed_one("concession counter coverage gap unresolved"))
    c = np.array(provider.embed_one("restroom cleaning supplies restocked"))
    sim_ab = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    sim_ac = float(np.dot(a, c) / (np.linalg.norm(a) * np.linalg.norm(c) + 1e-9))
    assert sim_ab > sim_ac


def test_tfidf_embedding_is_deterministic():
    provider = TfidfEmbeddingProvider(dim=64)
    assert provider.embed_one("same text") == provider.embed_one("same text")


def test_build_embedding_provider_defaults_to_tfidf_without_voyage_key():
    class FakeConfig:
        EMBEDDING_PROVIDER = "tfidf"
        VOYAGE_API_KEY = ""
        VOYAGE_MODEL = "voyage-3"
        EMBEDDING_DIM = 128
    provider = build_embedding_provider(FakeConfig())
    assert isinstance(provider, TfidfEmbeddingProvider)


def test_build_embedding_provider_falls_back_when_voyage_key_set_but_provider_is_tfidf():
    class FakeConfig:
        EMBEDDING_PROVIDER = "tfidf"
        VOYAGE_API_KEY = "some-key"
        VOYAGE_MODEL = "voyage-3"
        EMBEDDING_DIM = 128
    provider = build_embedding_provider(FakeConfig())
    assert isinstance(provider, TfidfEmbeddingProvider)


# ── IncidentNoteStore ────────────────────────────────────────────────

def test_incident_note_store_add_and_read(tmp_path):
    store = IncidentNoteStore(tmp_path / "notes.jsonl")
    note = store.add("Spilled drink near entrance, cleaned up", zone_id="entrance", author="alice")
    assert note["zone_id"] == "entrance"
    assert note["author"] == "alice"
    all_notes = store.read_all()
    assert len(all_notes) == 1
    assert all_notes[0]["note_id"] == note["note_id"]


def test_incident_note_store_missing_file_returns_empty(tmp_path):
    store = IncidentNoteStore(tmp_path / "nonexistent.jsonl")
    assert store.read_all() == []


def test_incident_note_store_skips_malformed_lines(tmp_path):
    path = tmp_path / "notes.jsonl"
    path.write_text('{"note_id": "1", "timestamp": "t", "text": "ok"}\nnot json\n')
    store = IncidentNoteStore(path)
    assert len(store.read_all()) == 1


# ── ingest ──────────────────────────────────────────────────────────

def _write_digest(path: Path, events: list):
    path.write_text("\n".join(json.dumps(e) for e in events))


def test_ingest_embeds_digest_events_and_notes(tmp_path):
    digest_path = tmp_path / "digest.jsonl"
    _write_digest(digest_path, [
        {"event_id": "e1", "timestamp": "2026-07-24T08:00:00Z", "zone_id": "concession",
         "zone_name": "Concession Counter", "camera_id": "cam1", "event_type": "zone_escalated",
         "confidence": 0.9, "message": "unresolved gap"},
    ])
    notes_store = IncidentNoteStore(tmp_path / "notes.jsonl")
    notes_store.add("Employee called in sick, zone uncovered most of shift", zone_id="concession")

    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=64)

    count = ingest_new_records(vector_store, embedding_provider, digest_path, notes_store)
    assert count == 2
    assert len(vector_store.all_ids()) == 2


def test_ingest_is_idempotent_on_rerun(tmp_path):
    digest_path = tmp_path / "digest.jsonl"
    _write_digest(digest_path, [
        {"event_id": "e1", "timestamp": "2026-07-24T08:00:00Z", "zone_id": "concession",
         "event_type": "zone_escalated", "confidence": 0.9},
    ])
    notes_store = IncidentNoteStore(tmp_path / "notes.jsonl")
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=64)

    first = ingest_new_records(vector_store, embedding_provider, digest_path, notes_store)
    second = ingest_new_records(vector_store, embedding_provider, digest_path, notes_store)
    assert first == 1
    assert second == 0  # already indexed — no duplicate embedding work


def test_ingest_new_note_after_initial_ingest_only_embeds_the_new_one(tmp_path):
    digest_path = tmp_path / "digest.jsonl"
    _write_digest(digest_path, [])
    notes_store = IncidentNoteStore(tmp_path / "notes.jsonl")
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=64)

    notes_store.add("First note", zone_id="lobby")
    assert ingest_new_records(vector_store, embedding_provider, digest_path, notes_store) == 1

    notes_store.add("Second note", zone_id="lobby")
    assert ingest_new_records(vector_store, embedding_provider, digest_path, notes_store) == 1
    assert len(vector_store.all_ids()) == 2


def test_ingest_source_ref_carries_zone_and_date_for_citation(tmp_path):
    digest_path = tmp_path / "digest.jsonl"
    _write_digest(digest_path, [
        {"event_id": "e1", "timestamp": "2026-07-24T08:00:00Z", "zone_id": "concession",
         "event_type": "zone_escalated", "confidence": 0.9},
    ])
    notes_store = IncidentNoteStore(tmp_path / "notes.jsonl")
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=64)
    ingest_new_records(vector_store, embedding_provider, digest_path, notes_store)

    record = vector_store.get("digest:e1")
    source_ref = json.loads(record["source_ref"])
    assert source_ref["zone_id"] == "concession"
    assert source_ref["date"] == "2026-07-24"
