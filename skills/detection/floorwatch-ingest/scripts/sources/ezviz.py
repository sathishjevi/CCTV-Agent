"""EzvizFrameSource — client scenario: cloud-only cameras with no local
storage and no official partner API, whose footage is only reachable
through the vendor's consumer account (EZVIZ).

**This is deliberately different from cloud_storage.py's model.** S3/Azure/
GCS involve a scoped, revocable credential issued for read access to one
bucket/container. This source instead logs in AS THE CUSTOMER'S REAL
EZVIZ ACCOUNT, using their actual username/password, against EZVIZ's
unofficial consumer-app API (via the reverse-engineered `pyezvizapi`
package — https://github.com/RenierM26/pyEzvizApi). There is no EZVIZ
Open Platform (official partner API) integration here; that path was
evaluated and would be materially safer (OAuth-style scoped tokens, no
raw password storage) but requires the customer to register a developer
app and go through EZVIZ's own consent flow — a decision explicitly made
to skip that in favor of this faster, riskier path. See
CCTV_INTEGRATION_SETUP.md's EZVIZ section for the full tradeoff writeup.

Consequences of that choice, worth re-reading before deploying this:
  - `FLOORWATCH_EZVIZ_PASSWORD` is the customer's actual account password,
    not a scoped API key — treat it with correspondingly higher care than
    any other secret in this system (see config/secrets.env.template).
  - This talks to an undocumented, unofficial API. It can break without
    notice on an EZVIZ app update, and automating it may violate EZVIZ's
    Terms of Service — confirmed real risk, not hypothetical caution.
  - Untested against a real account in this codebase (no EZVIZ credentials
    available here) — every request shape below is verified against
    pyezvizapi's own installed source and its CLI's reference
    implementation (not guessed), but test this against one real camera
    before relying on it, same as any other new source type.

Two download paths, because EZVIZ's cloud clips aren't uniformly
downloadable:
  1. **Simple**: some clip descriptors carry a direct HTTP(S) URL —
     `EzvizClient.download_cloud_video()` handles these directly, no
     subprocess needed.
  2. **Native-stream fallback**: most clips instead expose a native SDK
     stream descriptor that requires a ticket + camera key + decrypt
     sequence pyezvizapi implements in its CLI (`cloud_video_download`),
     not in a simple public method. Rather than reimplement that
     encryption/decryption logic here (real risk of getting it subtly
     wrong, untestable without real footage), this shells out to that
     CLI for exactly this case. To avoid ever putting the real password
     on a subprocess command line, login happens once in-process and the
     resulting session token is exported to a temp file the CLI reads via
     --token-file — the password itself never appears in argv.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.base import Frame, FrameSource  # noqa: E402
from sources.media_sampling import sample_media_file  # noqa: E402


def log(msg: str):
    print(f"[floorwatch-ingest:ezviz] {msg}", file=sys.stderr, flush=True)


class EzvizFrameSource(FrameSource):
    def __init__(self, camera_id: str, device_serial: str, username: str, password: str,
                 region: str = "apiieu.ezvizlife.com", channel: int = 1, fps: float = 0.5,
                 poll_interval_seconds: float = 15.0, tmp_dir: str = "/tmp/aegis_detection",
                 state_file: Optional[str] = None, list_limit: int = 20):
        super().__init__(camera_id, fps)
        self.device_serial = device_serial
        self.username = username
        self.password = password
        self.region = region
        self.channel = channel
        self.poll_interval_seconds = poll_interval_seconds
        self.list_limit = list_limit
        self.tmp_frame_path = Path(tmp_dir) / f"frame_{camera_id}.jpg"
        self.tmp_frame_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(state_file) if state_file else None
        self._processed = self._load_state()
        self._client = None

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

    def _get_client(self):
        """Lazily imports pyezvizapi and logs in once — a deployment not
        using EZVIZ never needs this package installed."""
        if self._client is None:
            from pyezvizapi.client import EzvizClient
            client = EzvizClient(account=self.username, password=self.password, url=self.region)
            client.login()
            self._client = client
        return self._client

    def _list_new_videos(self) -> list:
        client = self._get_client()
        response = client.get_cloud_videos(
            self.device_serial, self.channel, limit=self.list_limit, video_type=2)
        videos = response.get("videos")
        if not isinstance(videos, list):
            return []
        new = [v for v in videos if isinstance(v, dict) and str(v.get("seqId")) not in self._processed]
        return new

    def _download_simple(self, video: dict, dest_path: Path) -> bool:
        """Tries the direct-HTTP-URL path. Returns False (not an error) if
        this clip doesn't have one — that's the common case, not a bug."""
        from pyezvizapi import PyEzvizError

        client = self._get_client()
        try:
            details = client.get_cloud_video_details(self.device_serial, self.channel, [video])
            detail_list = details.get("videos")
            detail = detail_list[0] if isinstance(detail_list, list) and detail_list else video
            data = client.download_cloud_video(detail)
        except PyEzvizError:
            return False
        dest_path.write_bytes(data)
        return True

    def _download_native_fallback(self, seq_id: str, dest_path: Path) -> bool:
        """Native-SDK-stream clips need pyezvizapi's own CLI (ticket +
        camera-key + decrypt sequence — see module docstring for why this
        isn't reimplemented directly here). Auth via an exported session
        token file, never the raw password, on the subprocess command line."""
        client = self._get_client()
        try:
            export_token = client.export_token
        except AttributeError:
            log("Installed pyezvizapi version has no export_token() — cannot use the "
                "native-stream fallback path. Upgrade pyezvizapi.")
            return False

        with tempfile.TemporaryDirectory(prefix="floorwatch_ezviz_token_") as tmp_dir:
            token_path = Path(tmp_dir) / "ezviz_token.json"
            token_path.write_text(json.dumps(export_token()), encoding="utf-8")
            cmd = [
                sys.executable, "-m", "pyezvizapi",
                "-r", self.region,
                "--token-file", str(token_path),
                "cloud_video_download",
                "--serial", self.device_serial,
                "--channel", str(self.channel),
                "--seq-id", str(seq_id),
                "--output", str(dest_path),
                "--limit", str(self.list_limit),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                log(f"cloud_video_download subprocess failed for seqId={seq_id}: "
                    f"{result.stderr.strip()[:500]}")
                return False
        return dest_path.exists()

    def frames(self) -> Iterator[Frame]:
        while True:
            try:
                for video in self._list_new_videos():
                    seq_id = str(video.get("seqId"))
                    # Marked processed BEFORE downloading — same lesson as
                    # LocalFolderFrameSource/CloudStorageFrameSource: a
                    # single-yield caller pausing mid-generator must not
                    # cause a re-download on the next poll tick.
                    self._processed.add(seq_id)
                    self._save_state()

                    with tempfile.TemporaryDirectory(prefix="floorwatch_ezviz_dl_") as tmp_dir:
                        dl_path = Path(tmp_dir) / f"{seq_id}.mp4"
                        ok = self._download_simple(video, dl_path)
                        if not ok:
                            ok = self._download_native_fallback(seq_id, dl_path)
                        if not ok:
                            log(f"Could not download cloud video seqId={seq_id} for "
                                f"camera '{self.camera_id}' via either path — skipping.")
                            continue
                        try:
                            yield from sample_media_file(
                                dl_path, self.fps, self.tmp_frame_path, self.camera_id)
                        except Exception as e:
                            log(f"Error sampling downloaded clip seqId={seq_id}: {e}")
            except Exception as e:
                log(f"Error polling EZVIZ cloud videos for camera '{self.camera_id}': {e}")

            time.sleep(self.poll_interval_seconds)
