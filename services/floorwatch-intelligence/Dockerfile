# Floorwatch Supervisor Intelligence — Railway deployment image.
#
# Build context MUST be the repo root, not this service's own folder —
# same reasoning as services/floorwatch-rules-engine/Dockerfile (see that
# file's header comment and RAILWAY_DEPLOYMENT.md for the full
# explanation of why REPO_ROOT-based imports require this).
#
# Known limitation NOT fixed by this Dockerfile — see
# RAILWAY_DEPLOYMENT.md's "shift_digest.jsonl cross-service dependency"
# section: this service's default FLOORWATCH_DIGEST_PATH points at
# floorwatch-rules-engine's local shift_digest.jsonl file, which lives on
# a *different* Railway container's filesystem in a two-separate-services
# deployment. It won't exist here regardless of this Dockerfile — this is
# an application-level gap flagged for a decision, not something a
# Dockerfile can resolve.
FROM python:3.14-slim

WORKDIR /app

# System deps for building any C-extension wheels (scikit-learn, numpy,
# psycopg) that don't ship a prebuilt wheel for this platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (better layer caching).
COPY services/floorwatch-intelligence/requirements.txt ./services/floorwatch-intelligence/requirements.txt
RUN pip install --no-cache-dir -r services/floorwatch-intelligence/requirements.txt

# Copy the shared modules this service's config.py imports from/reads —
# same relative layout as the real repo.
COPY skills/lib/ ./skills/lib/
COPY config/ ./config/

# main.py serves the chat UI at "/" from REPO_ROOT/dashboard/floorwatch_chat.html
# (see app/main.py's CHAT_UI_PATH) — that folder lives at the repo root,
# not inside this service's own folder, so it needs its own explicit COPY
# or "/" 404s in the deployed container even though everything else works.
COPY dashboard/ ./dashboard/

# Copy this service itself. floorwatch-rules-engine's own folder is
# deliberately NOT copied here — the two services communicate over HTTP
# (FLOORWATCH_RULES_ENGINE_URL), not via a shared filesystem, except for
# the shift_digest.jsonl gap noted above.
COPY services/floorwatch-intelligence/ ./services/floorwatch-intelligence/

WORKDIR /app/services/floorwatch-intelligence/app

# Railway maps its dynamic external port to whatever this binds. Fixed at
# 8090, NOT 8080 — this service's own README and both services' CORS
# defaults already establish 8090 as this service's port (8080 is
# floorwatch-rules-engine's). Set Railway's target port for this service
# to 8090 to match (see RAILWAY_DEPLOYMENT.md).
EXPOSE 8090
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
