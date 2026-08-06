# Floorwatch Accuracy-Audit Harness

Phase 4 task 1: sample logged detection/coverage/effort events across
different times of day, lighting, and camera angles for manual comparison
against ground truth, before any real notification goes live.

## Stratification proxy — read before using

This pilot's event schema has no separate "lighting condition" field, and
one physical camera corresponds to one "angle." So this harness stratifies
by **(camera_id, hour-of-day bucket)** as the closest available proxy for
"different times of day / lighting / camera angles." That's a reasonable
stand-in, not a substitute — real lighting varies within an hour bucket
(cloud cover, indoor light schedules), and camera_id doesn't capture angle
changes if a camera is ever repositioned. If per-shift metadata (weather,
indoor lighting schedule) becomes available, extend `sample_events.py`'s
stratification key rather than relying on hour-of-day alone.

**No real diverse camera footage was available in this dev sandbox** — no
live cameras, no real shift history. This harness has been verified against
synthetic data with fabricated timestamps; running it against real data is
what actually validates accuracy. Don't treat "the tool runs" as
"accuracy is validated" — see `go_live_checklist.py` at the repo root
services dir for the gate that actually enforces a reviewed sample.

## Workflow

1. Run a shift (or several) so `shift_digest.jsonl` accumulates real
   `zone_gap`/`zone_escalated`/`task_flag` events.
2. Sample a review sheet:
   ```bash
   python sample_events.py --digest ../../services/floorwatch-rules-engine/shift_digest.jsonl --per-stratum 5 --out review_sample.csv
   ```
3. Open `review_sample.csv`, watch/recall what actually happened at each
   `timestamp`/`camera_id`/`zone_id`, and fill in `ground_truth` per row:
   `TP` (real), `FP` (false alarm), `FN` (missed — you noticed something
   the system didn't log), or `unclear`.
4. Compute accuracy stats:
   ```bash
   python compute_accuracy.py --review review_sample.csv --json-out accuracy_report.json
   ```
5. Feed `accuracy_report.json` into `go_live_checklist.py` — it gates
   real notifications on the false-positive rate here being below target.
