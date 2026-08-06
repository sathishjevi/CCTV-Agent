"""Unit tests for RtspFrameSource. A local video file stands in for a live
RTSP URL — cv2.VideoCapture treats both identically, and connection-drop/
reconnect logic is exercised separately via a mocked capture object."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sources.rtsp import RtspFrameSource  # noqa: E402

pytest.importorskip("cv2")
pytest.importorskip("numpy")


def _make_video(path: Path, num_frames=30, fps=10, size=(64, 64)):
    import cv2
    import numpy as np
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, size)
    for i in range(num_frames):
        writer.write(np.full((size[1], size[0], 3), i % 255, dtype=np.uint8))
    writer.release()


# ── URL redaction ─────────────────────────────────────────────────────────

def test_redact_url_hides_credentials():
    src = RtspFrameSource("cam1", "rtsp://admin:hunter2@192.168.1.50:554/stream1", fps=1)
    redacted = src._redact_url()
    assert "hunter2" not in redacted
    assert "admin" not in redacted
    assert "192.168.1.50:554/stream1" in redacted


def test_redact_url_handles_no_credentials():
    src = RtspFrameSource("cam1", "rtsp://192.168.1.50:554/stream1", fps=1)
    assert src._redact_url() == "rtsp://192.168.1.50:554/stream1"


# ── real sampling against a local file standing in for a live stream ──────

def test_samples_frames_from_stream_like_source(tmp_path):
    video_path = tmp_path / "fake_stream.mp4"
    _make_video(video_path, num_frames=30, fps=10)
    tmp_dir = tmp_path / "frames_out"

    src = RtspFrameSource("cam1", str(video_path), fps=5, tmp_dir=str(tmp_dir))
    gen = src.frames()
    frame1 = next(gen)
    assert frame1.camera_id == "cam1"
    assert Path(frame1.frame_path).exists()
    src.close()


def test_reuses_single_tmp_frame_path_not_accumulating_files(tmp_path):
    """Global Constraint 3 — never a growing archive, always one reusable path."""
    video_path = tmp_path / "fake_stream.mp4"
    _make_video(video_path, num_frames=30, fps=10)
    tmp_dir = tmp_path / "frames_out"

    src = RtspFrameSource("cam1", str(video_path), fps=5, tmp_dir=str(tmp_dir))
    gen = src.frames()
    paths_seen = {next(gen).frame_path for _ in range(3)}
    src.close()
    assert paths_seen == {str(Path(tmp_dir) / "frame_cam1.jpg")}
    assert len(list(Path(tmp_dir).iterdir())) == 1  # exactly one file, never accumulating


# ── reconnect / backoff logic (mocked cv2, no real stream needed) ─────────

def test_reconnects_after_stream_drop(tmp_path):
    tmp_dir = tmp_path / "frames_out"
    src = RtspFrameSource("cam1", "rtsp://fake/stream", fps=100, tmp_dir=str(tmp_dir),
                           initial_backoff_seconds=0.01, max_backoff_seconds=0.02)

    good_cap = MagicMock()
    good_cap.isOpened.return_value = True
    good_cap.read.return_value = (True, __import__("numpy").zeros((10, 10, 3), dtype="uint8"))

    dropped_cap = MagicMock()
    dropped_cap.isOpened.return_value = True
    dropped_cap.read.return_value = (False, None)  # simulates a dropped read

    call_count = {"n": 0}

    def fake_video_capture(url):
        call_count["n"] += 1
        return dropped_cap if call_count["n"] == 1 else good_cap

    with patch("cv2.VideoCapture", side_effect=fake_video_capture), \
         patch("cv2.imwrite", return_value=True):
        gen = src.frames()
        frame = next(gen)  # first connection drops on read; must reconnect and succeed
        assert frame.camera_id == "cam1"

    assert call_count["n"] >= 2  # proves a reconnect actually happened
    src.close()


def test_backoff_on_repeated_open_failure(tmp_path):
    tmp_dir = tmp_path / "frames_out"
    src = RtspFrameSource("cam1", "rtsp://fake/stream", fps=100, tmp_dir=str(tmp_dir),
                           initial_backoff_seconds=0.01, max_backoff_seconds=0.02)

    failing_cap = MagicMock()
    failing_cap.isOpened.return_value = False

    good_cap = MagicMock()
    good_cap.isOpened.return_value = True
    good_cap.read.return_value = (True, __import__("numpy").zeros((10, 10, 3), dtype="uint8"))

    call_count = {"n": 0}

    def fake_video_capture(url):
        call_count["n"] += 1
        return failing_cap if call_count["n"] <= 2 else good_cap

    with patch("cv2.VideoCapture", side_effect=fake_video_capture), \
         patch("cv2.imwrite", return_value=True):
        start = time.monotonic()
        gen = src.frames()
        frame = next(gen)
        elapsed = time.monotonic() - start

    assert frame.camera_id == "cam1"
    assert call_count["n"] == 3  # two failed opens, then success
    assert elapsed >= 0.01  # actually waited through backoff, not busy-looping
    src.close()
