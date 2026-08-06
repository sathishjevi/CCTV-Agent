"""Unit tests for retrieval.py — semantic search + citation formatting."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from embeddings import TfidfEmbeddingProvider  # noqa: E402
from retrieval import format_citation, semantic_search  # noqa: E402
from vector_store import SqliteVectorStore  # noqa: E402


def _seed(vector_store, embedding_provider, entries):
    """entries: list of (record_id, source_type, source_ref_dict, text)"""
    for record_id, source_type, source_ref, text in entries:
        vector_store.upsert(record_id, source_type, json.dumps(source_ref), text,
                            embedding_provider.embed_one(text))


def test_semantic_search_returns_relevant_results_with_citations(tmp_path):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=128)
    _seed(vector_store, embedding_provider, [
        ("digest:e1", "shift_digest",
         {"kind": "shift_digest", "date": "2026-07-20", "zone_id": "concession", "event_type": "zone_gap"},
         "concession counter coverage gap, employee left post unattended"),
        ("digest:e2", "shift_digest",
         {"kind": "shift_digest", "date": "2026-07-22", "zone_id": "restroomA", "event_type": "task_flag"},
         "restroom cleaning task flagged, low active time detected"),
    ])

    results = semantic_search(vector_store, embedding_provider,
                               "concession counter unattended coverage gap", top_k=5)
    assert len(results) == 2
    assert results[0]["record_id"] == "digest:e1"  # most similar to the query
    assert "2026-07-20" in results[0]["citation"]
    assert "concession" in results[0]["citation"]


def test_semantic_search_zone_filter_excludes_other_zones(tmp_path):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=128)
    _seed(vector_store, embedding_provider, [
        ("digest:e1", "shift_digest",
         {"date": "2026-07-20", "zone_id": "concession", "event_type": "zone_gap"},
         "concession counter gap"),
        ("digest:e2", "shift_digest",
         {"date": "2026-07-21", "zone_id": "lobby", "event_type": "zone_gap"},
         "lobby coverage gap"),
    ])

    results = semantic_search(vector_store, embedding_provider, "coverage gap", top_k=5, zone_id="concession")
    assert len(results) == 1
    assert results[0]["record_id"] == "digest:e1"


def test_semantic_search_on_empty_store_returns_empty(tmp_path):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=64)
    assert semantic_search(vector_store, embedding_provider, "anything", top_k=5) == []


def test_semantic_search_handles_malformed_source_ref_gracefully(tmp_path):
    vector_store = SqliteVectorStore(tmp_path / "v.sqlite3")
    embedding_provider = TfidfEmbeddingProvider(dim=64)
    vector_store.upsert("r1", "shift_digest", "not valid json", "some text",
                        embedding_provider.embed_one("some text"))
    results = semantic_search(vector_store, embedding_provider, "some text", top_k=5)
    assert len(results) == 1
    assert "unknown" in results[0]["citation"]


# ── format_citation ──────────────────────────────────────────────────

def test_format_citation_shift_digest():
    citation = format_citation("shift_digest", {"date": "2026-07-24", "zone_id": "concession", "event_type": "zone_escalated"})
    assert citation == "[shift digest, 2026-07-24, zone=concession, zone_escalated]"


def test_format_citation_incident_note():
    citation = format_citation("incident_note", {"date": "2026-07-24", "zone_id": "lobby"})
    assert citation == "[supervisor note, 2026-07-24, zone=lobby]"


def test_format_citation_unknown_source_type():
    citation = format_citation("mystery", {"date": "2026-07-24", "zone_id": "lobby"})
    assert "mystery" in citation
