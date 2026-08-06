# Floorwatch — Build Brief for Claude Code

This document is written to be fed to Claude Code as the working spec. Paste it into a `CLAUDE.md` at the repo root, or hand it over section by section per phase. Each phase includes explicit tasks and acceptance criteria — start a fresh Claude Code session per phase and paste that phase's section plus the "Global constraints" section every time.

---

## 0. Project overview (include in every session)

Floorwatch is an employee coverage- and effort-monitoring system for a cineplex, built on top of the open-source **DeepCamera / SharpAI Aegis** platform (https://github.com/SharpAI/DeepCamera) for camera ingestion and detection.

The system has two capabilities:
- **Part B — Coverage**: detects whether a work zone (concession, box office, lobby, restrooms, entrances) is staffed, and escalates unresolved gaps through a 3-tier flow (gamified nudge → supervisor command → logged/escalated).
- **Part A — Effort**: tracks active time spent on a supervisor-assigned task against its time budget, and flags cases where a task is marked complete but active time detected was far below the budget.

A working, presenter-scripted HTML/JS prototype of the dashboard already exists (floor status grid, event feed, supervisor queue, task effort cards). Claude Code's job across Phases 1–4 is to build the real backend pipeline that feeds that dashboard live data instead of a script. Phase 5 adds an optional read-only LLM query layer on top.

---

## 1. Global constraints (include in every session, every phase)

Enforce these without exception, and flag to the user if a task seems to require violating one:

1. **No automatic discipline or HR action.** The system only ever produces nudges, drafted supervisor messages, and logged flags. A human approves every supervisor-tier action. Never wire an escalation directly to any HR/employment action.
2. **Anonymous by default.** Track people with session-scoped anonymous IDs (`entity_ref`), not persistent identity. Named `employee_id` only appears in an event if a supervisor has explicitly escalated — never auto-populate it from face recognition or similar.
3. **No new video storage.** Only structured events (JSON) get persisted. Raw video/frames are never written to a new datastore — they stay wherever DeepCamera/the existing CCTV system already handles them.
4. **Shadow mode before real notifications.** Any new detection capability (coverage, then later effort) must run in a log-only mode first, with notifications suppressed, until accuracy is manually validated.
5. **Roster cross-check before any nudge.** Never let a zone/task nudge/flag fire if the roster shows nobody was actually assigned there — treat this as a hard precondition, not a nice-to-have.
6. **Shared event schema.** All events — from any phase — conform to the schema in Section 2. Extend it with new optional fields if needed; never fork a parallel schema.
7. **Phase 5 is read-only.** The LLM/agent layer in Phase 5 must never be able to call anything that mutates zone state, task state, or triggers a real notification. It only queries and summarizes.

---

## 2. Shared event schema (include in every session)

```json
{
  "event_id": "uuid",
  "timestamp": "ISO8601",
  "camera_id": "string",
  "zone_id": "string",
  "role_tag": "concession | usher | janitor | maintenance | ticketing | security",
  "entity_ref": "anonymous_track_id (default) | employee_id (only if escalated)",
  "event_type": "zone_covered | zone_gap | task_assigned | task_active_time_update | task_flag | task_resolved",
  "task_id": "string | null",
  "active_minutes": "number | null",
  "assigned_minutes": "number | null",
  "confidence": "0.0-1.0",
  "source_model_version": "string"
}
```

Validate every event against this schema at the point it's emitted (Pydantic if Python, Zod if TypeScript). Reject and log malformed events rather than passing them downstream.

---

## PHASE 1 — DeepCamera Foundation & Calibration

**Goal:** DeepCamera/Aegis running against 1–2 pilot camera feeds, a custom Floorwatch skill scaffolded against their JSONL protocol, zones calibrated.

**Tasks for Claude Code:**
1. Clone and study `github.com/SharpAI/DeepCamera` — specifically the skill development guide (`docs/skill-development.md`) and an existing detection skill (`skills/detection/yolo-detection-2026`) as a reference implementation.
2. Set up Aegis locally/on the target edge box; connect it to 1–2 RTSP/ONVIF camera sources (use test/sample RTSP streams if real cineplex cameras aren't available yet — flag this explicitly rather than silently faking success).
3. Confirm the existing `yolo-detection-2026` skill runs and produces person detections via the documented JSONL stdin/stdout protocol.
4. Scaffold a new skill package: `skills/detection/floorwatch-coverage/` with its own `SKILL.md` manifest, following the same structure as the reference skill.
5. Build a zone-calibration tool: a small web UI (canvas over a still frame from each camera) to draw and save zone polygons. Store as `{camera_id, zone_id, polygon: [[x,y],...]}`.
6. Wire the Floorwatch skill to consume detections from `yolo-detection-2026`'s output, map detected person coordinates into zone polygons, and emit **raw presence events** (zone occupied/unoccupied) — no rules or thresholds yet, this phase just proves the data path.

**Acceptance criteria:** Running the pipeline end to end (camera → DeepCamera detection → Floorwatch skill → console/log output) produces correctly zone-mapped presence events for a live or test video feed.

---

## PHASE 2 — Part B: Coverage Engine

**Goal:** Full coverage gap detection, 3-tier escalation state machine, and the existing prototype dashboard wired to live data instead of its script.

**Tasks for Claude Code:**
1. Extend the Floorwatch skill's raw presence output into schema-compliant events (`zone_covered`, `zone_gap`) with debounce (60s) and confidence threshold (≥0.8) applied before emitting a `zone_gap`.
2. Stand up Redis Streams as the event bus. Publish events from the skill; confirm delivery with a stub consumer.
3. Build the rules engine as a standalone service (Python/FastAPI or Node/TypeScript — match whatever the existing prototype/dashboard is in, to minimize glue code):
   - Tier 1: on `zone_gap`, start a nudge timer (3 min), cap 3 nudges/employee/shift.
   - Tier 2: on nudge timeout, or on the 3rd+ gap in a shift (bypass), issue a supervisor-command event (5 min timer, throttle 5/supervisor/hour).
   - Tier 3: on command timeout, mark `logged_escalated` and write to the shift-digest store.
   - 15-minute per-zone resolve cooldown after any resolution.
4. Take the existing HTML/JS dashboard prototype (floor grid, event feed, supervisor queue) and replace its `script[]`/simulated-event logic with a live WebSocket connection to the rules engine, using the same rendering functions already built — do not rewrite the frontend, only swap the event source.
5. Run the full pipeline in **shadow mode**: log gap/nudge/escalation events but suppress any real notification send. Compare a sample against manual observation and tune thresholds.

**Acceptance criteria:** Dashboard reflects real (or realistic test-feed) camera-derived coverage events live, end to end, with shadow-mode logging showing an acceptable false-positive rate before any notification goes live.

---

## PHASE 3 — Part A: Effort Engine

**Goal:** Task assignment, active-time tracking against a time budget, and effort-flag logic wired end-to-end.

**Tasks for Claude Code:**
1. Build a task-assignment input (simple form is fine for pilot): supervisor specifies `task_name`, `zone_id`, `assigned_minutes`. Store as a task record with a start timestamp.
2. Add a pose/motion signal alongside the existing person detection — DeepCamera's YOLO output alone does not give activity/motion classification, so integrate a separate lightweight pose model (e.g. MediaPipe Pose) reading the same frames, to distinguish active movement from standing-still/idle within a zone.
3. Accumulate `active_minutes` per open task window from that pose/motion signal; emit `task_active_time_update` events periodically.
4. Implement effort rules:
   - Mid-task: if active-time ratio falls significantly behind elapsed-time ratio, send a low-effort nudge (reuse the Part B nudge delivery mechanism).
   - At task completion (marked done by employee/supervisor): if `active_minutes` is far below `assigned_minutes`, emit `task_flag` — routed to the supervisor queue as a review item (confirm / dismiss), never an automatic action.
5. Extend the dashboard's task-card UI (already scoped in the prototype) to bind to live `task_*` events instead of the demo script.
6. Run Part A in shadow mode first, same as Part B. Use shadow-mode data across a few different task types to calibrate what a normal active-time ratio looks like **per task type** — do not hardcode one global threshold across all task types.
7. Wire in a roster/schedule cross-check (a manually maintained list is fine for pilot) so tasks/zones with nobody assigned never generate a flag or nudge.

**Acceptance criteria:** A test task assignment with deliberately low simulated activity produces a correctly timed nudge and, if completed with low cumulative active time, a properly formatted flag in the supervisor queue — with shadow-mode data available to justify the threshold used.

---

## PHASE 4 — Real Trial & Review

**Goal:** Validate accuracy/bias, go live with real notifications on pilot zones, finalize reporting.

**Tasks for Claude Code:**
1. Build a lightweight accuracy-audit harness: sample detection events across different times of day / lighting / camera angles and log them for manual comparison against ground truth.
2. Write an end-to-end integration test that runs the full pipeline (camera feed → DeepCamera → Floorwatch skill → rules engine → dashboard → notification-send stub) unattended for a simulated shift, and asserts no unhandled errors or dropped events.
3. Wire a real notification channel: Firebase Cloud Messaging for an employee app, or Twilio SMS as a no-app fallback for the pilot. Keep this behind a config flag so shadow mode can still be re-enabled instantly if needed.
4. Build the go-live checklist as an actual script/checklist artifact (not just documentation) that verifies: shadow-mode false-positive rate below target, roster cross-check active, notification channel tested, before flipping the "real notifications" flag on.
5. Build the shift-digest job (scheduled, e.g. cron or Celery beat) that summarizes the day's gap/flag events, tags patterns (recurring same zone/time vs. one-off), and stores/exports the digest.

**Acceptance criteria:** The go-live checklist passes, real notifications fire correctly for at least one full test shift with no false escalations, and a shift digest is generated automatically at end of shift from real event data.

---

## PHASE 5 — Supervisor Intelligence Layer (RAG + Vector DB + MCP)

**Goal:** A read-only natural-language query layer over historical events for supervisors. This phase must not be able to affect Phase 1–4's live system in any way beyond reading from its data store.

**Tasks for Claude Code:**
1. Add `pgvector` to the existing Postgres/Timescale instance used for event storage (reuse infra — do not stand up a separate vector database for a pilot-scale deployment).
2. Build an embedding pipeline that embeds shift digests and any supervisor-written incident notes as they're created, storing vectors alongside a reference to the source record.
3. Build retrieval logic: semantic search over embedded digests/notes (e.g. "find similar past coverage gaps in this zone").
4. Integrate an LLM (e.g. Claude via API) to answer supervisor questions, grounding answers in retrieved records — cite which shift/date/zone the answer is drawn from rather than answering from general knowledge.
5. Build an MCP server that wraps **read-only** endpoints of the event-store API (current zone status, current task status, historical query) as callable tools.
6. Wire the MCP tools into the LLM's available tool set; test multi-step queries that require both retrieval (history) and a live tool call (current status) in the same answer.
7. Build a simple chat UI for supervisors. It must have no write path to zone state, task state, or the notification system — verify this at the API layer, not just by omitting UI buttons.
8. Write and run adversarial guardrail tests: attempt, via crafted prompts, to get the agent to trigger a nudge, approve a command, or otherwise mutate state through the chat interface. All such attempts must fail. Document the test cases and results.

**Acceptance criteria:** A supervisor can ask a natural-language question spanning both historical patterns and live status and get a grounded, cited answer; guardrail tests confirm zero write-capability from the chat layer.

---

## How to use this with Claude Code

- Start a new Claude Code session per phase. Paste **Section 0, 1, and 2** every time (project overview, constraints, schema), then the relevant phase section.
- Ask Claude Code to restate the acceptance criteria back to you before it starts, so you can confirm scope before it writes code.
- After each phase, ask Claude Code to write a short `PHASE_N_NOTES.md` summarizing what was built, any deviations from this brief, and what it needs from you (credentials, real camera access, roster data, etc.) before the next phase can start for real rather than against test data.
