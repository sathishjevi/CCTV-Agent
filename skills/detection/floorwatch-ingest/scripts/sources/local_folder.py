"""LocalFolderFrameSource — Client scenario 2: an existing NVR/DVR (or any
process) writes recording files into a local folder, and Floorwatch reads
from there. No cloud account, no camera protocol — just a directory.

Handles two file shapes, since NVR software varies:
  - Video files (.mp4/.avi/.mov/.mkv) — sampled at `fps` using the same
    cv2.VideoCapture mechanism proven against real CCTV footage.
  - Image files (.jpg/.jpeg/.png) — e.g. periodic snapshot exports — used
    directly as a single frame each.
(Sampling logic itself lives in media_sampling.py, shared with
CloudStorageFrameSource/HttpApiFrameSource — see that module's docstring.)

On startup, processes files already present (useful for testing against a
folder you've already populated, or backfilling), then polls for new ones.
Never deletes, moves, or modifies anything in the watched folder — these
are the client's own recordings, not Floorwatch's to touch. "Already
processed" state is tracked separately (in-memory, optionally persisted
to a small JSON state file so a restart doesn't reprocess everything).

Global Constraint 3: this source only ever writes to the single reusable
per-camera temp frame path (overwritten every sample, matching Aegis's
own convention) — it never creates a second copy of the client's video.
"""

import json
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.base import Frame, FrameSource  # noqa: E402
from sources.media_sampling import is_media, sample_media_file  # noqa: E402


def log(msg: str):
    print(f"[floorwatch-ingest:local_folder] {msg}", file=sys.stderr, flush=True)


class LocalFolderFrameSource(FrameSource):
    def __init__(self, camera_id: str, folder: str, fps: float = 1.0,
                 poll_interval_seconds: float = 5.0, tmp_dir: str = "/tmp/aegis_detection",
                 state_file: Optional[str] = None):
        super().__init__(camera_id, fps)
        self.folder = Path(folder)
        self.poll_interval_seconds = poll_interval_seconds
        self.tmp_frame_path = Path(tmp_dir) / f"frame_{camera_id}.jpg"
        self.tmp_frame_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(state_file) if state_file else None
        self._processed = self._load_state()

    def _load_state(self) -> set:
        if self.state_file and self.state_file.exists():
            try:
                return set(json.loads(self.state_file.read_text()))
            except (json.JSONDecodeError, OSError):
                return set()
        return set()

    def _save_state(self):
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(sorted(self._processed)))

    def _file_key(self, path: Path) -> str:
        # mtime included so a file that gets overwritten/appended in place
        # (some NVRs do this) is picked up again, not permanently skipped.
        return f"{path}:{path.stat().st_mtime}"

    def _discover_new_files(self):
        if not self.folder.exists():
            log(f"WARNING: folder {self.folder} does not exist yet")
            return []
        candidates = sorted(p for p in self.folder.iterdir() if p.is_file() and is_media(p))
        return [p for p in candidates if self._file_key(p) not in self._processed]

    def frames(self) -> Iterator[Frame]:
        while True:
            new_files = self._discover_new_files()
            for path in new_files:
                log(f"Processing {path.name}")
                # Marked BEFORE yielding any frames, not after: a caller
                # that only pulls one frame per next() call (the normal
                # case) doesn't resume this generator past its first
                # `yield` until the NEXT call, so a finally-block marker
                # placed after the sampling loop wouldn't actually run
                # until then — for a single-frame image file that meant
                # it was never marked processed at all until a second,
                # unrelated next() call happened to resume this frame.
                # Marking up-front also means a crash partway through a
                # large video doesn't cause it to be silently reprocessed
                # from the start next poll cycle.
                self._processed.add(self._file_key(path))
                self._save_state()
                try:
                    yield from sample_media_file(path, self.fps, self.tmp_frame_path, self.camera_id)
                except Exception as e:
                    log(f"WARNING: error processing {path}: {e} — skipping, continuing")
            time.sleep(self.poll_interval_seconds)
