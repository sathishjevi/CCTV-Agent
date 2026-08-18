# Floorwatch — Data Protection & Security Analysis

A consolidated pass from a data-protection and data-security perspective, covering what's changed since `SECURITY_REVIEW.md` (auth, admin roles, Postgres-backed accounts, the Manage Users UI, EZVIZ, Railway deployment). Every finding below was verified directly against the current code — file and line cited — not inferred from memory. `SECURITY_REVIEW.md`'s own findings (C1/C2/H1-H3/M1-M4/L1-L2) are referenced, not repeated, where still relevant.

Severity follows the same convention as `SECURITY_REVIEW.md`: **Critical** (exploitable now, high impact) → **High** → **Medium** → **Low/informational**. Findings here are prefixed `DP-` to keep them distinct from that document's IDs.

---

## 1. What data this system actually holds (classification)

Worth stating plainly before the threat list, since it determines how seriously each finding below should be taken:

| Data | Sensitivity | Where |
|---|---|---|
| Employee zone-presence/motion/effort events, tied to camera+timestamp | Personal data (identifiable via shift roster even without facial recognition) | Redis Streams (transient) → shift digest JSONL (zone_escalated/task_flag only) + `floorwatch_event_history` table (full lifecycle: assignment, nudges, flags, resolutions — see `event_history.py`), Postgres if configured else local JSONL → optionally Postgres vector store |
| Supervisor/admin/viewer account credentials (hashed) | Authentication secret | `users.json` or Postgres `floorwatch_users` |
| Supervisor incident notes, chat Q&A | Free-text, potentially contains names/details written by a human | `incident_notes.jsonl`, vector store |
| CCTV source credentials — S3/Azure/GCS keys, and **a client's real EZVIZ account password** | Infrastructure/vendor credentials | `config/secrets.env` locally, Railway env vars in deployment |
| Auth signing secret (`FLOORWATCH_AUTH_SECRET`) | System-wide — compromise forges any login | File or env var |

**One genuinely good practice already in place**: per Global Constraint 3, no raw CCTV video is ever newly stored — only structured events. No facial recognition is performed (confirmed: zone presence and motion only, not identity). This meaningfully reduces this system's data-protection footprint compared to a typical CCTV analytics product, and is worth keeping true going forward — it's a design constraint, not an accident.

---

## 2. New findings (this session's work — none of this was in `SECURITY_REVIEW.md`)

### DP-H1. ✅ FIXED — Stored XSS — task names render unescaped, reachable by ANY supervisor, not just admin

`dashboard/floorwatch_demo.html`'s `renderTasks()`/`renderQueue()`/`logEvent()` build DOM via `innerHTML` with template-literal-interpolated values (`t.name`/task_name, `t.zone`/zone_name, `cmd.message`) and **no HTML-escaping**. `task_name` is free text any authenticated supervisor can set via the "Assign Task" form (`TaskAssignRequest`) — meaning **any supervisor**, not just an admin, can plant `<img src=x onerror=...>` as a task name, and it executes in the browser of every supervisor/admin who views the live dashboard afterward. This is the highest-severity finding here because the attacker doesn't need admin privilege, and the payload runs in the context of whoever's viewing — including admins, whose session token (`localStorage`) becomes readable to injected script.

**Fix applied**: added an `escapeHtml()` helper and applied it at every DOM-insertion site that interpolates task/zone/message/error text — `renderTasks()`, `renderQueue()`, every `logEvent()` call site, and hydration error paths. State stays raw; escaping happens once, at render time, to avoid double-escaping. Verified against a real `<img src=x onerror=alert(1)>` payload — no longer produces a live tag.

### DP-H2. ✅ FIXED — Same XSS pattern in the new Manage Users table — username unescaped, plus an attribute-breakout variant

`renderUsersTable()` (`dashboard/floorwatch_demo.html`, added this session) interpolates `u.username`, `u.role`, `u.last_login_at` directly into `innerHTML` with no escaping (line 829). Narrower blast radius than DP-H1 (only reachable by someone with account-creation ability — currently admin-only, or via the env-var seed), but same root cause.

