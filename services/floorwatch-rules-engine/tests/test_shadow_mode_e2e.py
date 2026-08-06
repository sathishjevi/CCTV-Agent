"""
Full shadow-mode end-to-end test, per Phase 2 task 5 of the build brief:
"Run the full pipeline in shadow mode: log gap/nudge/escalation events but
suppress any real notification send."

Wires together the REAL pieces, not mocks of them:
  1. skills/detection/floorwatch-coverage/scripts/main.py — spawned as an
     actual subprocess, fed synthetic `detections` JSONL on stdin exactly
     as an upstream detection skill would, publishing onto a Redis Stream.
  2. A fakeredis TcpFakeServer standing in for Redis (no Docker in this
     sandbox — see skills/detection/floorwatch-coverage/tests/test_redis_publish.py
     for the same rationale).
  3. The real RulesEngine (engine.py) consuming that stream and running
     the full Tier 1 -> Tier 2 -> Tier 3 state machine on real wall-clock
     timers (shortened for test speed, not faked).

Asserts every nudge/command send is marked shadow_mode_suppressed=True and
that the escalation reaches the shift digest — i.e. the pipeline observes
and logs everything but never actually sends a notification.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_SCRIPT = REPO_ROOT / "skills" / "detection" / "floorwatch-coverage" / "scripts" / "main.py"
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")

from digest_store import DigestStore  # noqa: E402
from engine import RulesEngine  # noqa: E402
from roster import Roster  # noqa: E402


class AllStaffedRoster:
    def is_zone_staffed(self, zone_id: str) -> bool:
        return True


ZONES_META = {"concession_a": {"name": "Concession Counter", "role_tag": "concession"}}


def test_full_pipeline_shadow_mode_gap_to_escalation(tmp_path):
    # ── 1. fake Redis ────────────────────────────────────────────────
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    redis_url = f"redis://127.0.0.1:{port}/0"

    # ── 2. real zone calibration + real coverage skill subprocess ────
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    (zones_dir / "lobby_cam_1.json").write_text(json.dumps({
        "camera_id": "lobby_cam_1",
        "zones": [{"zone_id": "concession_a", "role_tag": "concession",
                    "polygon": [[100, 150], [400, 150], [400, 500], [100, 500]]}],
    }))

    coverage = subprocess.Popen(
        [sys.executable, str(COVERAGE_SCRIPT),
         "--zones-dir", str(zones_dir),
         "--debounce-seconds", "0.3",
         "--gap-confidence-threshold", "0.8",
         "--redis-url", redis_url],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    ready = json.loads(coverage.stdout.readline())
    assert ready["redis_enabled"] is True

    # Person present (zone_covered), then person gone long enough to clear debounce (zone_gap).
    coverage.stdin.write(json.dumps({
        "event": "detections", "camera_id": "lobby_cam_1", "timestamp": "2026-07-24T18:00:00Z",
        "objects": [{"class": "person", "confidence": 0.9, "bbox": [150, 200, 250, 350]}],
    }) + "\n")
    coverage.stdin.flush()
    coverage.stdout.readline()  # zone_covered

    coverage.stdin.write(json.dumps({
        "event": "detections", "camera_id": "lobby_cam_1", "timestamp": "2026-07-24T18:00:00.5Z",
        "objects": [],
    }) + "\n")
    coverage.stdin.flush()
    time.sleep(0.5)
    coverage.stdin.write(json.dumps({
        "event": "detections", "camera_id": "lobby_cam_1", "timestamp": "2026-07-24T18:00:01Z",
        "objects": [],
    }) + "\n")
    coverage.stdin.flush()
    gap_line = coverage.stdout.readline()
    gap_event = json.loads(gap_line)
    assert gap_event["event_type"] == "zone_gap"

    coverage.stdin.write(json.dumps({"command": "stop"}) + "\n")
    coverage.stdin.flush()
    coverage.terminate()

    # ── 3. real RulesEngine consuming the real Redis stream ──────────
    digest = DigestStore(tmp_path / "digest.jsonl")
    engine = RulesEngine(
        roster=AllStaffedRoster(),
        digest=digest,
        zones_meta=ZONES_META,
        shadow_mode=True,  # the point of this test
        nudge_timer_seconds=0.3,
        command_timer_seconds=0.3,
        resolve_cooldown_seconds=1.0,
    )

    consumer = redis.Redis.from_url(redis_url, decode_responses=True)
    entries = consumer.xrange("floorwatch:events", "-", "+")
    assert len(entries) >= 1

    out_events = []
    for _entry_id, fields in entries:
        raw = json.loads(fields["data"])
        out_events.extend(engine.process_detection_event(raw))

    assert any(e["event_type"] == "zone_nudge_sent" for e in out_events)
    nudge_evt = next(e for e in out_events if e["event_type"] == "zone_nudge_sent")
    assert nudge_evt["shadow_mode_suppressed"] is True  # never actually sent

    # ── 4. let real timers expire -> Tier 2 -> Tier 3 ─────────────────
    deadline = time.time() + 3
    saw_command = saw_escalated = False
    while time.time() < deadline and not saw_escalated:
        for evt in engine.tick():
            if evt["event_type"] == "zone_supervisor_command":
                saw_command = True
                assert "throttled" in evt
            if evt["event_type"] == "zone_escalated":
                saw_escalated = True
        time.sleep(0.05)

    assert saw_command, "Tier 2 supervisor command never fired"
    assert saw_escalated, "Tier 3 escalation never fired"
    assert engine.zones["concession_a"].status == "escalated"

    digest_entries = digest.read_all()
    assert any(e["event_type"] == "zone_escalated" for e in digest_entries), \
        "escalation was not written to the shift-digest store"

    server.shutdown()
