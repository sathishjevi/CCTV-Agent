# Floorwatch — Railway Deployment Guide

How to deploy `floorwatch-rules-engine` and `floorwatch-intelligence` as two separate Railway services from this one GitHub repo. Read the whole thing before your first deploy — several defaults that are correct for local dev (`localhost` URLs, file-based fallbacks) are actively wrong on Railway and won't fail loudly.

---

## 1. Why "Root Directory" scoping breaks both services

Both services' `app/config.py` resolve `REPO_ROOT` by walking up from their own file location, then do:

```python
sys.path.insert(0, str(REPO_ROOT / "skills" / "lib"))
from floorwatch_auth import get_or_create_secret, issue_token
```

...and also read `REPO_ROOT / "config"` for `deployment.env`/`secrets.env`, and resolve a shared auth-secret fallback file at `REPO_ROOT / "services" / ".floorwatch_auth_secret"`.

Railway's normal per-service **Root Directory** setting scopes the build to only that subfolder — it never sees `skills/lib/` or `config/`, which are siblings, not children, of `services/floorwatch-rules-engine/`. The build succeeds but the container crashes on startup with `ModuleNotFoundError: No module named 'floorwatch_auth'` (or a `secrets_guard` import), because that folder simply isn't there.

**Fix**: build from the repo root using each service's own `Dockerfile`, which `COPY`s `skills/lib/`, `config/`, and the service folder together — reproducing the same relative layout `config.py` already assumes locally. This means:

- **Root Directory**: leave **blank** (repo root) for both services in Railway.
- **Dockerfile Path**: set explicitly per service —
  - `services/floorwatch-rules-engine/Dockerfile`
  - `services/floorwatch-intelligence/Dockerfile`
- Do **not** use Railway's default "detect and build from Root Directory" path — both services need the Dockerfile-based build with root context.

---

## 2. Known limitation — `shift_digest.jsonl` doesn't cross the container boundary

