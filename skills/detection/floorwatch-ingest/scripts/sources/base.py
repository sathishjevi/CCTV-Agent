"""FrameSource — the pluggable input abstraction for floorwatch-ingest.

Why this exists: every detection skill (yolo-detection-2026,
floorwatch-coverage, floorwatch-pose) only ever consumes one thing — a
JSONL `frame` event pointing at a JPEG on disk (docs/detection-protocol.md).
None of them care where that JPEG came from. Historically, the only thing
that ever fed frames into the pipeline was a one-shot test harness reading
a local video file. Real client deployments need one of several actual
source shapes — a live RTSP camera, a local folder an existing NVR writes
to, a cloud storage bucket, or a third-party surveillance platform's API —
and different clients will have different ones (Client A: AWS, Client B:
a local folder). Rather than hardcoding one shape, every concrete source
implements this one interface, and `ingest.py` picks the right one per
camera from config — the detection pipeline itself needs zero changes.

Global Constraint 3 ("No new video storage. Only structured events get
persisted.") applies directly to every FrameSource implementation: none
of them may accumulate a new permanent archive of video/frames. Each
implementation is responsible for either (a) overwriting a single reusable
per-camera temp frame file (matching Aegis's own convention:
/tmp/aegis_detection/frame_{camera_id}.jpg), or (b) downloading a clip to
a temp location, sampling frames from it, and deleting the temp download
immediately after — never leaving a second video store behind.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass
class Frame:
    """One sampled frame, ready to become a `frame` JSONL event."""
    camera_id: str
    frame_path: str      # local path to a JPEG — always transient, see module docstring
    timestamp: str        # ISO8601
    width: Optional[int] = None
    height: Optional[int] = None


class FrameSource(ABC):
    """One instance per camera. `frames()` is an infinite generator for
    live sources (RTSP, polling cloud/folder/API) — the caller (ingest.py)
    controls the sampling cadence by how often it pulls from the iterator,
    or the source enforces its own fps internally; see each implementation.
    """

    def __init__(self, camera_id: str, fps: float = 1.0):
        self.camera_id = camera_id
        self.fps = fps

    @abstractmethod
    def frames(self) -> Iterator[Frame]:
        """Yields Frame objects forever (or until the process is stopped).
        Implementations should handle their own transient failures
        (dropped RTSP connection, a cloud API hiccup, a partially-written
        file) by logging and retrying rather than raising — one bad frame
        should not take down ingestion for every other camera."""
        raise NotImplementedError

    def close(self):
        """Release any held resources (open stream handles, temp dirs).
        Default no-op; override where relevant."""
        pass


def utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
