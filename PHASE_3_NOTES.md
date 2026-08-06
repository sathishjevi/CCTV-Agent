# Phase 3 Notes — Part A: Effort Engine

## What was built

1. **`skills/detection/floorwatch-pose/`** — new skill, structured like `yolo-detection-2026`/`floorwatch-coverage`. Consumes `frame` events (same protocol as the detection skill — Aegis fans the same camera frame out to both) and emits a per-frame motion score. Two modes:
   - **Real**: MediaPipe PoseLandmarker (Tasks API), tracking landmark displacement frame-to-frame per camera.
   - **Fallback**: PIL+numpy grayscale frame-differencing, used automatically when the `.task` model file isn't present. Every event is tagged `"mode": "real"|"fallback"` so downstream consumers know which produced it.
   `deploy.sh`/`deploy.bat` attempt to download the model and fall back cleanly with a warning if that fails — see "Deviation" below for why fallback is what actually ran here.
2. **`app/effort_engine.py`** (in the existing `floorwatch-rules-engine` service, not a new service — see rationale below) — the Part A state machine: task assignment (roster-gated), active-minutes accumulation (only counted while the task's zone is Part-B-covered *and* motion is active), periodic `task_active_time_update`, a one-shot mid-task low-effort nudge (shadow-mode-suppressed exactly like Part B's), and completion-time flagging against a **per-task-type** threshold (`task_type_thresholds.json` — never one global percentage, per the brief). 18 passing unit tests.
3. **`services/floorwatch-rules-engine/app/main.py`** extended: a second Redis Stream consumer (`floorwatch:motion`), task REST endpoints (`POST /api/tasks`, `GET /api/tasks`, `POST /api/tasks/{id}/complete`, `GET /api/queue/tasks`, `POST /api/queue/task/{id}/confirm|dismiss`), all broadcasting over the same WebSocket as Part B.
4. **`dashboard/floorwatch_demo.html`** — added a task-assignment mini form, wired `applyEvent()` to handle all `task_*` event types, a live-updating elapsed-time bar, a "Mark complete" action per task card, and real REST calls for the existing confirm/dismiss queue buttons (previously local-only mutation from the Phase 2 wiring pass).
5. **Verified live in-browser**, not just in tests: assigned a task through the real form, fed synthetic motion events onto Redis, watched the active/elapsed bars update in real time, watched the mid-task nudge fire, clicked "Mark complete," watched it land in the supervisor queue as a `task_flag`, and clicked Confirm to resolve it — the full Part A loop, through the real REST/WebSocket stack.
6. **Full shadow-mode end-to-end test** (`test_shadow_mode_effort_e2e.py`) chaining the *real* `floorwatch-pose` subprocess → real Redis Stream → real `EffortEngine`, asserting the nudge is `shadow_mode_suppressed` and the flag lands in the shift digest.

## Architectural decision: EffortEngine lives in the existing rules-engine service, not a new one

The brief's Phase 3 tasks don't specify where Part A's logic should run. Rather than standing up a third service, I put `EffortEngine` alongside `RulesEngine` in `floorwatch-rules-engine` because:
- It needs to read Part B's live zone-occupancy state (motion in an empty zone isn't effort) — cheapest and least fragile as a direct in-process read (`_zone_is_covered`), not another network hop.
- Task 4 explicitly says to "reuse the Part B nudge delivery mechanism" — same process makes that a literal shared pattern (`shadow_mode_suppressed` flag, same `on_notify` hook shape) rather than a re-implementation.
- Both already need the same WebSocket broadcast to the same dashboard.

Flagging this as a decision, not a brief requirement, in case you'd rather split it out later for independent scaling.

## Deviations from the brief — flagged explicitly

- **The pose skill ran in fallback (frame-differencing) mode for all testing here, not real MediaPipe pose tracking.** Downloading the `.task` model bundle needs internet access to `storage.googleapis.com`, which this sandbox's network policy blocks (SSL interception on arbitrary hosts — same issue noted for the YOLO26 model in `PHASE_1_NOTES.md`). `deploy.sh` attempts the real download and only falls back with a loud warning if it fails — this isn't a shortcut taken silently, and every emitted event carries `"mode": "fallback"` so it's visible downstream too. **Before Phase 4, please confirm this can run with internet access** so real per-person pose landmark tracking is validated, not just frame-differencing.
- **Motion is per-camera, not per-person.** `floorwatch-pose` doesn't do multi-person pose detection in this pilot (MediaPipe's `PoseLandmarker` here is configured for `num_poses=1`, and the fallback mode has no person concept at all — it's whole-frame pixel differencing). Combined with Part B's zone occupancy, "zone occupied + motion above threshold" is treated as that zone's task being worked on. This is a reasonable approximation for a single-occupant-zone pilot; it will misattribute activity in a multi-occupant zone (e.g. a customer walking through a zone with an idle employee would read as "active"). Flagged in both `SKILL.md` and `effort_engine.py`'s docstring.
- **Per-task-type thresholds in `task_type_thresholds.json` are placeholder pilot defaults** (0.4–0.6 expected active ratio depending on type), not calibrated from real shadow-mode data — the brief explicitly calls for calibrating these from shadow-mode observation before flag thresholds go live (Phase 3 task 6). **Needs a real shadow-mode data collection period per task type before any real flag reaches a supervisor.**
- **Mid-task nudge is capped at one per task** (not explicitly specified in the brief either way) — chosen to avoid nudge spam on a long task that stays behind schedule; flagged in case you want a repeat/escalating nudge instead.
- **Roster cross-check applies at task assignment and again at nudge time**, but not re-checked at completion/flag time (a flag on a task whose zone roster status changed mid-task will still evaluate normally) — matches the brief's wording ("tasks/zones with nobody assigned never generate a flag **or nudge**"; completion flagging isn't listed alongside nudge/flag-gate in that sentence, but worth confirming this reading is what you intended).

## What's needed from you before Phase 4

1. **An environment with internet access** to validate the real MediaPipe pose model — same ask as Phase 1's YOLO26 model, now doubled up for both detection skills.
2. **Shadow-mode data across real task types** to calibrate `task_type_thresholds.json` for real — right now every number in it is a placeholder guess, not measured.
3. **Confirm the per-camera (not per-person) motion approximation is acceptable for pilot zones**, or flag if any pilot zone is expected to have multiple people working simultaneously (breaks the current attribution logic).
4. Everything still outstanding from `PHASE_1_NOTES.md`/`PHASE_2_NOTES.md` (real camera access, real Redis validation, dashboard `renderEmpPreview` discrepancy) remains open.