**Flagging this as a decision needed, not something silently patched.** `floorwatch-rules-engine` writes Tier-3 escalation events live, in append mode, to its own local file (`services/floorwatch-rules-engine/shift_digest.jsonl`, on *that* container's filesystem). `floorwatch-intelligence`'s default `FLOORWATCH_DIGEST_PATH` points at that same path — which, on Railway, is a **different container with no shared filesystem**. Setting `FLOORWATCH_DIGEST_PATH` as an env var doesn't fix this either; there's nothing on intelligence's side to point it at.

**This does not crash anything** — `floorwatch-intelligence`'s digest loader returns an empty list if the path doesn't exist, so the service starts and runs fine. The actual effect is quieter and easy to miss: the supervisor chat assistant will never surface shift-digest history (past zone escalations, coverage-gap narratives) in its answers — only incident notes (which live locally on that service, unaffected) and live rules-engine status (fetched over HTTP, which already works correctly).

Two real fixes exist, neither implemented here (this is deployment tooling, not application logic — see this project's global constraints):

1. **Add a small read-only HTTP endpoint to `floorwatch-rules-engine`** exposing its digest data, and change `floorwatch-intelligence`'s ingest to pull from that instead of a local path. Most consistent with how the two services already talk to each other for live status.
2. **A Railway persistent volume** mounted on rules-engine, with intelligence's `FLOORWATCH_DIGEST_PATH` pointed at a synced location. Fights Railway's per-service isolation more than option 1.

Until one of these is built, treat shift-digest-grounded chat answers as **not available** in a two-separate-Railway-services deployment.

---

## 3. Other local-file persistence — none of it survives a redeploy without a volume

Railway's container filesystem is ephemeral on redeploy. Beyond the sqlite vector-store fallback already noted in `SETUP_AUTH_AND_CONFIG.md`, these are also local files, written at runtime, on each service's own disk:

| Service | File | What's lost on redeploy without a volume |
|---|---|---|
| `floorwatch-rules-engine` | `users.json` | Every supervisor/viewer account — you'd have to re-run `create_user.py` after every redeploy |
| `floorwatch-rules-engine` | `shift_digest.jsonl` | Escalation history (also the cross-service gap above) |
| `floorwatch-rules-engine` | `roster.json`, `zones_meta.json`, `contacts.json`, `task_type_thresholds.json` | Whatever's been edited at runtime past what's checked into the repo — if these are only ever edited via the repo, this doesn't apply |
| `floorwatch-intelligence` | `vectors.sqlite3` | The embedded/searchable shift-digest and incident-note index |
| Both | `.floorwatch_auth_secret` (only if `FLOORWATCH_AUTH_SECRET` env var is left unset) | Every existing login session invalidates on redeploy |

For a real pilot beyond a quick demo, attach a Railway volume to `floorwatch-rules-engine` for `users.json` at minimum, and set `FLOORWATCH_AUTH_SECRET` explicitly (see §4) so logins survive redeploys regardless of volumes.

**Bootstrapping the first user account**: since `users.json` won't exist on first deploy, run `create_user.py` once against the running container — Railway's shell/one-off-command feature (`railway run`, or the equivalent in the dashboard) is the way to do this without a persistent interactive shell. Re-run it after every redeploy unless you've attached a volume.

---

## 4. Environment variables

### Shared — must be the *exact same value* on both services

| Variable | Notes |
|---|---|
| `FLOORWATCH_AUTH_SECRET` | Generate once: `python -c "import secrets; print(secrets.token_hex(32))"`. Set it explicitly and identically on both Railway services. **Do not** leave this unset and rely on the file-based `get_or_create_secret()` fallback — each container would generate its own independent secret, and a login token issued by one service would fail validation on the other. |

### `floorwatch-rules-engine` — service-specific

| Variable | Required? | Notes |
|---|---|---|
| `FLOORWATCH_REDIS_URL` | Yes | Point at a real Redis instance — Railway's Redis plugin, or external. This is the **only** external datastore this service uses; there is no Postgres/database variable anywhere in this service (verified — no `psycopg`/`sqlalchemy`/DSN references or dependency exist in this service's code or `requirements.txt`; Postgres only applies to `floorwatch-intelligence`, below). |
| `FLOORWATCH_CORS_ALLOWED_ORIGINS` | Recommended | Set to your real deployed dashboard origin(s) — see §5. |
| `FLOORWATCH_DOCS_ENABLED` | Recommended | See §5. |
| Everything else in `config/deployment.env.template` | Optional | Timers, thresholds, retention, notify channel — all have working code-level defaults; see §6 for why the template file itself doesn't travel with the image. |

### `floorwatch-intelligence` — service-specific

| Variable | Required? | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` (or `FLOORWATCH_LLM_API_KEY` for a non-Anthropic provider) | Yes, for `/api/chat` to work at all | Returns a clear 503 rather than crashing if unset. |
| `FLOORWATCH_RULES_ENGINE_URL` | Yes | **Must be `floorwatch-rules-engine`'s real Railway-generated public URL** (`https://<your-rules-engine-service>.up.railway.app`), not `http://localhost:8080` — the local-dev default will simply fail to connect across two separate containers. |
| `FLOORWATCH_POSTGRES_DSN` | Optional | Empty falls back to the sqlite vector store. **Known limitation for the pilot, not a silent gotcha**: sqlite's file (`vectors.sqlite3`) won't persist reliably across redeploys on Railway's ephemeral filesystem without a volume — the embedded index would need re-ingesting from scratch after every redeploy. For anything beyond a short pilot, set this to a real Postgres+pgvector instance instead. |
| `FLOORWATCH_CORS_ALLOWED_ORIGINS` | Recommended | See §5. |
| `FLOORWATCH_DOCS_ENABLED` | Recommended | See §5. |

---

## 5. Review before going beyond a quick demo

Both `config.py` files default `FLOORWATCH_DOCS_ENABLED=false` and a `localhost`-only `FLOORWATCH_CORS_ALLOWED_ORIGINS` — correct for local dev, wrong once deployed. Per the existing comments in both files referencing `SECURITY_REVIEW.md`:

- **`FLOORWATCH_CORS_ALLOWED_ORIGINS`**: set explicitly to wherever your actual dashboard is served from. Leaving the `localhost` default means no real deployed dashboard origin can call the API at all (which fails safe, but confusingly) — it is not a wildcard, so this isn't an open-CORS risk, just something that needs a deliberate real value.
- **`FLOORWATCH_DOCS_ENABLED`**: leave `false` in production. Only set `true` temporarily if you need to inspect `/docs`/`/redoc`/`/openapi.json` on the deployed instance.

---

## 6. A note on `config/deployment.env` and `config/secrets.env`

Both Dockerfiles `COPY config/ ./config/` — but the repo root's `.dockerignore` deliberately excludes `config/*.env` (only the `.template` files ship). **This is intentional, not a bug**: real secrets should never be baked into a container image; Railway's own environment variables are the source of truth at runtime instead (this mirrors the "real env var always wins" precedence already documented in `config/README.md`).

Practical effect: `config/deployment.env.template` and `config/secrets.env.template` are useful as **documentation** of every variable this system understands, but neither template's values travel into the deployed container. Anything you want to override from its code-level default must be set directly as a Railway environment variable on the relevant service — not by editing a local `deployment.env` and expecting it to ship.

---

## 7. Quick checklist

- [ ] Both services: Root Directory blank, Dockerfile Path set to the correct per-service path
- [ ] `FLOORWATCH_AUTH_SECRET` generated once, set identically on both services
- [ ] `floorwatch-rules-engine`: `FLOORWATCH_REDIS_URL` set to a real Redis instance
- [ ] `floorwatch-intelligence`: `ANTHROPIC_API_KEY` (or equivalent) set
- [ ] `floorwatch-intelligence`: `FLOORWATCH_RULES_ENGINE_URL` set to rules-engine's real Railway public URL
- [ ] `FLOORWATCH_CORS_ALLOWED_ORIGINS` set to your real dashboard origin(s) on both services
- [ ] `FLOORWATCH_DOCS_ENABLED` left `false` on both (or explicitly reviewed)
- [ ] First supervisor account created via `create_user.py` against the deployed rules-engine container
- [ ] Aware of and have made a decision on the `shift_digest.jsonl` cross-service gap (§2) — even if the decision is "acceptable for now, revisit before a real pilot"
- [ ] Aware of the sqlite/local-file persistence limitations (§3) if not attaching a Railway volume
