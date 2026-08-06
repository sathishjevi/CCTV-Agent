"""Rules engine configuration. All values overridable via env vars so the
same code runs unmodified from a laptop dev shell or docker-compose."""

import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_DIR.parent.parent

sys.path.insert(0, str(REPO_ROOT / "skills" / "lib"))
from floorwatch_auth import get_or_create_secret  # noqa: E402
from floorwatch_secrets_guard import load_deployment_config  # noqa: E402

# Loads config/deployment.env then config/secrets.env if present, without
# overriding any real environment variable already set — see
# config/README.md. Purely additive: a deployment that sets real env vars
# via its process manager doesn't need these files at all.
load_deployment_config(REPO_ROOT)

REDIS_URL = os.environ.get("FLOORWATCH_REDIS_URL", "redis://localhost:6379/0")
REDIS_STREAM = os.environ.get("FLOORWATCH_REDIS_STREAM", "floorwatch:events")
REDIS_CONSUMER_GROUP = os.environ.get("FLOORWATCH_REDIS_GROUP", "rules-engine")
REDIS_CONSUMER_NAME = os.environ.get("FLOORWATCH_REDIS_CONSUMER", "rules-engine-1")

ROSTER_PATH = Path(os.environ.get("FLOORWATCH_ROSTER_PATH", SERVICE_DIR / "roster.json"))
ZONES_META_PATH = Path(os.environ.get("FLOORWATCH_ZONES_META_PATH", SERVICE_DIR / "zones_meta.json"))
DIGEST_PATH = Path(os.environ.get("FLOORWATCH_DIGEST_PATH", SERVICE_DIR / "shift_digest.jsonl"))
TASK_TYPE_THRESHOLDS_PATH = Path(os.environ.get(
    "FLOORWATCH_TASK_TYPE_THRESHOLDS_PATH", SERVICE_DIR / "task_type_thresholds.json"))

# Global Constraint 4 — shadow mode before real notifications. Defaults to
# True; must be explicitly flipped off (and only after accuracy validation
# per the brief's go-live checklist, which is Phase 4 scope).
SHADOW_MODE = os.environ.get("FLOORWATCH_SHADOW_MODE", "true").lower() in ("1", "true", "yes")

# Tier 1 — nudge
NUDGE_TIMER_SECONDS = float(os.environ.get("FLOORWATCH_NUDGE_TIMER_SECONDS", 180))       # 3 min
NUDGE_CAP_PER_SHIFT = int(os.environ.get("FLOORWATCH_NUDGE_CAP_PER_SHIFT", 3))

# Tier 2 — supervisor command
COMMAND_TIMER_SECONDS = float(os.environ.get("FLOORWATCH_COMMAND_TIMER_SECONDS", 300))   # 5 min
COMMAND_THROTTLE_PER_HOUR = int(os.environ.get("FLOORWATCH_COMMAND_THROTTLE_PER_HOUR", 5))

# Post-resolution cooldown, all tiers
RESOLVE_COOLDOWN_SECONDS = float(os.environ.get("FLOORWATCH_RESOLVE_COOLDOWN_SECONDS", 900))  # 15 min

# How often the engine's background loop checks for timer expiry
TICK_INTERVAL_SECONDS = float(os.environ.get("FLOORWATCH_TICK_INTERVAL_SECONDS", 1.0))

# ── Part A — effort tracking ──────────────────────────────────────────
REDIS_MOTION_STREAM = os.environ.get("FLOORWATCH_REDIS_MOTION_STREAM", "floorwatch:motion")
REDIS_MOTION_CONSUMER_GROUP = os.environ.get("FLOORWATCH_REDIS_MOTION_GROUP", "rules-engine-effort")

# How often task_active_time_update is emitted for each open task
EFFORT_UPDATE_INTERVAL_SECONDS = float(os.environ.get("FLOORWATCH_EFFORT_UPDATE_INTERVAL_SECONDS", 30))
# Max gap (seconds) between motion samples counted toward active time — caps
# the contribution of a single stale/missed sample rather than assuming a
# person was continuously active for an arbitrarily long silent stretch.
EFFORT_MAX_MOTION_GAP_SECONDS = float(os.environ.get("FLOORWATCH_EFFORT_MAX_MOTION_GAP_SECONDS", 15))
# Don't mid-task nudge before this fraction of the assigned time has elapsed
# (avoids nudging someone 30 seconds into a 60-minute task).
EFFORT_NUDGE_GRACE_RATIO = float(os.environ.get("FLOORWATCH_EFFORT_NUDGE_GRACE_RATIO", 0.3))
# How far active-ratio must trail elapsed-ratio before a mid-task nudge fires.
EFFORT_NUDGE_MARGIN = float(os.environ.get("FLOORWATCH_EFFORT_NUDGE_MARGIN", 0.3))

