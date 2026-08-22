"""Unit/integration tests for scene_condition.py — camera->zone mapping
(reused from floorwatch-coverage's own calibration files), event
construction, and the check-interval throttling / stdin-driven main loop."""

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from scene_condition import build_camera_zone_map, judgment_to_event, load_config, main, parse_args  # noqa: E402
from vision_providers import SceneJudgment  # noqa: E402


# ── load_config — FLOORWATCH_SCENE_* env vars (Railway deployment path) ──

def test_load_config_reads_railway_style_env_vars(monkeypatch):
    monkeypatch.delenv("AEGIS_SKILL_PARAMS", raising=False)
    monkeypatch.setenv("FLOORWATCH_SCENE_PROVIDER", "openai")
    monkeypatch.setenv("FLOORWATCH_SCENE_API_KEY", "sk-fake")
    monkeypatch.setenv("FLOORWATCH_SCENE_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("FLOORWATCH_SCENE_REDIS_URL", "redis://real-host:6379/0")
    monkeypatch.setattr(sys, "argv", ["scene_condition.py"])
    args = parse_args()
    config = load_config(args)
    assert config["provider"] == "openai"
    assert config["api_key"] == "sk-fake"
    assert config["model"] == "gpt-4o-mini"
    assert config["redis_url"] == "redis://real-host:6379/0"


def test_load_config_env_vars_ignored_when_unset_falls_back_to_cli_args(monkeypatch):
    monkeypatch.delenv("AEGIS_SKILL_PARAMS", raising=False)
    monkeypatch.delenv("FLOORWATCH_SCENE_PROVIDER", raising=False)
    monkeypatch.setattr(sys, "argv", ["scene_condition.py", "--provider", "claude"])
    args = parse_args()
    config = load_config(args)
    assert config["provider"] == "claude"


def _write_zone_file(zones_dir: Path, camera_id: str, zones: list):
    zones_dir.mkdir(parents=True, exist_ok=True)
    (zones_dir / f"{camera_id}.json").write_text(json.dumps({"zones": zones}))


# ── build_camera_zone_map ─────────────────────────────────────────────────

def test_build_camera_zone_map_single_zone(tmp_path):
    zones_dir = tmp_path / "zones"
    _write_zone_file(zones_dir, "lobby_cam_1", [
        {"zone_id": "concession", "role_tag": "concession", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]},
    ])
    result = build_camera_zone_map(zones_dir)
    assert result == {"lobby_cam_1": ("concession", "concession")}