There's a second, distinct variant here worth calling out: `u.username` is also placed unescaped **inside an HTML attribute** — `data-username="${u.username}"` (lines 835, 837, 838). A username containing a `"` character breaks out of that attribute entirely (not just injecting as visible text), letting an attacker add arbitrary new attributes/event handlers to the `<button>` tag itself — e.g. a username like `x" onmouseover="alert(1)` would work even if the text-content escaping fix (DP-H1) is applied but attribute values are overlooked. The fix needs to cover both contexts: escape text content AND separately ensure attribute values can't contain an unescaped `"`.

**Fix applied**: `renderUsersTable()` now computes `escapeHtml(u.username)` once and reuses it in both the text content and every `data-username="..."` attribute. Verified against the exact `x" onmouseover="alert(document.cookie)"` breakout payload — no longer produces a raw `" onmouseover="` in rendered HTML. Additionally closed at the source: **DP-M5** now rejects non-alphanumeric usernames at account-creation time, so this specific payload shape can no longer even be stored.

### DP-H3. ✅ FIXED — No rate limiting or lockout on `/api/login`

Verified: zero throttling anywhere in `floorwatch-rules-engine` (a full-repo grep for `RateLimiter` in that service returns nothing — compare to `floorwatch-intelligence`'s `/api/chat`, which does have one). Combined with an 8-character-minimum-only password policy (DP-M1 below) and no account lockout, this endpoint is open to unlimited-speed online brute-force or credential-stuffing against any known/guessable username — including your seeded admin account, whose username you control and presumably know (or could be guessed if it's a real name pattern). The `/api/admin/users*` endpoints have the same gap — an admin session, once obtained, can be hammered with no throttling either.