# ── Phase 4 — real notification channel ───────────────────────────────
# "none" (NoOpSender, same behavior as shadow mode's own suppression),
# "twilio" (SMS), or "fcm" (Firebase Cloud Messaging push). Independent of
# SHADOW_MODE: even with a channel configured, engine.py/effort_engine.py
# never call the sender at all while SHADOW_MODE is True — this is the
# "instantly re-enable shadow mode" flag the brief asks for, since a
# misconfigured NOTIFY_CHANNEL with SHADOW_MODE left True still sends nothing.
NOTIFY_CHANNEL = os.environ.get("FLOORWATCH_NOTIFY_CHANNEL", "none")
CONTACTS_PATH = Path(os.environ.get("FLOORWATCH_CONTACTS_PATH", SERVICE_DIR / "contacts.json"))

TWILIO_ACCOUNT_SID = os.environ.get("FLOORWATCH_TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("FLOORWATCH_TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("FLOORWATCH_TWILIO_FROM_NUMBER", "")

FCM_CREDENTIALS_PATH = os.environ.get("FLOORWATCH_FCM_CREDENTIALS_PATH", "")

# ── Authentication (see skills/lib/floorwatch_auth.py) ─────────────────
# Set FLOORWATCH_AUTH_SECRET explicitly in any real deployment. Left unset,
# a secret is auto-generated once and persisted to a shared file so this
# service and floorwatch-intelligence agree on it without extra config —
# convenient for local/dev, not a substitute for real secret management.
AUTH_SECRET = os.environ.get("FLOORWATCH_AUTH_SECRET") or get_or_create_secret(
    Path(os.environ.get("FLOORWATCH_AUTH_SECRET_PATH", REPO_ROOT / "services" / ".floorwatch_auth_secret")))
USERS_PATH = Path(os.environ.get("FLOORWATCH_USERS_PATH", SERVICE_DIR / "users.json"))
TOKEN_TTL_SECONDS = int(os.environ.get("FLOORWATCH_TOKEN_TTL_SECONDS", 12 * 3600))

# ── CORS (SECURITY_REVIEW.md H1) ────────────────────────────────────────
# No wildcard default. An open CORS policy is a direct amplifier of any
# auth weakness — it turns "reachable on the network" into "reachable by
# any script in a browser tab the caller happens to have open" (a
# CSRF-shaped risk), and with auth now in place a stolen/expired-but-not-
# yet-invalidated token becomes usable from anywhere. Default covers only
# common local-dev origins for the dashboard; set explicitly for real
# deployments.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "FLOORWATCH_CORS_ALLOWED_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080,"
        "http://localhost:8090,http://127.0.0.1:8090,"
        "http://localhost:5500,http://127.0.0.1:5500"
    ).split(",") if o.strip()
]

# ── API docs (SECURITY_REVIEW.md M4) ────────────────────────────────────
# /docs, /redoc, /openapi.json fully document — and let you call — every
# endpoint, mutating ones included. Off by default; opt in for local dev.
DOCS_ENABLED = os.environ.get("FLOORWATCH_DOCS_ENABLED", "false").lower() in ("1", "true", "yes")

# ── Retention (SECURITY_REVIEW.md M1) ───────────────────────────────────
# The brief never specifies a retention window; 90 days is this project's
# chosen default (data-minimization-friendly for a system that otherwise
# leans on anonymity — see Global Constraint 2 — while keeping enough
# history for the shift-digest pattern-tagging job to be useful). Override
# via env; see retention.py for the actual rotation/deletion job.
RETENTION_DAYS = int(os.environ.get("FLOORWATCH_RETENTION_DAYS", 90))

SOURCE_MODEL_VERSION = "floorwatch-rules-engine/0.1.0"

# ── Secrets hygiene (small hardening, not a replacement for a real
#    secrets manager — see config/README.md "What this doesn't solve") ──
from floorwatch_secrets_guard import check_file_permissions, install_stderr_redaction  # noqa: E402

check_file_permissions(REPO_ROOT / "config" / "secrets.env")
check_file_permissions(Path(os.environ.get("FLOORWATCH_AUTH_SECRET_PATH", REPO_ROOT / "services" / ".floorwatch_auth_secret")))
install_stderr_redaction([TWILIO_AUTH_TOKEN, TWILIO_ACCOUNT_SID, AUTH_SECRET])
