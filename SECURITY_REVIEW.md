# Floorwatch Security Review — Access Control & Data Leak Assessment

**Scope:** `services/floorwatch-rules-engine/`, `services/floorwatch-intelligence/`, `skills/detection/floorwatch-coverage/calibration/`, and the dashboard HTML clients. Performed by running both services locally and issuing real, unauthenticated HTTP/WebSocket requests against them — every finding below is demonstrated with actual request/response evidence, not inferred from reading code alone.

**Headline finding (as originally filed):** none of Floorwatch's services had any authentication or authorization at all. Every API was fully open to anyone who could reach it on the network — read AND write. This directly contradicted "internal only, authorized should have access."

---

## Update — per-supervisor authentication implemented (C1, C2 fixed)

Per your decision to build per-supervisor login rather than a shared API key, authentication is now live across both services:

- **`skills/lib/floorwatch_auth.py`** — shared module (never forked per service): PBKDF2-HMAC-SHA256 password hashing, JWT (HS256) session tokens signed with one secret shared by both services (`FLOORWATCH_AUTH_SECRET`, or auto-generated once and persisted to `services/.floorwatch_auth_secret`), roles `supervisor` (full read/write) / `viewer` (read-only) / `service` (used only for `floorwatch-intelligence`'s own server-to-server reads — see below).
- **Every endpoint on both services now requires a valid Bearer token** — verified live: `curl` with no `Authorization` header now gets `401 {"detail":"Missing or malformed Authorization header"}` on `/api/state`, `/api/tasks`, `/api/tools`, `/api/chat`, `/api/incident-notes`, everywhere C1 previously demonstrated open write access.
- **C2 fixed**: `/events` (rules engine's live WebSocket) now requires `?token=<jwt>` in the connection URL — browsers can't set custom headers on the native WebSocket API, so the token travels as a query param, validated before `manager.connect()` is ever called. A connection without a valid token is closed immediately (code 4401).
- **Mutating endpoints require `supervisor` role specifically** (`Depends(require_supervisor)`); a `viewer`-role token gets `403 Forbidden` on them, confirmed live.
- **The author-spoofing sub-finding under C1 is also fixed**: `POST /api/incident-notes` no longer accepts a client-supplied `author` field at all — the note is always attributed to the authenticated caller's own username (`user["sub"]`). Same fix applied to zone-approve/reassign and task-confirm/dismiss, which previously hardcoded `"supervisor"` as the actor regardless of caller — `resolved_by` now reflects the real logged-in username.
- **`floorwatch-intelligence`'s server-to-server calls to the rules engine** (`tools.py`'s two GETs) now authenticate as a **`viewer`-scoped** self-issued service token — deliberately never `supervisor`-capable, so a bug in that read-only tool layer can't mutate rules-engine state even if it tried. This is a second, independent enforcement of Phase 5's Global Constraint 7, on top of the structural code-shape tests already in place.
- **Both dashboards** (`floorwatch_demo.html`, `floorwatch_chat.html`) now show a login screen on load if no valid session is stored, persist the session in `localStorage`, attach the token to every API call and the WebSocket connection, and drop back to the login screen on a 401 (e.g. expired token). Verified live in-browser: login, page reload preserving session, logout clearing it, and a token issued by the rules engine's `/api/login` working unmodified against `floorwatch-intelligence` (shared-secret cross-service validation).
- **`create_user.py`** bootstraps accounts — deliberately no default username/password shipped in the repo (a checked-in credential would be a checked-in secret); an operator runs this once per deployment.

**Test coverage**: 18 new unit tests for `floorwatch_auth.py` (password hashing, token issue/verify/expiry, forged-token rejection including the classic `alg:none` JWT attack, user store), plus updated fixtures across both services' existing suites so every test now authenticates realistically. 182 tests passing across both services.

**New known limitations, flagged rather than silently glossed over:**
- **No server-side token revocation list.** A stolen/leaked token remains valid until it expires (12h default, `FLOORWATCH_TOKEN_TTL_SECONDS`). A real deployment wanting instant revocation (e.g. an offboarded supervisor) needs a blocklist or a move to server-side sessions — not built here.
- **The auto-generated shared secret file** (`services/.floorwatch_auth_secret`, used when `FLOORWATCH_AUTH_SECRET` isn't set) is convenient for local/dev runs but is itself a secret sitting on disk — a real deployment should set `FLOORWATCH_AUTH_SECRET` explicitly via a proper secrets manager and ensure the fallback file path is never reachable/committed (it's `.gitignore`'d here, but that's not the same as real secret management).
- **Token stored in `localStorage`**, readable by any JavaScript executing in that origin. No XSS vector exists in these dashboards today (no un-escaped user content is rendered — chat answers and event text go through `escapeHtml()`/text-content assignment), but this is a design constraint worth re-checking if the UI ever grows a feature that renders untrusted HTML.
- **Passwords have a minimum length check (8 chars) but no complexity/breach-list check.** Fine for a small pilot with a handful of known operators; would want strengthening (or SSO) before a larger rollout.

---

---

## Critical

### C1. [FIXED — see "Update" above] Zero authentication on any service — full read/write access to anyone on the network

**Evidence (live test, no credentials sent):**
```bash
$ curl -s -X POST http://127.0.0.1:8080/api/tasks -H "Content-Type: application/json" \
    -d '{"task_name":"SECURITY-TEST unauthorized task","zone_id":"concession","assigned_minutes":5}'
{"event_id":"93b0e0ad-...","event_type":"task_assigned","task_id":"33bd3cad-...", ...}

$ curl -s http://127.0.0.1:8080/api/tasks
{"33bd3cad-...":{"task_name":"SECURITY-TEST unauthorized task", "status":"open", ...}}
```
A request with no API key, no session, no header of any kind successfully **created a live task in the system**, and it persisted. The same is true of `POST /api/queue/zone/{id}/approve`, `/reassign`, `POST /api/queue/task/{id}/confirm`, `/dismiss`, and `POST /api/tasks/{id}/complete` — every mutating endpoint on `floorwatch-rules-engine` is reachable by anyone.

The `floorwatch-intelligence` service has the same gap on its own writable surface:
```bash
$ curl -s -X POST http://127.0.0.1:8090/api/incident-notes -H "Content-Type: application/json" \
    -d '{"text":"SECURITY-TEST: injected note with zero credentials","zone_id":"concession"}'
{"note_id":"007e5736-...","author":"supervisor", ...}
```
Note the response: `"author":"supervisor"` — the field is hardcoded, not derived from any real identity, so an anonymous, unauthorized write is **falsely attributed** as coming from a legitimate supervisor. Anything indexed this way later gets cited by the RAG chat assistant as if it were a trustworthy supervisor note.

**Read access is equally open** — `GET /api/state`, `/api/tasks`, `/api/digest`, `/api/queue`, `/api/incident-notes` all return full data with no credentials.

**Impact:** Any device that can reach these ports — on the same LAN, or the public internet if ever port-forwarded/deployed without a reverse-proxy auth layer — can view all coverage/effort data, inject false supervisor notes, approve or dismiss real supervisor directives, and fabricate task completions. This is the most severe class of finding: it's not a leak of read access, it's unrestricted write access to a system whose whole design (per the original build brief's Global Constraint 1) depends on a human supervisor being the one making these decisions.

**Remediation:** Require authentication on every endpoint before any real deployment — at minimum a shared API key (`Authorization: Bearer ...`) checked via a FastAPI `Depends()`, ideally per-supervisor credentials with role separation (supervisor vs. read-only viewer vs. service-to-service). Do this **before** `FLOORWATCH_SHADOW_MODE=false` is ever set — right now `go_live_checklist.py` checks accuracy/roster/notification readiness but has no check for "is this API actually protected," which it should.

### C2. [FIXED — see "Update" above] WebSocket `/events` has no authentication

**Evidence:**
```python
async with websockets.connect('ws://127.0.0.1:8080/events') as ws:
    print('Connected to /events with ZERO authentication.')
# -> "Connected to /events with ZERO authentication."
```
Anyone who can open a WebSocket to the service receives the live, real-time stream of every zone/task state transition as it happens — no handshake, no token.

**Remediation:** Same as C1 — gate the WebSocket handshake on a credential (e.g. a token query param or header validated in `events_ws()` before `manager.connect()`).

---

## High

### H1. [FIXED] CORS wildcard allows any website to make authenticated-looking requests from a supervisor's browser

**Evidence:**
```bash
$ curl -s -i -X OPTIONS http://127.0.0.1:8080/api/state -H "Origin: https://evil.example.com" -H "Access-Control-Request-Method: POST"
access-control-allow-origin: *
access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT
```
Both services previously set `allow_origins=["*"]`. **Fixed**: both now default to a small allowlist of local-dev origins via `FLOORWATCH_CORS_ALLOWED_ORIGINS` (no wildcard fallback exists anymore) — set explicitly per real deployment. `allow_credentials=True` was also added so the browser actually sends the `Authorization` header cross-origin to an allowed origin. Verified: `app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ALLOWED_ORIGINS, ...)` in both `main.py` files; `config.CORS_ALLOWED_ORIGINS` printed live shows the allowlist, not `*`.

### H2. [FIXED] Unbounded `/api/chat` — cost and data-exposure surface

Auth (C1) already closed the data-exposure half. **Rate limiting is now also fixed**: `rate_limit.py`'s `RateLimiter` bounds `/api/chat` to `FLOORWATCH_CHAT_RATE_LIMIT_PER_MINUTE` (default 10) requests/minute per authenticated username, returning `429` with a `Retry-After` header once exceeded — verified live via `TestClient` (`test_chat_rate_limit_returns_429_after_limit_exceeded`). Deliberately in-process (not Redis-backed) since this service runs as a single process; flagged in the module docstring as needing to move to a shared store if this service is ever scaled to multiple workers.

### H3. [Already resolved] Default `--host 0.0.0.0` guidance in the rules-engine README

On review, the README already documented `--host 127.0.0.1` — this finding as originally filed no longer reflects the current file. The stale "no authentication" security note in the same README (predating the auth fix) has been updated to match reality, and the config table now documents `FLOORWATCH_CORS_ALLOWED_ORIGINS`/`FLOORWATCH_DOCS_ENABLED`/`FLOORWATCH_RETENTION_DAYS`.

---

## Medium

### M1. [FIXED] No retention/expiry policy — history grows unbounded forever

Directly relevant to your third question. **Previous answer: no limit, retained indefinitely. Now: 90 days by default** (`FLOORWATCH_RETENTION_DAYS`, overridable). Implemented via a shared helper (`skills/lib/floorwatch_retention.py`, never forked per service) plus a `retention.py` CLI in each service:
- `floorwatch-rules-engine/retention.py` prunes `shift_digest.jsonl`.
- `floorwatch-intelligence/retention.py` prunes `incident_notes.jsonl` **and** deletes vector-store rows via a new `delete_before(cutoff_iso)` method (added to both `SqliteVectorStore` and `PgVectorStore`), keyed off the `timestamp` already present in each row's metadata.

Design choices worth noting: pruned entries are **archived** to a dated JSONL file before deletion by default (`--no-archive` to skip), so this is rotation, not silent destruction. Entries with a missing/unparseable timestamp are always kept rather than guessed away. A `--dry-run` flag reports what would be pruned without changing anything. Neither script is wired to a scheduler yet — same as `shift_digest_job.py`, meant for cron/Task Scheduler/Celery Beat, an operator's deployment choice. 17 new tests (11 for the shared helper, 4+2 for the two CLIs), all passing.

### M2. [FIXED] PII (phone numbers) written to stderr logs

`notifications.py` previously logged phone numbers/FCM tokens in full. **Fixed**: `_mask_phone()`/`_mask_token()`/`_mask_context()` now redact every log call in the file — phone numbers show only the last 4 digits, tokens show only a short prefix/suffix. Verified with a test that captures actual log output and asserts the full number never appears in it (`test_notifications_log_never_contains_full_phone_number`).

### M3. [FIXED] No security-relevant metadata on the go-live checklist

`go_live_checklist.py` previously checked accuracy rate, roster, contacts, and notification-channel connectivity, but had no check for authentication being configured — a deployment could pass every existing check and still be C1-open. **Fixed**: two new checks added — `check_auth_accounts_exist` (at least one real account in `users.json`) and `check_auth_enforced_live` (an actual unauthenticated `GET /api/state` against a running instance must return 401, exactly the "does hitting a protected endpoint without credentials return 401" test this finding recommended — not just a code-shape check). `main()` now exits non-zero if either fails. 7 new tests plus an update to the existing full-flow test so it stays honest about what "ALL CHECKS PASSED" means.

### M4. [FIXED] Publicly reachable OpenAPI schema and docs UI

FastAPI's default `/docs`, `/redoc`, and `/openapi.json` were enabled on both services with no auth. **Fixed**: now off by default (`FLOORWATCH_DOCS_ENABLED=false`), gated in `FastAPI(docs_url=... if config.DOCS_ENABLED else None, ...)` on both services — verified live (`main.app.docs_url is None`, etc.) — opt in explicitly for local development only.

---

## Low / informational

### L1. Pre-existing weak default credentials in the base repo (not introduced by Floorwatch work)

`docker/.env` (part of the original SharpAI/DeepCamera repo this project is built on top of, not something added during Phases 1–5) contains hardcoded default credentials (`MYSQL_PASSWORD=shinobi`, `ADMIN_PASSWORD=admin`). Out of scope for the Floorwatch-specific review, but worth flagging since it's in the same repository and would be part of any real deployment's attack surface.

### L2. No input size/rate limits on incident notes or chat

An unauthenticated caller (per C1) could submit unbounded numbers/sizes of incident notes, growing the vector store arbitrarily and diluting/poisoning retrieval quality (a milder, persistent-storage variant of the prompt-injection scenario already covered defensively in Phase 5's `GUARDRAIL_TEST_RESULTS.md` — that document proves injected content can't trigger a *mutation*, but doesn't address it being writable at all, which is this finding, C1's data-integrity angle).

---

## What was and wasn't tested

**Tested live**, with real requests against running instances: unauthenticated reads and writes on both services' full REST surface (original finding), and — after the auth fix — 401/403 rejection on every endpoint without/with-wrong-role credentials, successful login, cross-service token validation, WebSocket token gating, in-browser login/reload/logout flow, and correct author attribution on a real supervisor note. CORS preflight behavior, and log output inspection were also tested live. Test artifacts (injected tasks/notes, test users, and their backing files) were deleted after each round — none of it was real operational data.

**Not tested** (out of reach in this environment, same limitations noted throughout `PHASE_1_NOTES.md`–`PHASE_5_NOTES.md`): behavior behind a real reverse proxy/firewall, real Postgres access-control (RLS, roles), whether a production deployment's network topology would actually expose these ports externally, and token revocation under real operational conditions (no revocation list exists yet — see "New known limitations" above).

## Recommended priority order

1. ~~**C1 + C2** — add authentication to every endpoint and the WebSocket~~ **FIXED.**
2. ~~**H1** — tighten CORS to the real dashboard origin(s)~~ **FIXED.**
3. ~~**H3** — fix the `0.0.0.0` documentation default~~ **Already resolved on review** (README already said `127.0.0.1`; stale security note updated instead).
4. ~~**M1** — decide and implement a retention policy~~ **FIXED** (90-day default, overridable — answers "how many days of history" with an actual number).
5. ~~**H2 (rate limiting), M2, M3, M4**~~ **All FIXED** — rate limiting, log redaction, go-live-checklist auth check, docs lockdown.

**Every finding in this review is now fixed except L1 and L2.** C1, C2, and the author-spoofing sub-finding were fixed as part of the per-supervisor login work. H1, H2, H3, M1, M2, M3, and M4 were fixed in the subsequent hardening pass described above.

**Remaining, deliberately left open (low severity, informational):**
- **L1** — pre-existing weak default credentials in the base DeepCamera/Shinobi repo (`docker/.env`), not introduced by Floorwatch work and out of this review's scope.
- **L2** — no input size/rate limits on incident-note *submission volume* specifically (distinct from H2's now-fixed chat rate limit) — an authenticated-but-malicious account could still submit unbounded numbers of notes. Lower severity now that C1's "anyone, no credentials" access is closed; worth a follow-up if this becomes a real concern at scale.

**Test coverage after this pass**: 303 tests passing across the whole Floorwatch codebase, covering every fix above with both unit tests and, where meaningful, live-request-style verification. (`skills/lib` also has 3 pre-existing failures unrelated to this review — a dead code path, `_load_coreml_with_compute_units`, never actually called by `load_optimized` in either the original or current code; see `REQUIREMENTS_STATUS.md`.)
