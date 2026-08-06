# Floorwatch — Requirements & Status Document

**Purpose**: one place answering "what was planned, what's actually built, what's missing, and what's next" — instead of piecing it together from six separate `PHASE_N_NOTES.md` files and a security review. Those files are still the source of truth for *how* each thing was verified; this document is the index and the gap analysis.

**How to read the status column**: ✅ Built & verified live · 🟡 Built, verified only against a mock/fallback (real integration code exists, untested against the real third-party service) · ⛔ Not built.

---

## 1. Original scope

Source: `docs/floorwatch/floorwatch_claude_code_brief.md`. Floorwatch is a two-part system built on the DeepCamera/Aegis CCTV platform:

- **Part B — Coverage**: is a work zone (concession, box office, lobby, restrooms, entrances) staffed? Escalates unresolved gaps through a 3-tier flow (nudge → supervisor command → logged escalation).
- **Part A — Effort**: is assigned work actually getting done? Tracks active time against a task's time budget, flags cases where a task is marked complete but detected activity was far below budget.

**Seven global constraints govern every phase** (still true today, not just "as planned"):
1. No automatic discipline/HR action — only nudges, drafted messages, logged flags; a human approves every supervisor-tier action.
2. Anonymous by default — session-scoped `entity_ref`, never persistent identity unless a supervisor has escalated.
3. No new video storage — only structured events persisted, raw video stays in the existing CCTV system.
4. Shadow mode before real notifications.
5. Roster cross-check is a hard precondition before any nudge/flag.
6. One shared event schema across every phase — extended, never forked.
7. The Phase 5 LLM/chat layer is structurally read-only.

