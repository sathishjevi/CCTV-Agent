"""Structured JSON logging — shared between both Floorwatch services.

Production-readiness gap: logging was plain print()-to-stderr text lines
(f"[service-tag] message"), readable in Railway's raw log stream but not
parseable by any real log aggregator/monitoring tool without custom
regex. This doesn't change WHERE logs go (still stdout/stderr — Railway
captures that either way) — it changes their SHAPE, so a future
aggregator (Datadog, CloudWatch, an ELK stack, whatever gets chosen)
can filter/query by level, service, and timestamp without parsing free
text. This is groundwork, not monitoring itself — see
DATA_PROTECTION_SECURITY_ANALYSIS.md's companion production-readiness
list ("no monitoring/alerting/error-tracking — just Railway's raw log
stream"); an aggregator still needs to be chosen and wired up separately,
and this module doesn't attempt that on its own.

Deliberately NOT applied to the top-level operator-run scripts
(retention.py, create_user.py, generate_admin_sql.py,
migrate_users_to_postgres.py, go_live_checklist.py, shift_digest_job.py)
— those produce human-readable console output for someone running them
interactively (SQL to paste, a checklist report, a user listing), and
JSON-wrapping that output would make it actively harder to read for its
actual purpose. This module is for the always-running FastAPI services'
own operational logging only.
"""

import json
import sys
from datetime import datetime, timezone
from typing import Callable


def get_logger(service: str) -> Callable[..., None]:
    """Returns a log(message, level="info", **fields) function that
    prints one JSON line per call to stderr — same destination as the
    print()-based logging this replaces, so Railway's log stream
    captures it identically either way.

    `service` identifies the calling component (e.g. "rules-engine",
    "rules-engine.cluster_bus") — kept as a plain string, not a fixed
    enum, since components get added over time and this shouldn't need
    a central registry to extend. The existing per-file [tag] prefixes
    (e.g. "[cluster_bus]") map directly onto this — same distinguishing
    information, now a structured field instead of embedded text.

    Extra keyword arguments become additional top-level JSON fields —
    e.g. log("deactivated account", username="alice") — for call sites
    with a genuinely structured value worth filtering/querying on
    directly. Most call sites won't need this; passing only a message
    (the same shape every existing call site already uses) remains
    fully valid and is the common case."""

    def log(message: str, level: str = "info", **fields):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "service": service,
            "message": message,
        }
        record.update(fields)
        print(json.dumps(record), file=sys.stderr, flush=True)

    return log
