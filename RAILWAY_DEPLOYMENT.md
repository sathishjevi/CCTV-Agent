# Floorwatch — Railway Deployment Guide

How to deploy `floorwatch-rules-engine`, `floorwatch-intelligence`, and `floorwatch-pipeline` as three separate Railway services from this one GitHub repo. Read the whole thing before your first deploy — several defaults that are correct for local dev (`localhost` URLs, file-based fallbacks) are actively wrong on Railway and won't fail loudly.

**`floorwatch-pipeline` is the odd one out**, and has its own dedicated README at `services/floorwatch-pipeline/README.md` — it's the CCTV ingestion→detection→coverage/pose pipeline (`tools/run_pipeline.py`), containerized so it can run as an always-on Railway service instead of a manually-launched local script. It only works for cloud-reachable CCTV sources (S3/Azure/GCS/EZVIZ/a third-party HTTP API) — RTSP and local-folder cameras need a machine that can actually reach that camera network or filesystem, which Railway isn't. Everything below through §6 applies to all three services unless a section says otherwise; §4 has a dedicated `floorwatch-pipeline` subsection.

---

## 1. Why "Root Directory" scoping breaks all three services

Both services' `app/config.py` resolve `REPO_ROOT` by walking up from their own file location, then do:

```python
sys.path.insert(0, str(REPO_ROOT / "skills" / "lib"))
from floorwatch_auth import get_or_create_secret, issue_token
```

...and also read `REPO_ROOT / "config"` for `deployment.env`/`secrets.env`, and resolve a shared auth-secret fallback file at `REPO_ROOT / "services" / ".floorwatch_auth_secret"`.

Railway's normal per-service **Root Directory** setting scopes the build to only that subfolder — it never sees `skills/lib/` or `config/`, which are siblings, not children, of `services/floorwatch-rules-engine/`. The build succeeds but the container crashes on startup with `ModuleNotFoundError: No module named 'floorwatch_auth'` (or a `secrets_guard` import), because that folder simply isn't there.

**Fix**: build from the repo root using each service's own `Dockerfile`, which `COPY`s `skills/lib/`, `config/`, and the service folder together — reproducing the same relative layout `config.py` already assumes locally. This means:

- **Root Directory**: leave **blank** (repo root) for all three services in Railway.
- **Builder**: must be set to **`Dockerfile`** explicitly — Railway defaults new services to its own auto-detecting builder (Railpack, formerly Nixpacks), and **having a `Dockerfile` in the repo does not switch this automatically.** This is the single most common way this deployment breaks — see §1a below for exactly what it looks like when this setting is wrong.
- **Dockerfile Path**: set explicitly per service —
  - `services/floorwatch-rules-engine/Dockerfile`
  - `services/floorwatch-intelligence/Dockerfile`
  - `services/floorwatch-pipeline/Dockerfile`
- **Config File Path** (a separate setting from Dockerfile Path — see §1b): point each service at its own `railway.json` (checked into this repo) so the Dockerfile builder is pinned in code, not just a dashboard toggle that can be missed or silently reset:
  - `services/floorwatch-rules-engine/railway.json`
  - `services/floorwatch-intelligence/railway.json`
  - `services/floorwatch-pipeline/railway.json`

### 1a. How to recognize "Railway used the wrong builder" from the logs

If deploy fails with `ModuleNotFoundError: No module named 'floorwatch_auth'` (or `floorwatch_secrets_guard`) **and** the traceback paths look like `/app/app/main.py` / `/app/app/config.py` (instead of `/app/services/floorwatch-rules-engine/app/main.py`) **and** you see `/app/.venv/...` or `/mise/installs/python/...` anywhere in the traceback — that's not a code bug, it's Railpack having built the image instead of the Dockerfile. A `python:3.13-slim`-based Docker build (what these Dockerfiles actually produce) never has a `.venv` or `mise` — those only appear from Railway's own auto-builder. The build log may still *show* Dockerfile-looking steps (`COPY`, `apt-get`) if a Dockerfile build ran at some point and got cached — that doesn't mean it's the image that's actually running. Fix: Settings → Build → confirm **Builder = Dockerfile** (not Railpack/Nixpacks), redeploy, and check the *next* build log's first line is `FROM python:3.13-slim`, not a Railpack banner.

### 1b. Dockerfile Path vs. Config File Path — these are two different settings

- **Dockerfile Path** tells Railway which Dockerfile to build, once Builder is already set to `Dockerfile`.
- **Config File Path** tells Railway where to find a `railway.json`/`railway.toml` that can *set* the Builder (among other things) declaratively. Because Root Directory is blank here, Railway looks for a config file at the **repo root** by default — it will **not** auto-discover `services/floorwatch-rules-engine/railway.json` on its own. You must explicitly set each service's Config File Path to its own `railway.json` in that service's Settings for the pinned-builder file to actually take effect.

