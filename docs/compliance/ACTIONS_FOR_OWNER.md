# Actions for the owner

Things the code cannot do for you. Roughly in priority order.

## Before / at deploy (Railway variables)

| Variable | Service(s) | Why |
|---|---|---|
| `FLOORWATCH_DATA_ENCRYPTION_KEY` | rules-engine | Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). Required to enrol MFA. Keep a copy somewhere safe: losing it locks enrolled users out. Rotation: put the new key first, old keys after, comma separated |
| `FLOORWATCH_ORG_NAME`, `FLOORWATCH_PRIVACY_CONTACT` (and optional `FLOORWATCH_PRIVACY_OFFICER`, `_REGULATOR`) | rules-engine | Fill the generated privacy notice |
| `FLOORWATCH_POSTHOG_API_KEY` | intelligence too | Error tracking for that service |
| `FLOORWATCH_TRUSTED_PROXY_HOPS` | all | Number of proxies in front (Railway: 1) so rate limits see the real client IP |
| `FLOORWATCH_DB_REQUIRE_TLS=1` | all using Postgres | Refuse non-TLS DB connections, once confirmed the Railway URL supports it |
| `FLOORWATCH_MFA_REQUIRED_ROLES=admin,secondary_admin` | rules-engine | **Only after** those users have enrolled, or they will be forced into enrolment at next login |
| Repo variable `DAST_TARGET_URL` | GitHub | Enables the ZAP baseline scan |

## Behaviour changes to know about

* `/healthz` is now minimal (`{"status":"ok"}`). Detail moved to `/api/admin/diagnostics` (admin auth). Update any monitor that read the old fields.
* Retention now **deletes automatically** (leader-only loop). Check the periods in config before the first run; the first run may remove old rows.
* Request bodies are strict: unknown fields get 422. Old clients sending extra fields will fail.
* Employee login has a per-IP limit of 30; offices behind one NAT should be fine, but watch for 429s.
* Passwords upgrade to scrypt on each user's next successful login; no action needed.

## Security alerts (break-in, after-hours, off-limits, hiding)

Full guide: `docs/floorwatch/SECURITY_ALERTS.md`. Nothing alerts until you opt a zone in, so deploying changes nothing by itself.

1. **Deploy order:** the rules-engine first, then the pipeline (the pipeline image carries the camera skill that now emits the
   presence readings; an older rules-engine would reject them as unknown events and log noise).
2. **Set on the rules-engine:** `FLOORWATCH_OPEN_HOURS` (e.g. `mon-fri 09:00-23:00; sat 10:00-02:00; sun closed`) and
   `FLOORWATCH_TIMEZONE` (e.g. `Asia/Kolkata`). Without them only *Off-limits* zones can alert. Optional tuning variables are in the guide.
3. **In the dashboard:** Manage Zones -> set each door/window zone's *Entry point* and each stockroom/office's *Security rule*.
   The zone must also be drawn in the camera calibration tool with the same `zone_id` (`role_tag: security`).
4. **Check System status** (admin): it should show opening hours set, zones marked, and who is messaged. "Nobody" means alerts
   would show on screen but no one would be told.
5. **Run in shadow mode first** (`FLOORWATCH_SHADOW_MODE=true`, the default): alerts appear on screen, nobody is messaged. Do the walk
   test in the guide, at night, in real lighting. Only then set shadow mode to `false`.
6. **Everyone will be asked to re-read the privacy notice** at their next login: the notice now describes security alerts, which changes
   its version. That is intended.
7. **Decide who may disarm.** Any supervisor, secondary admin or admin can (bounded to 12 h, recorded, the team is told). Tighten
   `FLOORWATCH_SECURITY_ALERT_ROLES` / role assignments if that is too broad for you.
8. **It is not a burglar alarm** (no siren, no dispatch, no 24/7 monitoring, and night-time detection accuracy must be proved in your
   building). Keep any existing alarm. Check local rules on workplace monitoring.

## Accounts and keys

1. Restrict the Firebase API key to the app's package name and SHA-1/SHA-256 (Google Cloud console, Credentials).
2. Run the least-privilege SQL (`least_privilege_postgres.sql`) and switch the services to the restricted role.
3. Review that no `token.txt` / data JSON remains on the Railway volume; clean old data you no longer want.

## Backups and recovery

1. Enable Postgres backups on Railway and **do a restore test** into a scratch database; record the date.
2. Keep the encryption key and JWT secret in a password manager, separate from the database backups.

## People and paperwork

1. Sign DPAs with each vendor in `VENDOR_REGISTER.md` (Railway, PostHog, Firebase/Google, Twilio, Anthropic/Voyage when enabled).
2. Have the privacy notice and `DPIA.md` reviewed by someone qualified in your jurisdiction.
3. Start a breach register (template in `BREACH_NOTIFICATION_PLAYBOOK.md`).
4. Commission an external penetration test and get a second reviewer on security-relevant PRs.
5. Enrol admins in two-step verification, store recovery codes offline.

## Still deferred (by your instruction)

iOS build, Play Console rollout, AI chat agent keys (Anthropic/Voyage, pgvector).

## App build note

For debug builds behind SSL inspection, pass the proxy CA as base64:
`flutter run --dart-define=FLOORWATCH_DEV_CA_B64=<base64 of PEM>`. Release builds ignore it.