def test_build_camera_zone_map_uses_first_zone_when_multiple(tmp_path):
    zones_dir = tmp_path / "zones"
    _write_zone_file(zones_dir, "multi_cam", [
        {"zone_id": "zone_a", "role_tag": "usher", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        {"zone_id": "zone_b", "role_tag": "security", "polygon": [[2, 2], [3, 2], [3, 3], [2, 3]]},
    ])
    result = build_camera_zone_map(zones_dir)
    assert result["multi_cam"] == ("zone_a", "usher")


def test_build_camera_zone_map_empty_dir_returns_empty(tmp_path):
    assert build_camera_zone_map(tmp_path / "nonexistent") == {}


def test_build_camera_zone_map_camera_with_no_zones_omitted(tmp_path):
    zones_dir = tmp_path / "zones"
    _write_zone_file(zones_dir, "empty_cam", [])
    result = build_camera_zone_map(zones_dir)
    assert "empty_cam" not in result


# ── judgment_to_event ──────────────────────────────────────────────────────

def test_judgment_to_event_shape():
    judgment = SceneJudgment(needs_task=True, task_type="restock_concession",
                              task_name="Restock the display", message="Picked-over.", confidence=0.75)
    evt = judgment_to_event(judgment, "lobby_cam_1", "concession", "concession", "2026-08-22T00:00:00Z")
    assert evt["event_type"] == "scene_task_suggested"
    assert evt["camera_id"] == "lobby_cam_1"
    assert evt["zone_id"] == "concession"
    assert evt["role_tag"] == "concession"
    assert evt["task_name"] == "Restock the display"
    assert evt["task_type"] == "restock_concession"
    assert evt["confidence"] == 0.75
    assert evt["entity_ref"] is None  # never carries identity


def test_judgment_to_event_drops_empty_timestamp():
    judgment = SceneJudgment(needs_task=True, task_type="clean_door", task_name="x", confidence=0.5)
    evt = judgment_to_event(judgment, "cam1", "zone1", "janitor", "")
    assert "timestamp" not in evt


# ── main() — the stdin-driven loop, check-interval throttling, and
# end-to-end schema-validated stdout emission ──────────────────────────────

def _frame_line(camera_id, frame_path, timestamp="2026-08-22T00:00:00Z"):
    return json.dumps({"event": "frame", "camera_id": camera_id, "frame_path": frame_path,
                        "timestamp": timestamp}) + "\n"


def _run_main(monkeypatch, capsys, stdin_text, argv_extra, provider_mock=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(sys, "argv", ["scene_condition.py"] + argv_extra)
    if provider_mock is not None:
        with patch("scene_condition.build_vision_provider", return_value=provider_mock):
            main()
    else:
        main()
    return capsys.readouterr()


def test_main_emits_scene_task_suggested_for_positive_judgment(tmp_path, monkeypatch, capsys):
    zones_dir = tmp_path / "zones"
    _write_zone_file(zones_dir, "lobby_cam_1", [
        {"zone_id": "concession", "role_tag": "concession", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    ])
    frame_file = tmp_path / "frame.jpg"
    frame_file.write_bytes(b"fake")

    fake_provider = MagicMock()
    fake_provider.judge.return_value = SceneJudgment(
        needs_task=True, task_type="restock_concession", task_name="Restock the display", confidence=0.8)

    stdin_text = _frame_line("lobby_cam_1", str(frame_file))
    out = _run_main(monkeypatch, capsys, stdin_text,
                     ["--zones-dir", str(zones_dir), "--provider", "openai", "--api-key", "sk-fake", "--model", "gpt-4o-mini"],
                     provider_mock=fake_provider)

    lines = [json.loads(l) for l in out.out.strip().splitlines()]
    events = [l for l in lines if l.get("event_type") == "scene_task_suggested"]
    assert len(events) == 1
    assert events[0]["task_name"] == "Restock the display"
    assert events[0]["zone_id"] == "concession"


def test_main_skips_negative_judgment(tmp_path, monkeypatch, capsys):
    zones_dir = tmp_path / "zones"
    _write_zone_file(zones_dir, "lobby_cam_1", [
        {"zone_id": "concession", "role_tag": "concession", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    ])
    frame_file = tmp_path / "frame.jpg"
    frame_file.write_bytes(b"fake")

    fake_provider = MagicMock()
    fake_provider.judge.return_value = SceneJudgment(needs_task=False, confidence=0.9)

    out = _run_main(monkeypatch, capsys, _frame_line("lobby_cam_1", str(frame_file)),
                     ["--zones-dir", str(zones_dir), "--provider", "openai", "--api-key", "sk-fake", "--model", "gpt-4o-mini"],
                     provider_mock=fake_provider)

    lines = [json.loads(l) for l in out.out.strip().splitlines()]
    assert not any(l.get("event_type") == "scene_task_suggested" for l in lines)


def test_main_respects_check_interval_skips_second_frame_too_soon(tmp_path, monkeypatch, capsys):
    """Cost control — a camera checked once must not be re-checked again
    within check_interval_seconds, even if more frames arrive."""
    zones_dir = tmp_path / "zones"
    _write_zone_file(zones_dir, "lobby_cam_1", [
        {"zone_id": "concession", "role_tag": "concession", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    ])
    frame_file = tmp_path / "frame.jpg"
    frame_file.write_bytes(b"fake")

    fake_provider = MagicMock()
    fake_provider.judge.return_value = SceneJudgment(
        needs_task=True, task_type="clean_door", task_name="x", confidence=0.9)

    stdin_text = _frame_line("lobby_cam_1", str(frame_file)) + _frame_line("lobby_cam_1", str(frame_file))
    _run_main(monkeypatch, capsys, stdin_text,
              ["--zones-dir", str(zones_dir), "--provider", "openai", "--api-key", "sk-fake",
               "--model", "gpt-4o-mini", "--check-interval-seconds", "900"],
              provider_mock=fake_provider)

    assert fake_provider.judge.call_count == 1  # second frame arrived too soon, skipped


def test_main_skips_uncalibrated_camera(tmp_path, monkeypatch, capsys):
    zones_dir = tmp_path / "zones"  # no zone files at all
    zones_dir.mkdir()
    frame_file = tmp_path / "frame.jpg"
    frame_file.write_bytes(b"fake")

    fake_provider = MagicMock()
    out = _run_main(monkeypatch, capsys, _frame_line("unknown_cam", str(frame_file)),
                     ["--zones-dir", str(zones_dir), "--provider", "openai", "--api-key", "sk-fake", "--model", "gpt-4o-mini"],
                     provider_mock=fake_provider)

    fake_provider.judge.assert_not_called()
    lines = [json.loads(l) for l in out.out.strip().splitlines()]
    assert not any(l.get("event_type") == "scene_task_suggested" for l in lines)


def test_main_no_provider_configured_never_calls_judge(tmp_path, monkeypatch, capsys):
    """provider="none" (the default) — frames flow through untouched, no
    vision calls, no cost incurred."""
    zones_dir = tmp_path / "zones"
    _write_zone_file(zones_dir, "lobby_cam_1", [
        {"zone_id": "concession", "role_tag": "concession", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    ])
    frame_file = tmp_path / "frame.jpg"
    frame_file.write_bytes(b"fake")

    out = _run_main(monkeypatch, capsys, _frame_line("lobby_cam_1", str(frame_file)),
                     ["--zones-dir", str(zones_dir), "--provider", "none"])

    lines = [json.loads(l) for l in out.out.strip().splitlines()]
    ready = [l for l in lines if l.get("event") == "ready"]
    assert ready and ready[0]["provider_active"] is False
    assert not any(l.get("event_type") == "scene_task_suggested" for l in lines)
