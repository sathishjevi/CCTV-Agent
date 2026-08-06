"""Unit tests for SqliteVectorStore — the fallback actually exercised in
this sandbox (no real Postgres+pgvector instance available; see
vector_store.py's module docstring and PHASE_5_NOTES.md)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from vector_store import SqliteVectorStore, build_vector_store  # noqa: E402


def test_upsert_and_get(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    store.upsert("r1", "digest", "2026-07-24", "concession gap unresolved", [1.0, 0.0, 0.0],
                 metadata={"zone_id": "concession"})
    record = store.get("r1")
    assert record["text"] == "concession gap unresolved"
    assert record["metadata"]["zone_id"] == "concession"


def test_get_missing_returns_none(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    assert store.get("nonexistent") is None


def test_delete_before_removes_only_old_timestamped_rows(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    store.upsert("old", "digest", "d1", "old entry", [1.0, 0.0], metadata={"timestamp": "2026-01-01T00:00:00Z"})
    store.upsert("new", "digest", "d2", "new entry", [0.0, 1.0], metadata={"timestamp": "2026-07-28T00:00:00Z"})
    deleted = store.delete_before("2026-07-01T00:00:00Z")
    assert deleted == 1
    assert store.get("old") is None
    assert store.get("new") is not None


def test_delete_before_keeps_rows_with_no_timestamp_in_metadata(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    store.upsert("no_ts", "digest", "d1", "no timestamp here", [1.0, 0.0], metadata={"zone_id": "concession"})
    deleted = store.delete_before("2026-07-01T00:00:00Z")
    assert deleted == 0
    assert store.get("no_ts") is not None


def test_upsert_is_idempotent_on_record_id(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    store.upsert("r1", "digest", "2026-07-24", "first version", [1.0, 0.0])
    store.upsert("r1", "digest", "2026-07-24", "updated version", [0.0, 1.0])
    assert store.get("r1")["text"] == "updated version"
    assert len(store.all_ids()) == 1


def test_search_ranks_by_cosine_similarity(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    store.upsert("close", "digest", "d1", "similar text", [1.0, 0.0, 0.0])
    store.upsert("far", "digest", "d2", "different text", [0.0, 1.0, 0.0])
    store.upsert("closest", "digest", "d3", "most similar", [0.9, 0.1, 0.0])

    results = store.search([1.0, 0.0, 0.0], top_k=3)
    assert [r["record_id"] for r in results] == ["close", "closest", "far"]
    assert results[0]["score"] > results[1]["score"] > results[2]["score"]


def test_search_respects_top_k(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    for i in range(10):
        store.upsert(f"r{i}", "digest", f"d{i}", f"text {i}", [float(i), 0.0])
    results = store.search([5.0, 0.0], top_k=3)
    assert len(results) == 3


def test_search_skips_dimension_mismatch(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    store.upsert("r1", "digest", "d1", "text", [1.0, 0.0, 0.0])
    store.upsert("r2", "digest", "d2", "text2", [1.0, 0.0])  # different dim
    results = store.search([1.0, 0.0, 0.0], top_k=5)
    assert len(results) == 1
    assert results[0]["record_id"] == "r1"


def test_all_ids_reflects_stored_records(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    store.upsert("r1", "digest", "d1", "text", [1.0])
    store.upsert("r2", "note", "d2", "text2", [2.0])
    assert store.all_ids() == {"r1", "r2"}


def test_search_on_empty_store_returns_empty_list(tmp_path):
    store = SqliteVectorStore(tmp_path / "v.sqlite3")
    assert store.search([1.0, 0.0], top_k=5) == []


# ── build_vector_store fallback behavior ────────────────────────────────

def test_build_vector_store_falls_back_to_sqlite_without_postgres_dsn(tmp_path):
    class FakeConfig:
        POSTGRES_DSN = ""
        SQLITE_VECTOR_DB_PATH = tmp_path / "v.sqlite3"
        EMBEDDING_DIM = 512

    store = build_vector_store(FakeConfig())
    assert isinstance(store, SqliteVectorStore)


def test_build_vector_store_falls_back_when_postgres_unreachable(tmp_path):
    class FakeConfig:
        POSTGRES_DSN = "postgresql://nonexistent:5432/floorwatch"
        SQLITE_VECTOR_DB_PATH = tmp_path / "v.sqlite3"
        EMBEDDING_DIM = 512

    store = build_vector_store(FakeConfig())  # must never raise
    assert isinstance(store, SqliteVectorStore)
