# Floorwatch Rules Engine

Standalone Python/FastAPI service implementing:
- **Part B** — the 3-tier coverage escalation state machine (see
  `app/engine.py` docstring). Consumes schema-validated `zone_covered`/
  `zone_gap` events from a Redis Stream (published by
  `skills/detection/floorwatch-coverage`).
- **Part A** — active-time/effort tracking (see `app/effort_engine.py`
  docstring). Consumes the motion signal published by
  `skills/detection/floorwatch-pose` on a second Redis Stream, correlated
  with Part B's live zone-occupancy state.

Both broadcast their resulting events over one shared WebSocket to
`dashboard/floorwatch_demo.html`.

## Run

```bash
pip install -r requirements.txt
export FLOORWATCH_REDIS_URL=redis://localhost:6379/0   # or redis://redis:6379/0 in docker-compose
cd app
uvicorn main:app --host 127.0.0.1 --port 8080
```

**Security note**: every endpoint requires a Bearer token (see
`skills/lib/floorwatch_auth.py` and `SECURITY_REVIEW.md` at the repo
root). Run `python create_user.py` once per deployment to bootstrap an
account — no default credentials ship in this repo. Binding to
`127.0.0.1` (the default above) still matters even with auth in place:
prefer a reverse proxy with TLS over binding `0.0.0.0` directly if this
ever needs to be reachable beyond localhost.

Then open `dashboard/floorwatch_demo.html`, log in, and it connects its
WebSocket to `ws://<host>:8080/events?token=<jwt>` automatically.

## Config (env vars — see `app/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `FLOORWATCH_REDIS_URL` | `redis://localhost:6379/0` | Redis Stream source |
| `FLOORWATCH_SHADOW_MODE` | `true` | Suppresses real notification sends (Global Constraint 4) |
| `FLOORWATCH_NUDGE_TIMER_SECONDS` | 180 | Tier 1 deadline |
| `FLOORWATCH_NUDGE_CAP_PER_SHIFT` | 3 | Nudges before bypassing straight to Tier 2 |
| `FLOORWATCH_COMMAND_TIMER_SECONDS` | 300 | Tier 2 deadline |
| `FLOORWATCH_COMMAND_THROTTLE_PER_HOUR` | 5 | Tier 2 throttle |
| `FLOORWATCH_RESOLVE_COOLDOWN_SECONDS` | 900 | Post-resolution cooldown per zone |
| `FLOORWATCH_ROSTER_PATH` | `roster.json` | Manually maintained staffing roster (hard precondition — Global Constraint 5) |
| `FLOORWATCH_REDIS_MOTION_STREAM` | `floorwatch:motion` | Motion signal source (Part A) |
| `FLOORWATCH_EFFORT_UPDATE_INTERVAL_SECONDS` | 30 | How often `task_active_time_update` is emitted per open task |
| `FLOORWATCH_TASK_TYPE_THRESHOLDS_PATH` | `task_type_thresholds.json` | Per-task-type expected active-ratio (Phase 3 task 6 — never one global threshold) |
| `FLOORWATCH_NOTIFY_CHANNEL` | `none` | `none` \| `twilio` \| `fcm` — real sends only ever happen when this is set to a real channel AND `FLOORWATCH_SHADOW_MODE=false` |
| `FLOORWATCH_CONTACTS_PATH` | `contacts.json` | Pilot contact directory (employee phone/FCM token per zone, supervisor phone) |
| `FLOORWATCH_TWILIO_ACCOUNT_SID` / `_AUTH_TOKEN` / `_FROM_NUMBER` | — | Twilio SMS credentials (Phase 4 task 3) |
| `FLOORWATCH_FCM_CREDENTIALS_PATH` | — | Path to a Firebase service-account JSON (Phase 4 task 3) |
| `FLOORWATCH_AUTH_SECRET` | auto-generated | JWT signing secret, shared with `floorwatch-intelligence` — set explicitly in any real deployment |
| `FLOORWATCH_TOKEN_TTL_SECONDS` | 43200 (12h) | Session token lifetime |
| `FLOORWATCH_CORS_ALLOWED_ORIGINS` | localhost dev origins | Comma-separated allowlist for the dashboard's origin(s) — no wildcard (`SECURITY_REVIEW.md` H1) |
| `FLOORWATCH_DOCS_ENABLED` | `false` | Enables `/docs`/`/redoc`/`/openapi.json` — leave off outside local dev (`SECURITY_REVIEW.md` M4) |
| `FLOORWATCH_RETENTION_DAYS` | 90 | Days of shift-digest history kept before `retention.py` prunes it (`SECURITY_REVIEW.md` M1) |

