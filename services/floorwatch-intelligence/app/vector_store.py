"""
Vector store abstraction — build brief Phase 5 task 1: "Add pgvector to
the existing Postgres/Timescale instance used for event storage (reuse
infra — do not stand up a separate vector database for a pilot-scale
deployment)."

**Deviation, flagged explicitly** (see PHASE_5_NOTES.md for the full
writeup): this codebase's actual event store through Phases 1-4 is Redis
Streams (transient, consumed-and-acked) plus a flat JSONL shift-digest
file (services/floorwatch-rules-engine/shift_digest.jsonl) — there is no
Postgres/Timescale instance anywhere in this repo's docker-compose or
elsewhere for this phase to "reuse." Docker Desktop also isn't runnable
headlessly in this dev sandbox (same limitation noted since
PHASE_1_NOTES.md), so standing one up here wasn't possible either.

So: `PgVectorStore` below is real, schema-complete production code for
when a real Postgres+pgvector instance IS available (the brief's intended
path) — it has not been exercised against a real database in this
sandbox. `SqliteVectorStore` is the fallback actually used for all
development/testing here: same interface, pure Python + numpy cosine
similarity, zero extra infrastructure. `build_vector_store()` tries
Postgres first and falls back automatically, logging clearly which one
is in use — the same honest-fallback pattern used throughout this
project (floorwatch-pose's MediaPipe/frame-diff fallback, the YOLO26
stub detector, etc.).

Whichever store is active, it is used ONLY for this phase's own
read-only-of-the-live-system index — populating/querying it never
mutates zone state, task state, or the notification system
(Global Constraint 7).
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from floorwatch_logging import get_logger

log = get_logger("intelligence.vector_store")


class SqliteVectorStore:
    """Fallback vector store: sqlite3 (stdlib) for storage, numpy cosine
    similarity for search. O(n) brute-force scan — perfectly adequate at
    pilot scale (hundreds to low thousands of shift digests/notes), not
    intended to scale past that without swapping in PgVectorStore."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        return sqlite3.connect(str(self.db_path))

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vectors (
                    record_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    metadata TEXT NOT NULL
                )
            """)

    def upsert(self, record_id: str, source_type: str, source_ref: str, text: str,
               embedding, metadata: Optional[dict] = None):
        emb_bytes = np.asarray(embedding, dtype=np.float32).tobytes()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO vectors (record_id, source_type, source_ref, text, embedding, metadata) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET "
                "source_type=excluded.source_type, source_ref=excluded.source_ref, "
                "text=excluded.text, embedding=excluded.embedding, metadata=excluded.metadata",
                (record_id, source_type, source_ref, text, emb_bytes, json.dumps(metadata or {})),
            )

    def all_ids(self) -> set:
        with self._connect() as conn:
            return {row[0] for row in conn.execute("SELECT record_id FROM vectors")}

    def search(self, query_embedding, top_k: int = 5) -> list:
        q = np.asarray(query_embedding, dtype=np.float32)
        q_norm = float(np.linalg.norm(q)) or 1e-9
        results = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_id, source_type, source_ref, text, embedding, metadata FROM vectors")
            for record_id, source_type, source_ref, text, emb_bytes, metadata_json in rows:
                v = np.frombuffer(emb_bytes, dtype=np.float32)
                if v.shape != q.shape:
                    continue  # embedding dimension mismatch (e.g. provider changed) — skip, don't crash
                v_norm = float(np.linalg.norm(v)) or 1e-9
                score = float(np.dot(q, v) / (q_norm * v_norm))
                results.append({
                    "record_id": record_id, "source_type": source_type, "source_ref": source_ref,
                    "text": text, "metadata": json.loads(metadata_json), "score": score,
                })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]

    def get(self, record_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_id, source_type, source_ref, text, metadata FROM vectors WHERE record_id=?",
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return {"record_id": row[0], "source_type": row[1], "source_ref": row[2],
                "text": row[3], "metadata": json.loads(row[4])}

    def delete_before(self, cutoff_iso: str) -> int:
        """SECURITY_REVIEW.md M1 — retention. Deletes rows whose
        metadata.timestamp (set at ingest — see ingest.py) is older than
        cutoff_iso. ISO8601 strings compare correctly lexicographically,
        so no parsing needed. Rows with no timestamp in metadata are kept
        — never guessed away. Returns the number of rows deleted."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM vectors WHERE json_extract(metadata, '$.timestamp') IS NOT NULL "
                "AND json_extract(metadata, '$.timestamp') < ?",
                (cutoff_iso,),
            )
            return cur.rowcount


