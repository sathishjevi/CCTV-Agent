"""RtspFrameSource — client scenario: a live camera or NVR exposing an
RTSP (or ONVIF-negotiated RTSP) stream directly. This is the ingestion
shape the original build brief assumed DeepCamera/Aegis would own — this
adapter is a small, self-contained stand-in that speaks the exact same
downstream `frame` JSONL protocol, proven against real footage using the
identical cv2.VideoCapture mechanism (a video FILE and an RTSP URL are the
same API to OpenCV — only the source string differs).

Unlike a video file, an RTSP source is a live, continuous, indefinite
stream — sampling is done by wall-clock interval (grab a frame, sleep
until the next tick), not by counting source frames. Streams also drop
and need reconnecting, which video files never do — this adapter retries
with backoff rather than treating a dropped connection as fatal, so one
camera's bad network doesn't take the whole ingestion process down.
"""

import sys
import time
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.base import Frame, FrameSource, utcnow_iso  # noqa: E402


def log(msg: str):
    print(f"[floorwatch-ingest:rtsp] {msg}", file=sys.stderr, flush=True)


class RtspFrameSource(FrameSource):
    def __init__(self, camera_id: str, url: str, fps: float = 1.0,
                 tmp_dir: str = "/tmp/aegis_detection",
                 max_backoff_seconds: float = 30.0, initial_backoff_seconds: float = 1.0):
        super().__init__(camera_id, fps)
        self.url = url
        self.tmp_frame_path = Path(tmp_dir) / f"frame_{camera_id}.jpg"
        self.tmp_frame_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_backoff_seconds = max_backoff_seconds
        self.initial_backoff_seconds = initial_backoff_seconds
        self._cap = None

    def _redact_url(self) -> str:
        """For log lines — never print embedded RTSP credentials."""
        if "@" in self.url and "://" in self.url:
            scheme, rest = self.url.split("://", 1)
            if "@" in rest:
                _creds, host_part = rest.split("@", 1)
                return f"{scheme}://***:***@{host_part}"
        return self.url

    def _open(self) -> bool:
        import cv2
        self._cap = cv2.VideoCapture(self.url)
        if not self._cap.isOpened():
            self._cap = None
            return False
        return True

    def _close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def close(self):
        self._close()

    def frames(self) -> Iterator[Frame]:
        import cv2

        backoff = self.initial_backoff_seconds
        sample_interval = (1.0 / self.fps) if self.fps > 0 else 1.0

        while True:
            if self._cap is None:
                log(f"Connecting to {self._redact_url()}")
                if not self._open():
                    log(f"WARNING: could not open stream — retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff_seconds)
                    continue
                backoff = self.initial_backoff_seconds  # reset on successful (re)connect
                log("Connected")

            t0 = time.monotonic()
            ok, frame = self._cap.read()
            if not ok:
                log("WARNING: stream read failed — reconnecting")
                self._close()
                continue

            cv2.imwrite(str(self.tmp_frame_path), frame)
            yield Frame(camera_id=self.camera_id, frame_path=str(self.tmp_frame_path), timestamp=utcnow_iso())

            elapsed = time.monotonic() - t0
            remaining = sample_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