Do **not** use Railway's default "detect and build from Root Directory" path for any of the three services.

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
| `floorwatch-rules-engine` | `users.json` | **Solved if `FLOORWATCH_POSTGRES_DSN` is set** — accounts then live in Postgres instead (see §3a below), same instance `floorwatch-intelligence` already uses. Only applies if you're still on the JSON fallback: every supervisor/viewer/admin account, and you'd have to re-run `create_user.py` after every redeploy. |
| `floorwatch-rules-engine` | `shift_digest.jsonl` | Escalation history (also the cross-service gap above) |
| `floorwatch-rules-engine` | `roster.json`, `zones_meta.json`, `contacts.json`, `task_type_thresholds.json` | Whatever's been edited at runtime past what's checked into the repo — if these are only ever edited via the repo, this doesn't apply |
| `floorwatch-intelligence` | `vectors.sqlite3` | The embedded/searchable shift-digest and incident-note index — same Postgres fix applies here too |
| Both | `.floorwatch_auth_secret` (only if `FLOORWATCH_AUTH_SECRET` env var is left unset) | Every existing login session invalidates on redeploy |

For a real pilot beyond a quick demo: set `FLOORWATCH_POSTGRES_DSN` on `floorwatch-rules-engine` (fixes `users.json`), and set `FLOORWATCH_AUTH_SECRET` explicitly (see §4) so logins survive redeploys regardless. A volume is still the only fix for `shift_digest.jsonl` and the runtime-edited JSON files, since those don't have a Postgres-backed store yet.

### 3a. Accounts and the admin role — no more manual `create_user.py` per account

`floorwatch-rules-engine` now has an admin-managed account system (three roles: `admin` > `supervisor` > `viewer`, hierarchical — an admin token passes any supervisor-only check too) instead of every account needing a CLI run. Once at least one admin account exists, further accounts are created from the dashboard's "Manage Users" screen (admin-only), not the CLI.

**Bootstrapping the first admin account** — nobody can grant themselves admin access from the UI before an admin account exists, so this one account has to be created before the dashboard's Manage Users screen is usable at all. Three ways to do it, pick whichever fits your workflow:

