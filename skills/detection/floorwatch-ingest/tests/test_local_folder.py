"""Unit tests for LocalFolderFrameSource — client scenario 2 (local storage)."""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sources.local_folder import LocalFolderFrameSource  # noqa: E402
from sources.media_sampling import sample_video_file  # noqa: E402

pytest.importorskip("PIL")
pytest.importorskip("cv2")


def _make_image(path: Path, fill=128, size=(64, 64)):
    from PIL import Image
    Image.new("RGB", size, color=(fill, fill, fill)).save(path)


def _make_video(path: Path, num_frames=10, fps=10, size=(64, 64)):
    import cv2
    import numpy as np
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, size)
    for i in range(num_frames):
        frame = np.full((size[1], size[0], 3), i * 20 % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()


def test_processes_existing_image_on_first_poll(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    _make_image(folder / "snapshot1.jpg")
    tmp_dir = tmp_path / "frames_out"

    src = LocalFolderFrameSource("cam1", str(folder), fps=1, tmp_dir=str(tmp_dir))
    frame = next(src.frames())
    assert frame.camera_id == "cam1"
    assert Path(frame.frame_path).exists()


def test_does_not_reprocess_same_file(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    _make_image(folder / "snapshot1.jpg")
    tmp_dir = tmp_path / "frames_out"

    src = LocalFolderFrameSource("cam1", str(folder), fps=1, tmp_dir=str(tmp_dir),
                                  poll_interval_seconds=0.01)
    gen = src.frames()
    next(gen)  # consumes snapshot1.jpg
    # second poll cycle should find nothing new (poll loop sleeps then re-scans;
    # simulate directly via _discover_new_files instead of blocking on the generator)
    assert src._discover_new_files() == []


def test_picks_up_new_file_added_later(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    tmp_dir = tmp_path / "frames_out"
    src = LocalFolderFrameSource("cam1", str(folder), fps=1, tmp_dir=str(tmp_dir))

    assert src._discover_new_files() == []
    _make_image(folder / "snapshot1.jpg")
    assert len(src._discover_new_files()) == 1


def test_never_modifies_or_deletes_source_files(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    original = folder / "snapshot1.jpg"
    _make_image(original)
    original_bytes = original.read_bytes()
    tmp_dir = tmp_path / "frames_out"

    src = LocalFolderFrameSource("cam1", str(folder), fps=1, tmp_dir=str(tmp_dir))
    next(src.frames())

    assert original.exists()
    assert original.read_bytes() == original_bytes


def test_samples_video_at_configured_fps(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    _make_video(folder / "clip1.mp4", num_frames=20, fps=10)  # 2 seconds of footage at 10fps source
    tmp_dir = tmp_path / "frames_out"

    src = LocalFolderFrameSource("cam1", str(folder), fps=2, tmp_dir=str(tmp_dir))  # sample at 2fps
    frames = list(sample_video_file(folder / "clip1.mp4", src.fps, src.tmp_frame_path, src.camera_id))
    # 10fps source, sampling at 2fps -> step=5 -> frames at idx 0,5,10,15 = 4 frames
    assert len(frames) == 4
    for f in frames:
        assert f.camera_id == "cam1"


def test_state_persists_across_restarts(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    _make_image(folder / "snapshot1.jpg")
    tmp_dir = tmp_path / "frames_out"
    state_file = tmp_path / "state.json"

    src1 = LocalFolderFrameSource("cam1", str(folder), fps=1, tmp_dir=str(tmp_dir), state_file=str(state_file))
    next(src1.frames())
    assert state_file.exists()

    # simulate a restart: brand new instance, same state file
    src2 = LocalFolderFrameSource("cam1", str(folder), fps=1, tmp_dir=str(tmp_dir), state_file=str(state_file))
    assert src2._discover_new_files() == []  # already-processed file not picked up again


def test_missing_folder_does_not_crash(tmp_path):
    tmp_dir = tmp_path / "frames_out"
    src = LocalFolderFrameSource("cam1", str(tmp_path / "does_not_exist"), fps=1, tmp_dir=str(tmp_dir))
    assert src._discover_new_files() == []


def test_corrupt_image_does_not_crash_ingestion(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    (folder / "corrupt.jpg").write_bytes(b"not a real image")
    tmp_dir = tmp_path / "frames_out"
    src = LocalFolderFrameSource("cam1", str(folder), fps=1, tmp_dir=str(tmp_dir))
    # _use_image just copies bytes (doesn't validate JPEG content) so this
    # actually succeeds at the copy step — real validation happens
    # downstream when detect.py opens it, which correctly surfaces as a
    # retriable per-frame error there, not a crash here. Just confirm
    # pulling one frame from the generator doesn't raise.
    frame = next(src.frames())
    assert frame.camera_id == "cam1"
