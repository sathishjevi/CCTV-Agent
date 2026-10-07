"""PostgreSQL access shared by every Floorwatch store: one connection pool per
database, with timeouts, health checks, slow-query logging and an audit trail
of schema changes.

WHY THIS EXISTS (checklist: "connection pooling", "slow query logging",
"schema changes tracked and auditable", "plaintext connections rejected")
  * Every store used to open a brand-new connection for every single call
    (`psycopg.connect(...)` per query). Under load that exhausts the
    database's connection limit; a pool bounds it and reuses connections.
  * No statement ever timed out, so one stuck query could hold a connection —
    and, with enough of them, every connection — forever. Each new connection
    now sets statement / lock / idle-in-transaction timeouts.
  * Nothing recorded when a table or column was created. `apply_schema` runs
    each (idempotent) DDL statement and writes the first application of each
    one to `floorwatch_schema_log`.
  * `FLOORWATCH_DB_REQUIRE_TLS=true` makes a plaintext connection an error.
    Off by default because whether Railway's private network offers TLS isn't
    something this code can assume; /api/admin/diagnostics reports what the
    connection actually is so the setting can be turned on knowingly.

Query PARAMETERS are never logged — only the statement text with literals
masked, and only when it was slow.

psycopg / psycopg_pool are imported lazily: a deployment on the local-file
fallback doesn't need them installed."""

import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterable

from floorwatch_logging import get_logger

log = get_logger("floorwatch.db")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


POOL_MIN = _int_env("FLOORWATCH_DB_POOL_MIN", 1)
POOL_MAX = _int_env("FLOORWATCH_DB_POOL_MAX", 10)
POOL_TIMEOUT_S = _int_env("FLOORWATCH_DB_POOL_TIMEOUT_SECONDS", 10)
STATEMENT_TIMEOUT_MS = _int_env("FLOORWATCH_DB_STATEMENT_TIMEOUT_MS", 30_000)
LOCK_TIMEOUT_MS = _int_env("FLOORWATCH_DB_LOCK_TIMEOUT_MS", 10_000)
IDLE_IN_TX_TIMEOUT_MS = _int_env("FLOORWATCH_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 30_000)
SLOW_QUERY_MS = _int_env("FLOORWATCH_DB_SLOW_QUERY_MS", 500)
REQUIRE_TLS = os.environ.get("FLOORWATCH_DB_REQUIRE_TLS", "false").lower() in ("1", "true", "yes")
APPLICATION_NAME = os.environ.get("FLOORWATCH_DB_APPLICATION_NAME", "floorwatch")

_pools: dict = {}
_pools_lock = threading.Lock()
_connection_class = None

def safe_target(dsn: str) -> str:
    """`host:port/database` for log lines — never the user or the password. A
    connection string is a credential; it must not be printed, even though
    stderr is also run through a redaction filter (belt AND braces)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(dsn)
        if u.hostname:
            return f"{u.hostname}{':' + str(u.port) if u.port else ''}{u.path or ''}"
    except ValueError:
        pass
    return "the configured database"


_LITERAL = re.compile(r"'(?:[^']|'')*'|\b\d+\b")


def describe_statement(query) -> str:
    """A loggable form of a statement: whitespace collapsed, literals masked,
    cut to 120 characters. (Parameters are bound separately and never get here.)"""
    text = re.sub(r"\s+", " ", str(query)).strip()
    return _LITERAL.sub("?", text)[:120]


def _timed_connection_class():
    global _connection_class
    if _connection_class is None:
        import psycopg

        class TimedConnection(psycopg.Connection):
            def execute(self, query, params=None, *, prepare=None, binary=False):
                start = time.perf_counter()
                try:
                    return super().execute(query, params, prepare=prepare, binary=binary)
                finally:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    if SLOW_QUERY_MS and elapsed_ms >= SLOW_QUERY_MS:
                        log(f"slow query ({elapsed_ms:.0f} ms): {describe_statement(query)}",
                            level="warning", duration_ms=round(elapsed_ms))

        _connection_class = TimedConnection
    return _connection_class


def _configure(conn):
    """Runs once for each NEW physical connection."""
    conn.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
    conn.execute(f"SET lock_timeout = {int(LOCK_TIMEOUT_MS)}")
    conn.execute(f"SET idle_in_transaction_session_timeout = {int(IDLE_IN_TX_TIMEOUT_MS)}")
    if REQUIRE_TLS:
        row = conn.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
        if not row or not row[0]:
            raise RuntimeError("FLOORWATCH_DB_REQUIRE_TLS is set but this database connection is not encrypted")


def get_pool(dsn: str):
    """The shared pool for this database, created on first use. Raises (and
    caches nothing) if the database can't be reached within a few seconds —
    callers rely on that to fall back to local files, as they always did."""
    with _pools_lock:
        pool = _pools.get(dsn)
        if pool is not None:
            return pool
        from psycopg_pool import ConnectionPool
        pool = ConnectionPool(
            conninfo=dsn, min_size=POOL_MIN, max_size=max(POOL_MIN, POOL_MAX), timeout=POOL_TIMEOUT_S,
            max_lifetime=3600, max_idle=300, open=False,
            connection_class=_timed_connection_class(),
            kwargs={"autocommit": True, "connect_timeout": 5, "application_name": APPLICATION_NAME},
            configure=_configure, check=ConnectionPool.check_connection,
            name="floorwatch-db",
        )
        try:
            pool.open(wait=True, timeout=8)
        except Exception:
            pool.close()
            raise
        _pools[dsn] = pool
        return pool


@contextmanager
def connection(dsn: str):
    """`with floorwatch_db.connection(dsn) as conn:` — a pooled, autocommit
    connection, returned to the pool on exit."""
    with get_pool(dsn).connection() as conn:
        yield conn


def close_all():
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


_SCHEMA_LOG_DDL = """
CREATE TABLE IF NOT EXISTS floorwatch_schema_log (
    store TEXT NOT NULL,
    statement_sha256 TEXT NOT NULL,
    statement TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store, statement_sha256)
);
"""


def apply_schema(conn, store: str, statements: Iterable[str]):
    """Runs each DDL statement (all are idempotent: IF NOT EXISTS) and records
    the FIRST time each one was applied, with a log line, so "what changed in
    the schema and when" is answerable from the database itself instead of
    from nobody's memory."""
    conn.execute(_SCHEMA_LOG_DDL)
    for statement in statements:
        conn.execute(statement)
        digest = hashlib.sha256(statement.strip().encode("utf-8")).hexdigest()
        first_time = conn.execute(
            "INSERT INTO floorwatch_schema_log (store, statement_sha256, statement) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            (store, digest, statement.strip())).fetchone()
        if first_time:
            log(f"schema change applied for '{store}': {describe_statement(statement)}",
                level="info", schema_store=store)


def tls_status(dsn: str) -> dict:
    """Is the connection the application actually uses encrypted? Asked of the
    server itself (pg_stat_ssl), not inferred from the connection string."""
    try:
        with connection(dsn) as conn:
            row = conn.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone()
            server_version = conn.execute("SHOW server_version").fetchone()[0]
        return {"configured": True, "tls": bool(row[0]) if row else None,
                "tls_version": row[1] if row else None, "server_version": server_version,
                "tls_required_by_config": REQUIRE_TLS, "pool_max": POOL_MAX}
    except Exception as e:
        return {"configured": True, "error": type(e).__name__}