**Fix applied**: two independent limiters on `/api/login` — per-IP (`FLOORWATCH_LOGIN_RATE_LIMIT_PER_IP_PER_MINUTE`, default 10/min, `X-Forwarded-For`-aware behind Railway's proxy) and per-username (`FLOORWATCH_LOGIN_RATE_LIMIT_PER_USERNAME_PER_MINUTE`, default 5/min) — catching both a single-source attacker and a distributed/botnet attempt against one account. A third limiter (`FLOORWATCH_ADMIN_RATE_LIMIT_PER_MINUTE`, default 30/min) covers all four `/api/admin/users*` endpoints, bounding a compromised admin token. All three built on the shared `RateLimiter` (moved to `skills/lib/floorwatch_rate_limit.py` rather than duplicated — same module `/api/chat` already used).

### DP-M1. ✅ FIXED — Password policy — length only, no complexity, no breach check

Confirmed: every password-setting path (`change_password`, admin create, admin reset) checks only `len(password) < 8`. No character-class requirements, no check against known-breached password lists, no reuse prevention. For a system where account compromise means access to employee monitoring data, this is thin — especially paired with DP-H3.

**Fix applied**: `validate_password_strength()` in `skills/lib/floorwatch_auth.py` — 10-character minimum, rejection against a curated common/breached-password blocklist, and rejection of a password equal to its own username. Deliberately NOT arbitrary character-class complexity rules (NIST SP 800-63B guidance — see the function's docstring for the reasoning). Wired into all 3 password-setting endpoints plus the env-var admin-seed path.

### DP-M2. ✅ FIXED — No security headers on either service

Confirmed via grep: neither service sets `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, or `Strict-Transport-Security`. Only `CORSMiddleware` is configured. A CSP would materially contain the impact of DP-H1/H2 even before they're fixed in code (script-src restrictions would block a lot of injected-script payloads outright). Missing `X-Frame-Options`/`frame-ancestors` also leaves both dashboards clickjackable in theory.

**Fix applied**: `skills/lib/floorwatch_security_headers.py`, installed on both services — `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, `Strict-Transport-Security`, and a `Content-Security-Policy` (`default-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`, etc.). Honestly scoped, not oversold: the CSP allows `'unsafe-inline'` for script/style since neither dashboard has nonce/hash infrastructure — see the module's docstring for exactly what this does and doesn't protect against.

### DP-M3. Auth token travels as a URL query parameter for the WebSocket connection — open

`connect()` in `floorwatch_demo.html` builds `wss://host/events?token=<jwt>` — necessary because browsers can't set custom headers on the native WebSocket API, but it means the token can end up in server access logs, browser history, and any intermediate proxy/CDN log. Bounded by the token's 12h TTL, but still a real leakage channel worth knowing about. (Regular REST calls correctly use an `Authorization` header instead, via `authFetch()`.) Not fixed — genuine browser API limitation, not scheduled in the current priority order.

### DP-M4. ✅ FIXED — Retention policy doesn't cover accounts at all

`retention.py` in both services was checked directly: rules-engine's only prunes `shift_digest.jsonl`; intelligence's only prunes incident notes + the vector store. **Neither references `floorwatch_users`/`PostgresUserStore`/`UserStore` in any way.** A deactivated account's record — and any personal data in `created_by`/`last_login_at` — persists forever with no expiry path. This compounds with the already-documented lack of server-side token revocation (`floorwatch_auth.py`'s own docstring): deactivating someone blocks their *next* login but not an already-issued token, and nothing ever purges old account records either.

**Fix applied**: accounts now record `deactivated_at` (both `UserStore` and `PostgresUserStore`, cleared on reactivation). `purge_stale_deactivated_accounts()` in `floorwatch_auth.py` purges accounts deactivated more than the retention window ago — never touches an active account regardless of age, never guesses an age for a record missing the timestamp — archiving to JSONL before deletion, same archive-then-delete pattern as the existing JSONL retention. Wired into `floorwatch-rules-engine/retention.py`'s existing CLI/schedule. Server-side token revocation itself remains a separate, open gap (tracked in Set 2, "no server-side token revocation").

### DP-M5. ✅ FIXED — No input validation on new-account usernames

`CreateUserRequest` (`main.py`) has no length limit or character whitelist on `username` — only role validity, password length, and duplicate-check. Low severity on its own, but it's the specific gap that makes DP-H2 possible, and separately it's just inconsistent hygiene (an admin could create a username that's an empty-looking Unicode string, absurdly long, etc.).

**Fix applied**: `validate_username()` in `floorwatch_auth.py` — 3-32 characters, ASCII whitelist (letters, digits, underscore, hyphen, period). Wired into `/api/admin/users`. Directly closes the DP-H2 attribute-breakout payload shape at the source (a `"` can no longer be part of a username at all).

### DP-M6. ✅ FIXED — Dependency versions are unbounded, no vulnerability scanning

Both `requirements.txt` files use `>=` ranges exclusively (`fastapi>=0.110`, `psycopg[binary]>=3.1`, etc.) with no upper bounds or lockfile, and no `.github/dependabot.yml` or `pip-audit`/`safety` step anywhere in CI (confirmed: only two unrelated workflow files exist in `.github/workflows/`). A `pip install` today can silently pull a different — and potentially vulnerable — transitive dependency version next month, and nothing would catch a newly-disclosed CVE in a package this system depends on.

**Fix applied**: `.github/dependabot.yml` — pip scanning for both services (+ their test requirements), the pipeline's direct skill dependencies, Docker base-image scanning for all three deployed services' Dockerfiles, and GitHub Actions version scanning. Deliberately scoped to what Floorwatch actually deploys, not every `requirements.txt` in this monorepo — see the file's header comment for why.

### DP-L1. EZVIZ credential storage — already self-documented, restated here for completeness

`sources/ezviz.py`'s own docstring already states this plainly: it stores "the customer's actual account password, not a scoped API key," against an unofficial/reverse-engineered API, with explicit Terms-of-Service risk acknowledged and accepted. Flagged here only so it appears in one consolidated data-protection list rather than requiring someone to know to look in `floorwatch-ingest`'s source comments — the risk itself isn't new, just re-surfaced for completeness.

### DP-L2. ✅ FIXED — No documented incident response / credential-rotation runbook

If `FLOORWATCH_AUTH_SECRET`, the Postgres DSN, or an EZVIZ password were ever suspected compromised, there's no written runbook for what to rotate, in what order, or how to invalidate existing sessions (compounded by DP-M4's lack of token revocation). Worth having before a real incident forces you to improvise one.

**Fix applied**: `INCIDENT_RESPONSE_RUNBOOK.md` — a general contain/rotate/verify/document checklist, then a per-credential section (AUTH_SECRET, Postgres DSN, EZVIZ, cloud storage, Twilio/FCM, LLM API keys, Redis) covering where each lives, blast radius, exact rotation steps, and how to confirm the old value stopped working. Explicitly documents the AUTH_SECRET-rotation-is-the-only-full-logout-lever tradeoff (no per-token revocation exists) rather than glossing over it.

---

## 3. Consolidated best-practices checklist

| # | Item | Status |
|---|---|---|
| 1 | Passwords hashed (PBKDF2-HMAC-SHA256, salted, 200k iterations) | ✅ Done |
| 2 | Auth required on every endpoint, role-based (admin/supervisor/viewer, hierarchical) | ✅ Done |
| 3 | CORS restricted to real origins, no wildcard | ✅ Done (`SECURITY_REVIEW.md` H1) |
| 4 | SQL injection — parameterized queries throughout | ✅ Confirmed, no gaps found |
| 5 | Secrets excluded from git, pre-commit secret scanner | ✅ Done |
| 6 | PII/secret redaction in logs | ✅ Done (`SECURITY_REVIEW.md` M2) |
| 7 | No new raw video storage, no facial recognition | ✅ Design constraint, holding |
| 8 | Rate limiting on `/api/chat` | ✅ Done |
| 9 | **Rate limiting on `/api/login` and `/api/admin/*`** | ✅ DP-H3 fixed |
| 10 | **HTML-escaping on all dashboard-rendered user/server text** | ✅ DP-H1, DP-H2 fixed |
| 11 | **Security headers (CSP, X-Frame-Options, etc.)** | ✅ DP-M2 fixed |
| 12 | Password complexity/breach checks | ✅ DP-M1 fixed |
| 13 | Retention policy covering accounts/sessions | ✅ DP-M4 fixed (accounts); token revocation also fixed, see Set 2 |
| 14 | Input validation on account creation | ✅ DP-M5 fixed |
| 15 | Pinned/scanned dependencies | ✅ DP-M6 fixed (scanning); still `>=`-range, not pinned/locked |
| 16 | Token travels via header, not URL, everywhere | 🟡 Partial — REST yes, WebSocket no (DP-M3, browser API limitation) |
| 17 | Incident response runbook | ✅ DP-L2 fixed |
| 18 | Employee notice/consent | 🟡 Attestation flag exists (`FLOORWATCH_CONSENT_CONFIRMED`), not a technical control — real sign-off is a legal/process item outside code |

---

## 4. Suggested priority order

1. ✅ **DP-H1 + DP-H2** (XSS) — highest impact, reachable by a supervisor not just an admin, and directly threatens admin session tokens. Fixed.
2. ✅ **DP-H3** (login rate limiting) — straightforward, reuses code that already exists for `/api/chat`. Fixed.
3. ✅ **DP-M2** (security headers) — cheap, and directly reduces the blast radius of #1 even before/alongside that fix. Fixed.
4. ✅ **DP-M1** (password policy) — pairs naturally with #2. Fixed.
5. ✅ **DP-M4 + DP-M5** — smaller, but close the loop on account lifecycle hygiene. Fixed.
6. ✅ **DP-M6** (dependency scanning) — infrastructure, not code; added Dependabot as a config-only change. Fixed.
7. ✅ **DP-L1/DP-L2** — already understood risks; DP-L2 (runbook) is a documentation task, not code. Fixed.

**Set 1 complete.** Remaining open: DP-M3 only (WebSocket token-in-URL — genuine browser API limitation, not currently scheduled; DP-L1 needed no further action, already self-documented).