**No real Twilio account or Firebase project is available in this dev sandbox.** The sender integration code in `app/notifications.py` is real, but has only been verified against mocked SDK clients (`tests/test_notifications.py`) — see `PHASE_4_NOTES.md`.

## Endpoints

Part B:
- `GET /healthz` — liveness + shadow-mode status
- `GET /api/state` — current status of every zone the engine has seen
- `GET /api/queue` — zones currently awaiting supervisor approve/reassign
- `GET /api/digest` — the shift-digest log (Tier 3 escalations)
- `POST /api/queue/zone/{zone_id}/approve` — human approves the drafted directive
- `POST /api/queue/zone/{zone_id}/reassign` — human resolves manually, no directive sent

Part A:
- `POST /api/tasks` — assign a task `{task_name, zone_id, assigned_minutes, task_type?}` (400 if the zone isn't staffed per the roster)
- `GET /api/tasks` — all tasks and their current active/assigned minutes
- `POST /api/tasks/{task_id}/complete` — mark complete; auto-resolves or flags depending on active time vs. that task type's threshold
- `GET /api/queue/tasks` — tasks currently flagged, awaiting supervisor confirm/dismiss
- `POST /api/queue/task/{task_id}/confirm` — supervisor confirms the flag, will follow up with the employee
- `POST /api/queue/task/{task_id}/dismiss` — supervisor dismisses as a false alarm

Both:
- `WS /events` — shared live event stream for the dashboard

## Phase 4 — go-live gate and shift digest

- `go_live_checklist.py` — gates flipping `FLOORWATCH_SHADOW_MODE=false` on
  a reviewed accuracy report, an active roster, a populated contact
  directory, and a live-tested notification channel. Exits non-zero if
  any check fails; safe to wire into a deploy pipeline.
  ```bash
  python go_live_checklist.py --accuracy-report ../../tools/accuracy_audit/accuracy_report.json
  ```
- `shift_digest_job.py` — end-of-shift summarization job (recurring vs.
  one-off pattern tagging by zone + time-of-day bucket). Plain script,
  meant for cron/Task Scheduler/Celery Beat — see its docstring.
  ```bash
  python shift_digest_job.py --date 2026-07-24
  ```

## Horizontal scaling (multiple replicas)

This service is safe to run as 2+ replicas — it wasn't always. `RulesEngine`/
`EffortEngine` are still private, in-process Python objects (unchanged), but
now only the current **leader** replica ever calls their mutating methods,
determined by a Redis-backed lease (`app/leader_election.py`). Everything
else routes through Redis so it works correctly regardless of which replica
a given request lands on:

- **Reads** (`/api/state`, `/api/queue`, `/api/tasks`, `/api/queue/tasks`)
  come from a JSON snapshot the leader writes to Redis after every change —
  every replica answers identically.
- **Mutating REST calls** (approve/reassign/assign_task/complete_task/
  confirm_flag/dismiss_flag) go through a Redis Streams command bus
  (`app/cluster_bus.py`) — a request landing on a follower is forwarded to
  and applied by whichever replica currently holds leadership, not silently
  dropped or applied to an inert local copy.
- **WebSocket broadcast** fans out over a Redis Stream with one consumer
  group per replica, so a dashboard connected to any replica sees every
  event the leader produces, not just events from whichever replica happens
  to be consuming the detection stream.

No new required configuration — this reuses `FLOORWATCH_REDIS_URL`, already
a hard dependency for the event/motion streams. On Railway, just raise the
service's replica count; leadership is acquired automatically, and if the
leader is lost (crash, redeploy) another replica takes over once its lease
expires (default 15s — see `leader_election.py`'s `DEFAULT_LEASE_SECONDS`).

Known residual scope: the login/admin rate limiters
(`skills/lib/floorwatch_rate_limit.py`) are still per-process, not
Redis-backed — a narrower, separately-tracked limitation, not part of this
fix (see that module's docstring).

## Known Phase 2 approximation

Nudge caps and command throttling are scoped per-**zone**, not
per-employee/per-supervisor, because detections only carry per-frame
anonymous `entity_ref`s (no persistent cross-frame identity yet — that's
Phase 3+ re-ID work). Flagged in `engine.py`'s module docstring and
`PHASE_2_NOTES.md`.

## Tests

```bash
pip install -r requirements.txt pytest fakeredis httpx
python -m pytest tests/ -v
```

No Docker/real Redis required — tests use `fakeredis.TcpFakeServer`
against the same `redis` client code path production uses.
