# Floorwatch Rules Engine — Railway deployment image.
#
# Build context MUST be the repo root, not this service's own folder —
# Railway's "Dockerfile Path" setting lets you point at this file while
# still building from root, which is what makes the two COPY lines below
# (skills/lib, config) work. See RAILWAY_DEPLOYMENT.md for the exact
# Railway service settings this requires.
#
# Why the repo-root build context matters: app/config.py resolves
# REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent and
# does sys.path.insert(0, REPO_ROOT / "skills" / "lib") to import
# floorwatch_auth / floorwatch_secrets_guard, plus reads REPO_ROOT/config
# for deployment.env/secrets.env and REPO_ROOT/services for the shared
# auth-secret fallback file. Copying only this service's subfolder (what
# Railway's default Root-Directory-scoped build would do) breaks all of
# that at runtime with ModuleNotFoundError — copying the full tree here,
# with paths preserved, reproduces the same directory layout config.py
# already assumes when run locally.
FROM python:3.13-slim

WORKDIR /app

# System deps for building any C-extension wheels (e.g. websockets,
# pydantic-core) that don't ship a prebuilt wheel for this platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (better layer caching — only reinstalls if
# requirements.txt changes, not on every source edit).
COPY services/floorwatch-rules-engine/requirements.txt ./services/floorwatch-rules-engine/requirements.txt
RUN pip install --no-cache-dir -r services/floorwatch-rules-engine/requirements.txt

# Copy the shared modules this service's config.py imports from/reads —
# same relative layout as the real repo, so REPO_ROOT-based resolution
# in config.py needs zero code changes to work inside this image.
COPY skills/lib/ ./skills/lib/
COPY config/ ./config/

# Copy this service itself. The rest of services/ (floorwatch-intelligence,
# any other sibling) is deliberately NOT copied — this image only needs
# its own service directory plus the two shared folders above.
COPY services/floorwatch-rules-engine/ ./services/floorwatch-rules-engine/

WORKDIR /app/services/floorwatch-rules-engine/app

# Railway maps its dynamic external port to whatever this binds — the
# project's Railway networking config already targets 8080 for this
# service (per the existing README's local-dev convention), so this
# stays fixed rather than reading $PORT.
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