Planned phases: **1** DeepCamera foundation/calibration → **2** Coverage engine → **3** Effort engine → **4** Real trial & go-live → **5** Supervisor RAG/chat layer. (Phases 6+ are new, added after this session's competitive analysis — see §5.)

---

## 2. Status summary

| Phase | Scope | Status | Tests passing |
|---|---|---|---|
| 1 | Camera ingestion, zone calibration, raw presence detection | ✅ Built, live-verified against real video (this session) | 16 (`floorwatch-coverage`) |
| 2 | Coverage engine — debounce, 3-tier escalation, Redis event bus, dashboard wiring | ✅ Built, live-verified | included above + rules-engine suite |
| 3 | Effort engine — pose/motion signal, active-time tracking, task flagging | ✅ Built, live-verified | 7 (`floorwatch-pose`) |
| 4 | Accuracy audit, notifications, go-live checklist, shift digest | 🟡 Built, notification channel untested against real Twilio/FCM | included in rules-engine suite |
| 5 | RAG/vector search, MCP server, LLM chat, guardrails | 🟡 Built, untested against real Anthropic/Voyage/Postgres | 72 (`floorwatch-intelligence`) |
| — | Authentication (added mid-session, not in original brief) | ✅ Built, live-verified | 18 (`floorwatch_auth`, counted in the two service totals) |
| — | UI restyle (white/blue theme) | ✅ Built | n/a (visual) |
| 6–8 | POS integration, closed-loop SMS, multi-location rollup | ⛔ Not started — planned this session, see §5 | — |

**Total: 263 tests passing** across `skills/lib`, `floorwatch-coverage`, `floorwatch-pose`, `floorwatch-rules-engine`, `floorwatch-intelligence`, and `tools/accuracy_audit`, as of this document. (`skills/lib` also has 3 pre-existing, unrelated failing tests — traced to a dead code path, `_load_coreml_with_compute_units`, never actually called by `load_optimized` in either the original or current code; not something this project's work broke, left alone rather than scope-crept into.)

---

## 3. What's built — by component

### Detection layer (skills)
- **`skills/detection/yolo-detection-2026/`** — pre-existing base skill; its model-loading bug (bundled RT-DETR-format ONNX silently produced zero detections on non-Apple backends) was found and fixed this session by running real CCTV footage through it for the first time. Regression-tested.
- **`skills/detection/floorwatch-coverage/`** — zone-polygon calibration tool (canvas UI + server), person-to-zone mapping, 60s debounce, ≥0.8 confidence gate, schema-validated `zone_covered`/`zone_gap` output, optional Redis publish.
- **`skills/detection/floorwatch-pose/`** — motion/activity signal. Real mode: MediaPipe PoseLandmarker. Fallback mode (what's actually been exercised): frame-differencing, auto-selected when the model file isn't present.
- **`skills/lib/floorwatch_schema.py`** — the one shared Pydantic event schema, extended per phase, never forked.
- **`skills/lib/floorwatch_auth.py`** — shared auth module (see §3 Auth below).

### `services/floorwatch-rules-engine/`
- Tier 1/2/3 coverage escalation state machine (`engine.py`), effort/active-time state machine (`effort_engine.py`), Redis Stream consumers for both the detection and motion streams, WebSocket broadcast, roster cross-check enforced as a hard precondition, shadow-mode gate.
- REST surface: task assignment/completion, zone approve/reassign, supervisor queues, digest export — all now behind authentication.
- `app/notifications.py` — real Twilio SMS + Firebase Cloud Messaging integrations, gated by `FLOORWATCH_NOTIFY_CHANNEL` and always short-circuited by shadow mode. 🟡 exercised only against mocked SDK clients.
- `go_live_checklist.py` — executable gate checking accuracy rate, roster, contacts, notification-channel readiness. Does **not yet** check that auth is configured (flagged in `SECURITY_REVIEW.md` M3).
- `shift_digest_job.py` — end-of-shift summarization with recurring-vs-one-off pattern tagging.

### `services/floorwatch-intelligence/`
- Vector store (Postgres+pgvector real path 🟡 untested; sqlite fallback ✅ actually used), embedding pipeline (Voyage AI real path 🟡 untested; TF-IDF fallback ✅ actually used), semantic retrieval with mandatory citations, MCP server (3 read-only tools), Anthropic tool-use loop 🟡 (mocked in tests, no live API key configured), chat UI, incident notes store.
- Structurally read-only: no code path in this service can mutate rules-engine state — enforced by test, not just convention (`GUARDRAIL_TEST_RESULTS.md`, 8 adversarial scenarios, all pass).

### `tools/accuracy_audit/`
- Stratified sampling harness (camera × hour-of-day bucket, flagged as a proxy for "lighting/angle," not a real substitute) and false-positive-rate computation. 🟡 only run against simulated ground truth — no real shift history exists yet to review.

### Authentication (added this session, not in the original brief)
- Per-supervisor login, JWT sessions, `supervisor`/`viewer`/`service` roles, shared secret across both services, both dashboards gated with a login screen. Full findings and live-tested evidence in `SECURITY_REVIEW.md`.

### UI
- `dashboard/floorwatch_demo.html`, `dashboard/floorwatch_chat.html`, and the zone-calibration tool restyled to a white background / blue CTA theme.

---

## 4. What's missing — gap list

### Blocking a real pilot (must fix/decide before any real deployment)
| Gap | Why it matters | Where it's tracked |
|---|---|---|
| Real camera never connected | Entire accuracy story is unproven against a live RTSP/ONVIF feed — only synthetic data and one offline video file have been tested. **Ingestion itself is no longer the blocker** — `floorwatch-ingest` (new, this session) now supports RTSP, local folder, cloud storage (S3/Azure/GCS), and third-party provider APIs, so this is purely "we haven't yet pointed it at this specific client's real credentials," not a missing capability. See `CCTV_INTEGRATION_SETUP.md` | `PHASE_1_NOTES.md` |
| No POS/transaction integration | Blocks any "cash leak"/financial-loss feature (see §5) | New, this session |
| CORS still wildcard (`*`) | Now a smaller risk with auth in place, but still lets any page in a supervisor's browser hit the API | `SECURITY_REVIEW.md` H1 |
| No retention policy | History is stored **unlimited, forever**, by default — a data-minimization gap, not just a capacity one | `SECURITY_REVIEW.md` M1 |
| README tells operators to bind `0.0.0.0` | Exposes the service to the whole LAN by default | `SECURITY_REVIEW.md` H3 |
| Employee notice/consent sign-off | Legal/process item, not code — the brief calls this out explicitly and it's never been addressed | `PHASE_4_NOTES.md` |

### Important, not launch-blocking
- No rate limiting on `/api/chat` (cost/DoS exposure once a real API key exists) — `SECURITY_REVIEW.md` H2
- No token revocation list — a leaked token is valid until it expires (12h default)
- Phone numbers/FCM tokens written to logs in plaintext — `SECURITY_REVIEW.md` M2
- `go_live_checklist.py` doesn't check that auth is actually configured — `SECURITY_REVIEW.md` M3
- `/docs`, `/redoc`, `/openapi.json` publicly reachable, fully documenting the API — `SECURITY_REVIEW.md` M4
- Accuracy thresholds (`task_type_thresholds.json`) and false-positive targets are placeholder guesses, never calibrated from real shift data

### Real integrations pending third-party credentials (code is real, untested against the live service)
- Twilio / Firebase Cloud Messaging (notifications)
- Anthropic API key (chat assistant — currently correctly returns "unavailable" rather than faking it)
- Voyage AI API key (real embeddings, vs. current TF-IDF fallback)
- Postgres + pgvector (vs. current sqlite fallback)
- MediaPipe pose model download (network-blocked in this dev sandbox; frame-differencing fallback used instead)

---

## 5. Future phases — planned, not started

From the `usemarty.com` competitive analysis (this session). Marty's public site is thin on technical detail, so treat these as directionally scoped, not fully speced:

**Phase 6 — POS Integration & Cash-Leak Detection**
- POS adapter (pick one system first — this decision gates the whole phase)
- Extend the shared schema with `pos_event` / `cash_leak_flag` types
- Correlation engine matching POS transactions to camera/motion events
- Shadow-mode this at a *higher* bar than coverage/effort — false positives here look like accusations, not just missed nudges

**Phase 7 — Closed-Loop SMS-First GM Workflow**
- Per-flag verification state machine: alert → SMS → timer → re-check → confirm/escalate
- Reorient SMS (already wired) from "supplementary" to "primary" channel for GM-facing flags

**Phase 8 — Scheduled Recap + Multi-Location Rollup**
- Extend `shift_digest_job.py` into a scheduled, auto-sent daily recap
- New portfolio-level service aggregating across multiple site deployments (doesn't exist today — every deployment is single-site)
- `$ recovered`/`$ at risk` estimate model — needs an agreed methodology before building; a wrong financial claim is a bigger liability than a wrong coverage alert

---

## 6. Recommended order of work

Sequenced by dependency and risk, not just phase number:

1. **Close the remaining security gaps** (H1 CORS, H3 bind address, M1 retention policy, M3 add an auth check to the go-live checklist) — small, contained, and Phase 6 will add a financially-sensitive data type that makes an open API worse, not better, to leave unfixed.
2. **Decide the retention window** — answers "how many days of history" with a real number, and Phase 6's financial data makes this more urgent, not less.
3. **Get one real camera connected** — every accuracy claim anywhere in this system (coverage, effort, and any future cash-leak detection) is downstream of this.
4. **Decide the POS system for Phase 6** — this single decision determines the phase's entire scope.
5. **Decide the financial-loss framing risk** (do you want "$ leaked" claims at all, and under what methodology) before Phase 6 writes any code that produces a dollar figure.
6. **Build Phase 6** (POS + cash-leak detection), then **7** (closed-loop SMS), then **8** (recap + multi-location) — in that order, since 7 and 8 both assume Phase 6's detection surface exists.
7. **Real credentials pass**: once real camera + POS decisions are made, do one integration pass to swap every 🟡 fallback (Twilio/FCM, Anthropic, Voyage, Postgres) for the real service and re-verify each against reality instead of a mock.

---

## 7. Decisions needed from you

Consolidated from every phase's "what's needed from you" plus this document's new items:

1. Which of the 4 supported CCTV source types this client actually uses (RTSP/NVR, local folder, cloud storage, or third-party provider API) and real credentials/access for it — see `CCTV_INTEGRATION_SETUP.md` (or continued test-footage-only development)
2. POS system to integrate first (Phase 6 blocker)
3. Whether to build the financial-loss ("$ leaked") framing at all, and what methodology to use if so
4. Retention window (how many days of history should actually be kept)
5. Real Twilio account or Firebase project + real employee/supervisor contact info
6. Real Postgres+pgvector instance, or explicit sign-off that sqlite is fine at pilot scale
7. Voyage AI API key (or a different embeddings decision)
8. `ANTHROPIC_API_KEY` for the application itself
9. Employee notice/consent sign-off (legal/process, not engineering)
