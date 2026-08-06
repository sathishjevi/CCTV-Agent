"""Unit tests for the fallback (frame-differencing) motion estimator and
the CLI pipeline. No MediaPipe model file is available in this sandbox
(see SKILL.md "Two execution modes"), so build_estimator() will select
fallback mode — that's exactly what these tests exercise, deliberately."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from pose import FallbackMotionEstimator, build_estimator  # noqa: E402

pytest.importorskip("PIL")
pytest.importorskip("numpy")


def _make_image(path: Path, fill: int, size=(64, 64)):
    from PIL import Image
    Image.new("L", size, color=fill).save(path)


def test_fallback_estimator_first_frame_has_no_baseline(tmp_path):
    est = FallbackMotionEstimator()
    img = tmp_path / "f1.jpg"
    _make_image(img, 100)
    score, has_baseline = est.score("cam1", str(img))
    assert has_baseline is False
    assert score == 0.0


def test_fallback_estimator_identical_frames_score_near_zero(tmp_path):
    est = FallbackMotionEstimator()
    img1 = tmp_path / "f1.jpg"
    img2 = tmp_path / "f2.jpg"
    _make_image(img1, 100)
    _make_image(img2, 100)
    est.score("cam1", str(img1))
    score, has_baseline = est.score("cam1", str(img2))
    assert has_baseline is True
    assert score == pytest.approx(0.0, abs=0.01)


def test_fallback_estimator_very_different_frames_score_high(tmp_path):
    est = FallbackMotionEstimator()
    img1 = tmp_path / "f1.jpg"
    img2 = tmp_path / "f2.jpg"
    _make_image(img1, 0)
    _make_image(img2, 255)
    est.score("cam1", str(img1))
    score, has_baseline = est.score("cam1", str(img2))
    assert has_baseline is True
    assert score == 1.0  # clipped at max


def test_fallback_estimator_isolates_state_per_camera(tmp_path):
    est = FallbackMotionEstimator()
    img_a1 = tmp_path / "a1.jpg"
    img_b1 = tmp_path / "b1.jpg"
    _make_image(img_a1, 50)
    _make_image(img_b1, 200)
    est.score("camA", str(img_a1))
    est.score("camB", str(img_b1))
    # second frame for camA identical to its own first — should be low motion,
    # unaffected by camB's very different brightness level
    img_a2 = tmp_path / "a2.jpg"
    _make_image(img_a2, 50)
    score, _ = est.score("camA", str(img_a2))
    assert score == pytest.approx(0.0, abs=0.01)


def test_build_estimator_falls_back_when_model_missing(tmp_path):
    estimator, mode = build_estimator(str(tmp_path / "nonexistent.task"))
    assert mode == "fallback"
    assert isinstance(estimator, FallbackMotionEstimator)


def test_cli_emits_ready_and_pose_motion_events(tmp_path):
    img1 = tmp_path / "f1.jpg"
    img2 = tmp_path / "f2.jpg"
    _make_image(img1, 0)
    _make_image(img2, 255)

    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "pose.py"),
         "--model-path", str(tmp_path / "nonexistent.task"),
         "--active-threshold", "0.1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    ready = json.loads(proc.stdout.readline())
    assert ready["event"] == "ready"
    assert ready["mode"] == "fallback"

    for i, img in enumerate([img1, img2], start=1):
        proc.stdin.write(json.dumps({
            "event": "frame", "frame_id": i, "camera_id": "cam1",
            "timestamp": "2026-07-24T10:00:00Z", "frame_path": str(img),
        }) + "\n")
        proc.stdin.flush()

    first = json.loads(proc.stdout.readline())
    second = json.loads(proc.stdout.readline())
    assert first["active"] is False  # no baseline yet
    assert second["motion_score"] == 1.0
    assert second["active"] is True
    assert second["mode"] == "fallback"

    proc.stdin.write(json.dumps({"command": "stop"}) + "\n")
    proc.stdin.flush()
    proc.terminate()
