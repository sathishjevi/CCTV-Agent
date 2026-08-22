# Floorwatch Scene-Condition Skill

## What this does

Every other detection skill in this repo answers "is a person present" (floorwatch-coverage) or "how much motion is there" (floorwatch-pose) — mechanical, near-deterministic readings. This skill answers a genuinely different question: **"does this scene show a condition that needs staff attention"** — a messy or depleted display, a spill, a blocked walkway. That's a judgment call, not a sensor reading, so it's the one detection skill in this repo backed by a vision-language model instead of a lightweight CV model.

On a configurable per-camera interval (default 15 minutes — a messy shelf doesn't change at video framerate, and every check costs real money; see "Cost" below), it asks a vision provider to look at the current frame and decide. A positive judgment becomes a schema-compliant `scene_task_suggested` event, published onto the same Redis Stream `floorwatch-coverage` uses. The rules engine already knows what to do with it — see `services/floorwatch-rules-engine/PHASE_3_NOTES.md`'s addendum: it routes straight to the auto-assign path, landing on the department's primary-contact supervisor, exactly like a `zone_escalated` coverage gap does. This skill's only job is the judgment call; everything downstream (auto-assignment, supervisor review, reassignment to an employee) already existed before this skill did.

## Provider-agnostic by design

Not locked to one AI vendor. `scripts/vision_providers.py` supports:

| `provider` | Notes |
|---|---|
| `openai` | OpenAI's vision-capable models (e.g. `gpt-4o-mini`) |
| `grok` | xAI Grok |
| `llama` | Any OpenAI-compatible hosted Llama vision model (Together/Fireworks/Groq/Deepinfra/...) — **requires `base_url`**, since there's no single default host |
| `claude` | Anthropic Claude |
| `none` | Disabled (default) — frames are read but never judged |

`openai`, `grok`, and `llama` share one implementation (`OpenAICompatibleProvider`) — they're all the same request shape, just a different `base_url`/model name. Only `claude` needs a distinct implementation. Adding a fifth OpenAI-compatible provider is a one-line addition, not a new class.

## Cost

Real, recurring, per-camera, per-check-interval money — this isn't free like the presence-detection skills. Rough order of magnitude at a 15-minute interval, 6 cameras: **~$6/month on a cheap provider (e.g. `gpt-4o-mini`) up to ~$40/month on a pricier one** — see the cost comparison in the rules-engine's operational notes for the full per-provider breakdown. Widen `check_interval_seconds` before switching providers if cost matters more than latency; the interval is a much bigger lever than the model choice.

## Configuration

See `config.yaml` for the full parameter list (Aegis skill-registry format). Same `AEGIS_SKILL_PARAMS` env var → `--config` file → CLI args precedence every other skill in this repo uses.

Camera → zone_id/role_tag mapping **reuses `floorwatch-coverage`'s own zone calibration files** (`--zones-dir`, same `<camera_id>.json` format) rather than inventing a second config format for the same information — calibrate a zone once, both skills use it.

## Input / output

**Input:** `frame` JSONL events on stdin — the exact same protocol `floorwatch-ingest` already produces for every other detection skill (`docs/detection-protocol.md`). No new frame-sourcing code needed; wire this skill up wherever `floorwatch-coverage`/`floorwatch-pose` already are, downstream of the same ingest process.

**Output:** `scene_task_suggested` events (schema: `skills/lib/floorwatch_schema.py`) to stdout, and via `--redis-url`/`--redis-stream` onto a Redis Stream — same publish mechanism, same stream, as `floorwatch-coverage`.

## What this deliberately does NOT do

- **No occupancy/presence logic.** Never touches `zone_gap`/`zone_covered` or the Tier 1→2→3 escalation state machine — a completely separate judgment from a completely separate signal.
- **No auto-assignment logic of its own.** This skill only decides *whether* a condition exists; the rules engine (already-existing code, unmodified by this skill) decides *who* it goes to.
- **No line-employee identity anywhere in this skill.** Events carry `zone_id`/`role_tag`, never a person's identity — consistent with the anonymity discipline the rest of this repo already follows.

## Known limitation

A camera calibrated with more than one zone polygon (in `floorwatch-coverage`'s zone files) gets approximated to its *first* calibrated zone here — this skill judges the whole frame, not a per-polygon sub-region the way bbox-based occupancy detection does. Fine for a camera that covers one physical area; not precise for a camera spanning several unrelated zones.
