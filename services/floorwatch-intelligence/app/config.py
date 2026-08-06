"""Floorwatch supervisor-intelligence service configuration."""

import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_DIR.parent.parent

sys.path.insert(0, str(REPO_ROOT / "skills" / "lib"))
from floorwatch_auth import get_or_create_secret, issue_token  # noqa: E402
from floorwatch_secrets_guard import load_deployment_config  # noqa: E402

# Loads config/deployment.env then config/secrets.env if present, without
# overriding any real environment variable already set — see
# config/README.md. Same as floorwatch-rules-engine's config.py.
load_deployment_config(REPO_ROOT)

# ── Vector store (see vector_store.py docstring for the Postgres deviation) ──
POSTGRES_DSN = os.environ.get("FLOORWATCH_POSTGRES_DSN", "")  # empty -> sqlite fallback
SQLITE_VECTOR_DB_PATH = Path(os.environ.get(
    "FLOORWATCH_SQLITE_VECTOR_DB_PATH", SERVICE_DIR / "vectors.sqlite3"))

# ── Embeddings (see embeddings.py docstring) ──
EMBEDDING_PROVIDER = os.environ.get("FLOORWATCH_EMBEDDING_PROVIDER", "tfidf")  # "voyage" | "tfidf"
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
VOYAGE_MODEL = os.environ.get("FLOORWATCH_VOYAGE_MODEL", "voyage-3")
EMBEDDING_DIM = int(os.environ.get("FLOORWATCH_EMBEDDING_DIM", 512))  # must match provider's output dim

# ── Data sources this phase reads (read-only) ──
DIGEST_PATH = Path(os.environ.get(
    "FLOORWATCH_DIGEST_PATH",
    REPO_ROOT / "services" / "floorwatch-rules-engine" / "shift_digest.jsonl"))
INCIDENT_NOTES_PATH = Path(os.environ.get(
    "FLOORWATCH_INCIDENT_NOTES_PATH", SERVICE_DIR / "incident_notes.jsonl"))

# ── Rules engine — the only place live status is read from (read-only HTTP) ──
RULES_ENGINE_BASE_URL = os.environ.get("FLOORWATCH_RULES_ENGINE_URL", "http://localhost:8080")

# ── LLM (Claude) ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("FLOORWATCH_ANTHROPIC_MODEL", "claude-sonnet-4-5")
MAX_TOOL_ITERATIONS = int(os.environ.get("FLOORWATCH_MAX_TOOL_ITERATIONS", 5))

RETRIEVAL_TOP_K = int(os.environ.get("FLOORWATCH_RETRIEVAL_TOP_K", 5))

# ── Authentication (see skills/lib/floorwatch_auth.py) ─────────────────
# Same shared-secret resolution as floorwatch-rules-engine's config.py, so
# a login issued by either service (in practice, only rules-engine issues
# real supervisor/viewer logins) is valid on both without a network call
# between them. Set FLOORWATCH_AUTH_SECRET explicitly in any real deployment.
AUTH_SECRET = os.environ.get("FLOORWATCH_AUTH_SECRET") or get_or_create_secret(
    Path(os.environ.get("FLOORWATCH_AUTH_SECRET_PATH", REPO_ROOT / "services" / ".floorwatch_auth_secret")))

# This service's own token for its server-to-server GETs against the rules
# engine (tools.py). Deliberately issued at "viewer" scope — never a role
# that could pass require_supervisor there — so a bug in the read-only tool
# layer can't accidentally mutate rules-engine state even if it tried; see
# Global Constraint 7 in main.py's docstring.
SERVICE_TOKEN = issue_token(AUTH_SECRET, "floorwatch-intelligence", "viewer", ttl_seconds=365 * 24 * 3600)

# ── CORS (SECURITY_REVIEW.md H1) ────────────────────────────────────────
# Same rationale as floorwatch-rules-engine's config.py — no wildcard
# default. The chat UI is served same-origin (GET "/"), so this mainly
# guards against any other page trying to call this API cross-origin.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "FLOORWATCH_CORS_ALLOWED_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080,"
        "http://localhost:8090,http://127.0.0.1:8090,"
        "http://localhost:5500,http://127.0.0.1:5500"
    ).split(",") if o.strip()
]

# ── API docs (SECURITY_REVIEW.md M4) ────────────────────────────────────
DOCS_ENABLED = os.environ.get("FLOORWATCH_DOCS_ENABLED", "false").lower() in ("1", "true", "yes")

# ── Rate limiting (SECURITY_REVIEW.md H2) ───────────────────────────────
# Per-caller (by authenticated username) limit on /api/chat — bounds
# Anthropic API cost/DoS exposure once a real ANTHROPIC_API_KEY is set.
CHAT_RATE_LIMIT_PER_MINUTE = int(os.environ.get("FLOORWATCH_CHAT_RATE_LIMIT_PER_MINUTE", 10))

# ── Retention (SECURITY_REVIEW.md M1) ───────────────────────────────────
# Same default/rationale as floorwatch-rules-engine's config.py — kept in
# sync deliberately (a system-wide retention window, not per-service).
RETENTION_DAYS = int(os.environ.get("FLOORWATCH_RETENTION_DAYS", 90))

# ── Secrets hygiene (small hardening, not a replacement for a real
#    secrets manager — see config/README.md "What this doesn't solve") ──
from floorwatch_secrets_guard import check_file_permissions, install_stderr_redaction  # noqa: E402

check_file_permissions(REPO_ROOT / "config" / "secrets.env")
check_file_permissions(Path(os.environ.get("FLOORWATCH_AUTH_SECRET_PATH", REPO_ROOT / "services" / ".floorwatch_auth_secret")))
install_stderr_redaction([VOYAGE_API_KEY, ANTHROPIC_API_KEY, POSTGRES_DSN, AUTH_SECRET])