- **Via Railway environment variables** (no CLI/container access needed at all — the recommended default for a Railway deployment): set `FLOORWATCH_ADMIN_USERNAME` and `FLOORWATCH_ADMIN_PASSWORD` on `floorwatch-rules-engine`. On startup, if that username doesn't already exist, it's created automatically with `role="admin"` (see `app/main.py`'s `_seed_admin_from_env()`). **Idempotent by design** — it only ever creates, never overwrites, so once the admin logs in and sets their real password (forced on first login, same as any admin-created account), a later redeploy with these same variables still set does **not** revert it back to the seed value. Safe to leave both set permanently.

- **Via CLI against the running container** (needs `railway run`/shell access):
  ```bash
  railway run --service floorwatch-rules-engine python create_user.py alice --role admin
  ```
  Uses whichever store the service itself is configured for (`build_user_store()` — Postgres if `FLOORWATCH_POSTGRES_DSN` is set, else `users.json`).

- **Directly in Postgres, no container access needed** — generates the SQL locally (computes the password hash on your own machine — the plaintext never leaves it) and you paste the output into Railway's Postgres query console yourself:
  ```bash
  python generate_admin_sql.py alice --role admin
  ```
  This is the better fit if you're managing the database directly rather than shelling into the service, and works even before the service has started (it creates the table too, `CREATE TABLE IF NOT EXISTS`, harmless if the service already has).

If you already have accounts in `users.json` from before Postgres was configured, run `python migrate_users_to_postgres.py` once to copy them over (`--dry-run` first to preview).

Admin-created accounts get a temporary password the admin sets and shares out-of-band (Slack, in person — no email system exists in this codebase) and are forced to set their own password on first login.

---

## 4. Environment variables

### Shared — must be the *exact same value* on both services

| Variable | Notes |
|---|---|
| `FLOORWATCH_AUTH_SECRET` | Generate once: `python -c "import secrets; print(secrets.token_hex(32))"`. Set it explicitly and identically on both Railway services. **Do not** leave this unset and rely on the file-based `get_or_create_secret()` fallback — each container would generate its own independent secret, and a login token issued by one service would fail validation on the other. |

### `floorwatch-rules-engine` — service-specific

| Variable | Required? | Notes |
|---|---|---|
| `FLOORWATCH_REDIS_URL` | Yes | Point at a real Redis instance — Railway's Redis plugin, or external. |
| `FLOORWATCH_POSTGRES_DSN` | Recommended | Empty falls back to the local `users.json` (doesn't survive a redeploy — see §3/§3a). Point at the same Postgres instance `floorwatch-intelligence` uses if you have one — accounts get their own table, no second database needed. |
| `FLOORWATCH_ADMIN_USERNAME` / `FLOORWATCH_ADMIN_PASSWORD` | Optional | Bootstraps the first admin account automatically on startup — see §3a. Safe to leave set permanently; only ever creates, never overwrites. |
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

### `floorwatch-pipeline` — service-specific

Full reference in `services/floorwatch-pipeline/README.md`; summary here:

| Variable | Required? | Notes |
|---|---|---|
| `FLOORWATCH_CAMERAS_JSON` | Yes (unless using a mounted volume) | Raw JSON content of `cameras.json`, as one variable — see `cameras.json.template`. Only `s3`/`azure_blob`/`gcs`/`http_api`/`ezviz` cameras work from this container. |
| `FLOORWATCH_REDIS_URL` | Yes | Same instance `floorwatch-rules-engine` uses. |
| `FLOORWATCH_EZVIZ_USERNAME` / `FLOORWATCH_EZVIZ_PASSWORD` | If any `ezviz` camera | Real account password — see `sources/ezviz.py`'s module docstring. |
| `FLOORWATCH_AWS_ACCESS_KEY_ID` / `FLOORWATCH_AWS_SECRET_ACCESS_KEY` | If any `s3` camera without an IAM role | |
| `FLOORWATCH_AZURE_STORAGE_CONNECTION_STRING` | If any `azure_blob` camera | |
| `FLOORWATCH_GCS_CREDENTIALS_PATH` | If any `gcs` camera without ADC | Needs the actual key file present in the container — not provisioned by this Dockerfile as written; use ADC instead unless a volume is added. |

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

- [ ] All three services: Root Directory blank, **Builder explicitly set to `Dockerfile`** (not left on Railpack/Nixpacks default), Dockerfile Path set to the correct per-service path
- [ ] All three services: Config File Path set to their own `railway.json` (§1b) so the Dockerfile builder is pinned in code
- [ ] After first deploy, confirmed each build log's first line is `FROM python:3.13-slim`, not a Railpack/Nixpacks banner (§1a)
- [ ] `FLOORWATCH_AUTH_SECRET` generated once, set identically on `floorwatch-rules-engine` and `floorwatch-intelligence` (not needed on `floorwatch-pipeline` — it doesn't issue or validate login tokens)
- [ ] `floorwatch-rules-engine`: `FLOORWATCH_REDIS_URL` set to a real Redis instance
- [ ] `floorwatch-intelligence`: `ANTHROPIC_API_KEY` (or equivalent) set
- [ ] `floorwatch-intelligence`: `FLOORWATCH_RULES_ENGINE_URL` set to rules-engine's real Railway public URL
- [ ] `FLOORWATCH_CORS_ALLOWED_ORIGINS` set to your real dashboard origin(s) on rules-engine and intelligence
- [ ] `FLOORWATCH_DOCS_ENABLED` left `false` on rules-engine and intelligence (or explicitly reviewed)
- [ ] First **admin** account bootstrapped — either `FLOORWATCH_ADMIN_USERNAME`/`FLOORWATCH_ADMIN_PASSWORD` set on the service, or `create_user.py --role admin` / `generate_admin_sql.py` run manually (§3a) — further accounts created from the dashboard's Manage Users screen afterward
- [ ] If switching an existing deployment's accounts from `users.json` to Postgres, ran `migrate_users_to_postgres.py` once
- [ ] Aware of and have made a decision on the `shift_digest.jsonl` cross-service gap (§2) — even if the decision is "acceptable for now, revisit before a real pilot"
- [ ] Aware of the sqlite/local-file persistence limitations (§3) if not attaching a Railway volume
- [ ] `floorwatch-pipeline`: `FLOORWATCH_CAMERAS_JSON` set (or a volume with `cameras.json` mounted), pointed at `floorwatch-rules-engine`'s same `FLOORWATCH_REDIS_URL`
- [ ] `floorwatch-pipeline`: confirmed every camera in `cameras.json` actually uses a cloud-reachable `source_type` (`s3`/`azure_blob`/`gcs`/`http_api`/`ezviz`) — `rtsp`/`local_folder` cameras need a separate on-prem process, not this service
- [ ] `floorwatch-pipeline` not build-tested against a real Docker daemon in this codebase — verify with a real deploy before relying on it (see its own README's "Known limitations")