class PgVectorStore:
    """Real Postgres+pgvector implementation — the brief's intended path.
    Requires `CREATE EXTENSION vector` privileges. Not exercised against
    a real database in this sandbox; see module docstring."""

    SCHEMA_SQL_TEMPLATE = """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS floorwatch_vectors (
            record_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            text TEXT NOT NULL,
            embedding VECTOR({dim}) NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS floorwatch_vectors_embedding_idx
            ON floorwatch_vectors USING ivfflat (embedding vector_cosine_ops);
    """

    def __init__(self, dsn: str, embedding_dim: int):
        import psycopg
        self.dsn = dsn
        self.embedding_dim = embedding_dim
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(self.SCHEMA_SQL_TEMPLATE.format(dim=embedding_dim))

    def upsert(self, record_id: str, source_type: str, source_ref: str, text: str,
               embedding, metadata: Optional[dict] = None):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(
                "INSERT INTO floorwatch_vectors (record_id, source_type, source_ref, text, embedding, metadata) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (record_id) DO UPDATE SET "
                "source_type=EXCLUDED.source_type, source_ref=EXCLUDED.source_ref, text=EXCLUDED.text, "
                "embedding=EXCLUDED.embedding, metadata=EXCLUDED.metadata",
                (record_id, source_type, source_ref, text, list(embedding), json.dumps(metadata or {})),
            )

    def search(self, query_embedding, top_k: int = 5) -> list:
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
            rows = conn.execute(
                "SELECT record_id, source_type, source_ref, text, metadata, "
                "1 - (embedding <=> %s) AS score FROM floorwatch_vectors "
                "ORDER BY embedding <=> %s LIMIT %s",
                (list(query_embedding), list(query_embedding), top_k),
            ).fetchall()
        return [
            {"record_id": r[0], "source_type": r[1], "source_ref": r[2],
             "text": r[3], "metadata": r[4], "score": float(r[5])}
            for r in rows
        ]

    def all_ids(self) -> set:
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
            return {row[0] for row in conn.execute("SELECT record_id FROM floorwatch_vectors")}

    def get(self, record_id: str) -> Optional[dict]:
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT record_id, source_type, source_ref, text, metadata "
                "FROM floorwatch_vectors WHERE record_id=%s",
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return {"record_id": row[0], "source_type": row[1], "source_ref": row[2],
                "text": row[3], "metadata": row[4]}

    def delete_before(self, cutoff_iso: str) -> int:
        """Same contract as SqliteVectorStore.delete_before — see its
        docstring. Not exercised against a real Postgres instance in this
        sandbox (see this class's own docstring)."""
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
            cur = conn.execute(
                "DELETE FROM floorwatch_vectors WHERE metadata->>'timestamp' IS NOT NULL "
                "AND metadata->>'timestamp' < %s",
                (cutoff_iso,),
            )
            return cur.rowcount


def build_vector_store(config):
    """Tries Postgres+pgvector first (the brief's intended path); falls
    back to SqliteVectorStore automatically if unreachable/unconfigured."""
    if getattr(config, "POSTGRES_DSN", None):
        try:
            store = PgVectorStore(config.POSTGRES_DSN, config.EMBEDDING_DIM)
            log(f"Using PgVectorStore at {config.POSTGRES_DSN}")
            return store
        except Exception as e:
            log(f"could not connect to Postgres+pgvector ({e}) — "
                f"falling back to SqliteVectorStore. This is expected in "
                f"this dev sandbox; see PHASE_5_NOTES.md.", level="warning")
    else:
        log("No POSTGRES_DSN configured — using SqliteVectorStore (dev/pilot fallback).")
    return SqliteVectorStore(config.SQLITE_VECTOR_DB_PATH)
