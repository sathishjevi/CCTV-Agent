# Floorwatch — Incident Response & Credential Rotation Runbook

DP-L2 (`DATA_PROTECTION_SECURITY_ANALYSIS.md`): *"If `FLOORWATCH_AUTH_SECRET`, the Postgres DSN, or an EZVIZ password were ever suspected compromised, there's no written runbook for what to rotate, in what order, or how to invalidate existing sessions."* This is that runbook. It assumes a Railway deployment (matching `RAILWAY_DEPLOYMENT.md`) but the same steps apply locally with `config/secrets.env` in place of Railway's Variables tab.

This is a **procedure document**, not code — nothing here was auto-generated from the system; every step was checked against the actual config variable names and endpoints as they exist in this repo today.

---

## 1. General incident checklist (any credential)

Work through these in order — don't skip to rotation before containment, and don't skip verification after.

1. **Contain.** If the exposure is ongoing (leaked in a public repo, posted somewhere public, on a lost/stolen device), treat the credential as live and hostile immediately — don't wait to "confirm" abuse first.
2. **Rotate.** Generate a new value at the source (Railway Variables tab, the vendor's dashboard, etc.) — see the per-credential sections below for exactly where.
3. **Redeploy.** Railway services pick up changed Variables on their next deploy/restart — confirm the affected service actually restarted, not just that the Variable was saved.
4. **Verify the old value is dead.** Confirm the specific mechanism (see each section — "how to confirm the old value stopped working").
5. **Check for damage.** Review whatever logs are available for the affected surface (Railway's log stream is the only log store this system has — see `DATA_PROTECTION_SECURITY_ANALYSIS.md`'s note on no monitoring/alerting) for activity in the suspected exposure window.
6. **Document.** Record: what was exposed, when, how it was discovered, what was rotated, when, and what (if anything) was found in step 5. There's no incident-tracking system wired up for this — a dated file/note is enough, but write it down somewhere durable.

---

## 2. Credential inventory, blast radius, and rotation steps

### 2.1 `FLOORWATCH_AUTH_SECRET` — session-signing secret

**What it is:** Signs every JWT session token across *both* `floorwatch-rules-engine` and `floorwatch-intelligence` (shared secret — see `skills/lib/floorwatch_auth.py`). Whoever has it can forge a valid login as **any user, any role, including admin** — no password needed.

**Blast radius if compromised:** Total. Highest-severity credential in the system.

**Where it lives:** Set directly as a Railway env var on **both** services (must be identical on both — they don't share one automatically in production, only in local dev via the shared secret file). If unset, each service auto-generates one to `services/.floorwatch_auth_secret` on first run — **check whether that's actually the case for your deployment before assuming it's explicitly set**.

**Rotation steps:**
1. Generate a new random secret (32+ bytes, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
2. Set `FLOORWATCH_AUTH_SECRET` to the new value on **both** `floorwatch-rules-engine` and `floorwatch-intelligence`'s Railway Variables tabs. If they were relying on the auto-generated file instead, this is also the moment to switch to an explicit value — see `config/README.md`'s "Why AUTH_SECRET is handled separately."
3. Redeploy both services.

**This is still the way to force-logout EVERYONE at once.** Rotating `AUTH_SECRET` invalidates every currently-issued token for every user, because none of them verify against the new secret anymore. For a single compromised account, prefer deactivation or a forced password reset instead (§2.6) — both now also revoke that specific user's existing token immediately (see `skills/lib/floorwatch_auth.py`'s `RevocationStore`), so a full secret rotation is only needed when you genuinely need to kick everyone, not just one account.

Two caveats worth knowing before relying on per-account revocation instead of a full rotation: it's Redis-backed, so it only works where the checking service is actually pointed at the same Redis instance that `floorwatch-rules-engine` uses (`floorwatch-intelligence` needs `FLOORWATCH_REDIS_URL` set for this — check its startup log for a warning if you're not sure it's configured). And it's per-USERNAME, not per-session — there's no way to kill one specific browser tab/device while leaving that same user's other active sessions alone.

**How to confirm the old value stopped working:** Any token issued before rotation should now get `401 Invalid or expired token` from either service. If you have a token from before the rotation handy (e.g. still in a browser's `localStorage`), a REST call with it is the fastest check.

### 2.2 `FLOORWATCH_POSTGRES_DSN` — database connection string

**What it is:** Connection string (includes a password) to the Postgres instance backing `floorwatch_users` (accounts) and, if `floorwatch-intelligence` also points at it, the vector store.

**Blast radius if compromised:** Full read/write on account records (password hashes — PBKDF2, not plaintext, but still crackable offline given enough time) and any indexed shift-digest/incident-note content, depending on what's stored there.

**Where it lives:** Railway Variables tab on **both** services if they share one instance (they don't inherit from each other — must be set independently on each, per `RAILWAY_DEPLOYMENT.md`).

**Rotation steps:**
1. In Railway's Postgres service, rotate the database password (Railway's Postgres plugin regenerates credentials from its own dashboard — check the current DSN Railway shows you after rotating, since the host/port/db name stay the same but the password changes).
2. Update `FLOORWATCH_POSTGRES_DSN` on every service that references it.
3. Redeploy each affected service.
4. If you suspect actual data exfiltration (not just credential exposure), that's a data breach, not just a rotation — see §3.

**How to confirm the old value stopped working:** Attempt a connection with the old DSN (e.g. `psql "<old dsn>"`) — should be refused once Railway's rotation completes.

### 2.3 `FLOORWATCH_EZVIZ_USERNAME` / `FLOORWATCH_EZVIZ_PASSWORD`

**What it is:** The client's **real EZVIZ account password** (not a scoped API key — see `skills/detection/floorwatch-ingest/scripts/sources/ezviz.py`'s own docstring, and DP-L1). Authenticates against an unofficial/reverse-engineered API.

**Blast radius if compromised:** Full access to the client's actual EZVIZ account — not just this system's camera feed, but everything else that account can see/do in EZVIZ's own app/service. This is the client's credential, not this system's — treat a suspected compromise here as urgent as it would be for the client to lose their own EZVIZ login, because that's exactly what happened.

**Rotation steps:**
1. **Notify the client immediately** — this is their account, not infrastructure this project owns. Don't rotate it unilaterally without them knowing, since it's their password on their account.
2. Have the client change their EZVIZ account password directly in the EZVIZ app.
3. Update `FLOORWATCH_EZVIZ_USERNAME`/`FLOORWATCH_EZVIZ_PASSWORD` on `floorwatch-pipeline`'s Railway Variables (or wherever `floorwatch-ingest` actually runs — see `CCTV_INTEGRATION_SETUP.md`).
4. Redeploy the ingest process.

**How to confirm the old value stopped working:** The ingest process's logs should show an auth failure against the EZVIZ API immediately after rotation if the old value is still configured anywhere; confirm no such failures appear after redeploying with the new value.

### 2.4 Cloud storage credentials (S3 / Azure Blob / GCS)

**What it is:** `FLOORWATCH_AWS_ACCESS_KEY_ID`/`FLOORWATCH_AWS_SECRET_ACCESS_KEY`, `FLOORWATCH_AZURE_STORAGE_CONNECTION_STRING`, `FLOORWATCH_GCS_CREDENTIALS_PATH` (a service-account JSON key file — the file itself is the secret).

**Blast radius if compromised:** Whatever the credential's IAM policy/connection string scope allows — ideally read-only access to one client's camera-footage bucket/container, but verify the actual scope on whichever cloud account issued it; this system doesn't control that policy.

**Rotation steps:**
1. AWS: rotate/deactivate the IAM access key in the AWS Console (IAM → Users → Security credentials), issue a new one scoped the same way.
2. Azure: regenerate the storage account's access key (Azure Portal → Storage Account → Access keys) — note this invalidates the connection string, so update it everywhere it's used, not just this system, if the storage account is shared with anything else.
3. GCS: revoke/delete the old service-account key in GCP Console, issue a new JSON key, replace the file at whatever path `FLOORWATCH_GCS_CREDENTIALS_PATH` points to.
4. Update the relevant Railway Variable(s) on `floorwatch-pipeline`, redeploy.

**How to confirm the old value stopped working:** Cloud-provider-side — check the IAM/access-key console for the old key's status (AWS/Azure both show key state directly); a subsequent API call with the old value should fail with an auth error.

### 2.5 Notification channel credentials (Twilio, FCM)

**What it is:** `FLOORWATCH_TWILIO_ACCOUNT_SID`/`FLOORWATCH_TWILIO_AUTH_TOKEN`/`FLOORWATCH_TWILIO_FROM_NUMBER`, `FLOORWATCH_FCM_CREDENTIALS_PATH` (a Firebase service-account JSON file).

**Blast radius if compromised:** Twilio — ability to send SMS (and be billed) as this account, potentially to arbitrary numbers, not just this system's contacts list. FCM — ability to push notifications to this system's registered devices, and depending on the service account's scope, potentially other Firebase project resources.

**Rotation steps:**
1. Twilio: regenerate the Auth Token from the Twilio Console (Account → API keys & tokens) — the Account SID itself doesn't rotate, only the token.
2. FCM: revoke the old service-account key in the Firebase/GCP Console, generate a new one, replace the file at `FLOORWATCH_FCM_CREDENTIALS_PATH`.
3. Update Railway Variables on `floorwatch-rules-engine` (the only service that sends notifications), redeploy.

**Note:** as of this document, Twilio/FCM integration code exists but has never been exercised against the live services with real credentials (`DATA_PROTECTION_SECURITY_ANALYSIS.md`'s companion production-readiness list) — if you're reading this because of a suspected compromise, that's also the first time either integration will have been verified end-to-end; budget time for that, not just the credential swap.

### 2.6 A single supervisor/admin account (not a system-wide secret)

**What it is:** One user's login was compromised (phished password, shared laptop, etc.) — not a system-wide secret.

**Response, fastest first:**
1. **Deactivate the account immediately**: an admin calls `POST /api/admin/users/{username}/deactivate`, or via the Manage Users UI. This blocks all *future* logins AND immediately revokes any token the attacker may already be holding (`RevocationStore` — checked on `floorwatch-rules-engine` unconditionally, and on `floorwatch-intelligence` too if it has `FLOORWATCH_REDIS_URL` set to the same Redis instance).
2. This is now the fast, targeted option — you do NOT need to rotate `FLOORWATCH_AUTH_SECRET` (§2.1) just to kick one compromised account; that's still there for when you need to log out *everyone*, not as a workaround for single-account revocation.
3. Once contained, reactivate with a forced password reset: `POST /api/admin/users/{username}/reset-password` sets a new temporary password, forces a change on next login (`must_change_password`), and also revokes whatever token existed at reset time — belt and suspenders with step 1 if you did both, but either alone is now sufficient on its own.
4. If the account is an admin and you suspect it was used to create other accounts or change other users' roles, review `list_users()` output for anything unfamiliar (`created_by` records who created each account) before reactivating.
5. Residual caveat: revocation is per-username. If `floorwatch-intelligence` doesn't have `FLOORWATCH_REDIS_URL` configured, a token revoked on `floorwatch-rules-engine` can still be used against `floorwatch-intelligence`'s endpoints (chat, incident notes) until it naturally expires — check that service's startup log for the "cannot check token revocation" warning if you're not certain it's wired up.

### 2.7 LLM API keys (`ANTHROPIC_API_KEY` / `FLOORWATCH_LLM_API_KEY`, `VOYAGE_API_KEY`)

**What it is:** Credentials for the supervisor-chat LLM provider and (if configured) Voyage embeddings.

**Blast radius if compromised:** API spend under this account/key, and — depending on the provider's data-retention policy for API calls — potential exposure of whatever's been sent through it (shift-digest/incident-note text passed as chat context). Not direct system access.

**Rotation steps:**
1. Revoke the old key in the provider's dashboard (Anthropic Console / OpenAI / Google AI Studio / Voyage), issue a new one.
2. Update the Railway Variable on `floorwatch-intelligence`, redeploy.
3. Check the provider's usage dashboard for anomalous spend in the suspected exposure window.

### 2.8 `FLOORWATCH_REDIS_URL`

**What it is:** Connection string for the Redis instance used as the event bus between the pipeline, rules engine, and effort tracking. Despite living in `deployment.env.template` (not `secrets.env.template`, since a bare `redis://localhost:6379/0` for local dev has nothing to protect), a **real Railway-managed Redis URL embeds a password** (`redis://default:<password>@host:port`) — treat the deployed value with the same care as anything in `secrets.env`, regardless of which template it's documented alongside.

**Blast radius if compromised:** Read/write access to the live event stream — an attacker could inject fake motion/zone events or read live coverage data in transit. Streams are transient (not a long-term data store), so this is closer to real-time tampering risk than a data-exposure risk.

**Rotation steps:**
1. Regenerate the Redis instance's password from Railway's dashboard for that service.
2. Update `FLOORWATCH_REDIS_URL` on every service that connects to it (`floorwatch-pipeline`, `floorwatch-rules-engine`), redeploy both.

---

## 3. If you suspect actual data exposure (not just a leaked credential)

Everything above is about rotating a credential *before* or *in response to* suspected exposure. If you have reason to believe data was actually accessed or exfiltrated (not just "the credential could have allowed it"):

1. This is a genuine data-protection incident, not just a rotation task. Given this system's data classification (`DATA_PROTECTION_SECURITY_ANALYSIS.md` §1 — employee zone-presence data is personal data), assess whether it triggers a legal notification obligation (breach notification laws vary by jurisdiction and depend on what was actually exposed) — that determination is outside this document's scope and needs whoever handles legal/compliance for this deployment, not just an engineering rotation.
2. Preserve whatever logs exist *before* they roll off Railway's retention window — there's no long-term log store today (`DATA_PROTECTION_SECURITY_ANALYSIS.md`'s "no monitoring/alerting" gap), so act quickly if you need evidence of what happened.
3. Complete the relevant rotation(s) above regardless — containment first, investigation continues in parallel.

---

## 4. What this runbook does not solve

- **No automated alerting** tells you a credential was likely compromised — this is entirely reactive, triggered by a human noticing (a leaked repo, an unexpected bill, a user report). See `DATA_PROTECTION_SECURITY_ANALYSIS.md`'s "no monitoring/alerting/error-tracking" gap.
- **Revocation is per-username, not per-session** — killing one specific browser tab/device while leaving that same user's other active sessions alone still isn't possible; the two real triggers (deactivation, forced password reset) both mean "kill everything this user currently holds." True per-session revocation would need real server-side sessions instead of self-contained JWTs.
- **`floorwatch-intelligence`'s revocation check is opt-in** — it only takes effect if `FLOORWATCH_REDIS_URL` is set there, pointed at the same Redis instance `floorwatch-rules-engine` uses. Confirm this is actually configured for your deployment (check that service's startup log) before assuming §2.6's revocation reaches both services.
- **This document itself needs to stay current** — if a new credential type is added (a new notification channel, a new cloud provider), add a section here in the same pass, not as an afterthought.
