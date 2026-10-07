# Security & Compliance Checklist — Audit Report

Checklist: *Vibe Code Security & Compliance Checklist*. Scope: `floorwatch-rules-engine`,
`floorwatch-intelligence`, `floorwatch-pipeline`, `dashboard/floorwatch_demo.html`,
`mobile/floorwatch_app`, CI, and Railway deployment config.

Status key: **Done** (implemented and tested in this repo) · **Owner** (needs an action outside the code; see
`ACTIONS_FOR_OWNER.md`) · **N/A** (does not apply; rationale given).

## 1. Secrets & credentials

| Item | Status | Evidence |
|---|---|---|
| No secrets in source or git history | Done | Custom history scan clean after removing `token.txt`, `tasks.json`, `zone_directory.json`, Norton CA from the index; `.gitleaks.toml` + gitleaks in `security.yml` keeps it clean |
| Secrets only from environment | Done | All keys read via `config.py` env vars; Firebase/PostHog/DB keys from Railway |
| Data files / dev certs not tracked | Done | `services/.gitignore`; dev CA is now a `--dart-define` (`FLOORWATCH_DEV_CA_B64`), asset removed |
| Restrict client API keys | Owner | Firebase API key restriction to app package + SHA-1 |

## 2. Authentication & sessions

| Item | Status | Evidence |
|---|---|---|
| Strong password hashing | Done | scrypt (N=2^15, r=8, p=3), transparent upgrade from PBKDF2 on next login; `test_password_hashing.py` |
| Constant-time unknown-user path | Done | `verify_password_dummy` |
| Brute-force protection | Done | Per-IP, per-identity and global ASGI limits, memory-bounded; `test_request_guard.py` |
| Multi-factor authentication | Done | TOTP (RFC 6238), recovery codes, replay protection, role enforcement via `FLOORWATCH_MFA_REQUIRED_ROLES`; `test_mfa.py` (45 tests); dashboard and app UIs |
| Token revocation / logout | Done | Prior work; socket revocation sweep |
| Trusted proxy IP handling | Done | `FLOORWATCH_TRUSTED_PROXY_HOPS`, rightmost X-Forwarded-For |

## 3. Input validation & API hardening

| Item | Status | Evidence |
|---|---|---|
| Strict request schemas | Done | Pydantic `extra="forbid"`, length/range caps (`schemas.py`, intelligence `StrictModel`) |
| Validation errors don't echo input | Done | `install_error_handlers`; `test_api_hardening.py` |
| Every route has an auth dependency | Done | Route-dependency audit script, all non-public routes covered |
| CORS tight | Done | Explicit origins, methods and headers |
| Health endpoint leaks nothing | Done | `/healthz` minimal; detail at `/api/admin/diagnostics` (admin only) |
| SQL injection | Done | Parameterised queries throughout (`floorwatch_db`); bandit clean (justified `nosec` on identifier-only formatting) |

## 4. Data protection

| Item | Status | Evidence |
|---|---|---|
| Encryption in transit | Done / Owner | Railway TLS at the edge; optional DB TLS enforcement `FLOORWATCH_DB_REQUIRE_TLS` |
| Field-level encryption | Done | Fernet with key rotation (`floorwatch_crypto.py`); MFA secrets encrypted. Needs `FLOORWATCH_DATA_ENCRYPTION_KEY` |
| Least-privilege DB role | Done | `least_privilege_postgres.sql` |
| Backups and restore test | Owner | Railway Postgres backups |
| Minimal vendor-bound data | Done | IPs truncated before PostHog; error-level logs only |

## 5. Dependencies & supply chain

| Item | Status | Evidence |
|---|---|---|
| Hash-pinned locks | Done | `requirements.txt` per service via `uv pip compile --generate-hashes`; Dockerfiles use `--require-hashes` |
| Vulnerability scanning | Done | pip-audit (0 known vulns at time of audit), CI job in `security.yml` |
| SAST | Done | bandit (clean) + CodeQL |
| Secret scanning in CI | Done | gitleaks |
| SBOM | Done | CycloneDX in `docs/compliance/sbom/` |
| Dependency confusion / abandoned packages | Done | PyPI checks, see `DEPENDENCIES.md` |
| Automated updates | Done | `.github/dependabot.yml` |
| DAST | Done / Owner | ZAP baseline in `security.yml`, runs when repo variable `DAST_TARGET_URL` is set |

## 6. Logging & monitoring

| Item | Status | Evidence |
|---|---|---|
| Security event log + alerting | Done | `SecurityMonitor`; error-level reach PostHog |
| Error tracking (server + app) | Done | Prior work, PostHog |
| No sensitive data in logs | Done | Passwords/tokens never logged; request bodies not echoed |

## 7. Privacy engineering (GDPR / CCPA-style principles)

| Item | Status | Evidence |
|---|---|---|
| Privacy notice | Done | Generated from live config, versioned, shown in app and available via API; acknowledgment recorded |
| Data-subject requests | Done | Register with statutory deadlines; access, export, erasure (anonymise, keep employee number), correction, restriction, objection |
| Retention | Done | Automatic leader-only retention loop; periods in config |
| Data map / ROPA | Done | `DATA_MAP.md` |
| Vendor register / DPAs | Done / Owner | `VENDOR_REGISTER.md`; DPAs need signing |
| DPIA | Done / Owner | `DPIA.md` draft; legal review |
| Breach playbook | Done | `BREACH_NOTIFICATION_PLAYBOOK.md`, `INCIDENT_RESPONSE_RUNBOOK.md` |

## 8. Not applicable

| Item | Rationale |
|---|---|
| COPPA | Internal staff tool for adult employees; no children's data |
| HIPAA | No protected health information is collected or processed |
| CAN-SPAM / CASL | Operational task messages to staff, no marketing |
| Payment (PCI DSS) | No payments handled |

## 9. Verification summary

* Rules-engine: 718 tests; intelligence: 93 tests; 32 real-Postgres tests (embedded Postgres, including concurrency races).
* Flutter: `flutter analyze` clean, unit/widget tests pass; on-device check on an Android emulator: login,
  privacy-notice gate, Privacy & security screen.
* Web dashboard: MFA sign-in/enrolment, Security, System status and Privacy requests modals exercised in a real browser.

## 10. Residual risk (honest list)

* No third-party penetration test and no second human reviewer yet (checklist items that need people).
* Privacy notice text is generated; it has not had legal review.
* MFA is optional until `FLOORWATCH_MFA_REQUIRED_ROLES` is set.
