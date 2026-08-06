"""Shared "turn a downloaded/local media file into Frame objects" logic —
used by LocalFolderFrameSource, CloudStorageFrameSource, and
HttpApiFrameSource alike, since all three eventually end up with a local
video or image file to sample from, just acquired differently (already
local vs. downloaded from a bucket vs. downloaded from an API). Factored
out once rather than duplicated three times.
"""

import sys
from pathlib import Path
from typing import Iterator

from sources.base import Frame, utcnow_iso

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}


def _log(msg: str):
    print(f"[floorwatch-ingest:media_sampling] {msg}", file=sys.stderr, flush=True)


def _suffix(path: Path) -> str:
    """path.suffix directly, except object_key is sometimes a full URL
    (HttpApiFrameSource) with a query string or fragment after the
    extension — e.g. ".../clip.mp4?token=abc" — which would otherwise
    make Path.suffix return ".mp4?token=abc" and fail every comparison
    against a known extension. Strip those before checking."""
    name = path.name.split("?", 1)[0].split("#", 1)[0]
    return Path(name).suffix.lower()


def is_video(path: Path) -> bool:
    return _suffix(path) in VIDEO_EXTENSIONS


def is_image(path: Path) -> bool:
    return _suffix(path) in IMAGE_EXTENSIONS


def is_media(path: Path) -> bool:
    return is_video(path) or is_image(path)


def sample_video_file(path: Path, fps: float, tmp_frame_path: Path, camera_id: str) -> Iterator[Frame]:
    import cv2
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        _log(f"WARNING: could not open {path} — skipping")
        return
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(src_fps / fps)) if fps > 0 else int(src_fps)

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            cv2.imwrite(str(tmp_frame_path), frame)
            yield Frame(camera_id=camera_id, frame_path=str(tmp_frame_path), timestamp=utcnow_iso())
        idx += 1
    cap.release()


def use_image_file(path: Path, tmp_frame_path: Path, camera_id: str) -> Iterator[Frame]:
    import shutil
    try:
        shutil.copyfile(path, tmp_frame_path)
    except OSError as e:
        _log(f"WARNING: could not read {path}: {e} — skipping")
        return
    yield Frame(camera_id=camera_id, frame_path=str(tmp_frame_path), timestamp=utcnow_iso())


def sample_media_file(path: Path, fps: float, tmp_frame_path: Path, camera_id: str) -> Iterator[Frame]:
    """Dispatches to the right sampler by extension."""
    if is_video(path):
        yield from sample_video_file(path, fps, tmp_frame_path, camera_id)
    elif is_image(path):
        yield from use_image_file(path, tmp_frame_path, camera_id)
    else:
        _log(f"WARNING: unrecognized media type for {path} — skipping")
