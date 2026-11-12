# Data map (record of processing)

| Data | Subject | Purpose | Where | Retention | Recipients |
|---|---|---|---|---|---|
| Name, employee number, department, role, work phone | Employees | Identify staff, send tasks | Postgres `employees` | Life of employment; anonymised on erasure; deactivated records purged per config | Rules-engine; Twilio (phone, for SMS) |
| Password hash (scrypt) | Employees, dashboard users | Authentication | Postgres | Until account removed | None |
| MFA secret (encrypted), recovery-code hashes | Users who enrol | Two-step sign-in | MFA store (Fernet-encrypted) | Until disabled; deleted on erasure | None |
| Push token | Employees using the app | Deliver notifications | Postgres | Until logout/replaced | Google FCM |
| Tasks and task events | Employees | Operate and audit task workflow | Postgres | Config period (`RETENTION_*`) | Dashboard users |
| Zone activity counts/timings from cameras | Anonymous (no face/body, no video stored) | Detect uncovered areas | Postgres / pipeline | Config period | Rules-engine |
| Presence readings ("a person is in zone X") | Anonymous, whoever is on camera | Feed security alerts | Redis stream, in memory only; **not stored** | Not retained | Rules-engine |
| Security alerts and what was done about them (who acknowledged/closed, outcome, note), zone security-setting changes, disarm/re-arm | Anonymous detection; supervisors who act on it | Break-in / after-hours / off-limits / hiding alerts; accountability | Event history (Postgres); open alerts also in a Redis snapshot | Event-history period (default 30 days) | Supervisors/admins; push/SMS text to alert recipients |
| Sign-in and security events, IP addresses | Users | Account protection | Logs / security monitor | Config period | PostHog (IP truncated, errors only) |
| Privacy requests | Employees | Honour data-subject rights | `privacy_requests.json` / store | Per config after closure | Admins |
| Chat questions/notes (when enabled) | Staff | AI assistant | Postgres (pgvector) | Config period | Anthropic, Voyage |

Cross-border transfers: Railway, Google, PostHog, Twilio and AI vendors may process outside the employee's country;
see `VENDOR_REGISTER.md` and sign the DPAs/SCCs.
