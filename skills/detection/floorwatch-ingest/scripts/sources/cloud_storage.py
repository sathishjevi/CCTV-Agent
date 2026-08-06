"""CloudStorageFrameSource — client scenario 1: recordings land in a cloud
bucket/container (AWS S3, Azure Blob Storage, or Google Cloud Storage).
One shared polling/state/sampling implementation in the base class;
each provider only implements "list new objects" and "download one
object" — the two things that actually differ between clouds.

Non-destructive toward the client's own data, same policy as
LocalFolderFrameSource: this NEVER deletes or modifies the object in the
client's bucket. It downloads a TEMPORARY local copy to sample frames
from, then deletes that temporary copy immediately after — Global
Constraint 3 ("no new video storage") applies to floorwatch's own disk,
not to what already exists in the client's cloud account, and this source
never leaves a growing local archive behind either way.

Each cloud SDK (boto3 / azure-storage-blob / google-cloud-storage) is
imported lazily inside its subclass, not at module load — a deployment
using only one cloud shouldn't need all three SDKs installed.
"""

import json
import sys
import tempfile
import time
from abc import abstractmethod
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.base import Frame, FrameSource  # noqa: E402
from sources.media_sampling import is_media, sample_media_file  # noqa: E402


def log(msg: str):
    print(f"[floorwatch-ingest:cloud_storage] {msg}", file=sys.stderr, flush=True)


class CloudStorageFrameSource(FrameSource):
    """Base class — subclasses implement _list_new_objects()/_download()."""

    def __init__(self, camera_id: str, fps: float = 1.0, prefix: str = "",
                 poll_interval_seconds: float = 15.0, tmp_dir: str = "/tmp/aegis_detection",
                 state_file: Optional[str] = None):
        super().__init__(camera_id, fps)
        self.prefix = prefix
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

    @abstractmethod
    def _list_new_objects(self) -> list:
        """Returns [(object_key, version_marker), ...] for objects not yet
        processed. version_marker (e.g. ETag/last-modified) lets an object
        that gets overwritten in place be picked up again, same idea as
        LocalFolderFrameSource's mtime-based file key."""
        raise NotImplementedError

    @abstractmethod
    def _download(self, object_key: str, dest_path: Path):
        """Downloads object_key to dest_path. Must raise on failure."""
        raise NotImplementedError

    def frames(self) -> Iterator[Frame]:
        while True:
            try:
                new_objects = self._list_new_objects()
            except Exception as e:
                log(f"WARNING: could not list objects: {e} — retrying in {self.poll_interval_seconds:.0f}s")
                time.sleep(self.poll_interval_seconds)
                continue

            for object_key, version_marker in new_objects:
                state_key = f"{object_key}:{version_marker}"
                if state_key in self._processed:
                    continue
                log(f"Processing {object_key}")
                # Marked before downloading/sampling — same rationale as
                # LocalFolderFrameSource: avoids reprocessing on a caller
                # that only pulls one frame at a time, and avoids
                # re-downloading a large file from scratch after a crash.
                self._processed.add(state_key)
                self._save_state()

                if not is_media(Path(object_key)):
                    continue

                with tempfile.TemporaryDirectory(prefix="floorwatch_cloud_dl_") as tmpdir:
                    # object_key is sometimes a full URL (HttpApiFrameSource)
                    # with a query string/fragment — strip those before using
                    # it as a local filename. On Windows "?" is an invalid
                    # filename character, so this isn't just cosmetic.
                    clean_name = Path(object_key).name.split("?", 1)[0].split("#", 1)[0] or "download"
                    local_path = Path(tmpdir) / clean_name
                    try:
                        self._download(object_key, local_path)
                    except Exception as e:
                        log(f"WARNING: could not download {object_key}: {e} — skipping")
                        continue
                    try:
                        yield from sample_media_file(local_path, self.fps, self.tmp_frame_path, self.camera_id)
                    except Exception as e:
                        log(f"WARNING: error sampling {object_key}: {e} — skipping, continuing")
                    # TemporaryDirectory context exit deletes the downloaded
                    # copy here — never accumulates a second archive.

            time.sleep(self.poll_interval_seconds)


class S3FrameSource(CloudStorageFrameSource):
    def __init__(self, camera_id: str, bucket: str, region: Optional[str] = None,
                 access_key_id: Optional[str] = None, secret_access_key: Optional[str] = None, **kwargs):
        super().__init__(camera_id, **kwargs)
        self.bucket = bucket
        self._client_kwargs = {}
        if region:
            self._client_kwargs["region_name"] = region
        # If credentials aren't given, boto3's default chain (env vars,
        # ~/.aws/credentials, IAM instance role, etc.) is used — that's
        # the recommended path for anything beyond a quick local test.
        if access_key_id and secret_access_key:
            self._client_kwargs["aws_access_key_id"] = access_key_id
            self._client_kwargs["aws_secret_access_key"] = secret_access_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("s3", **self._client_kwargs)
        return self._client

    def _list_new_objects(self) -> list:
        client = self._get_client()
        paginator = client.get_paginator("list_objects_v2")
        results = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                results.append((obj["Key"], obj.get("ETag", obj.get("LastModified", ""))))
        return results

    def _download(self, object_key: str, dest_path: Path):
        client = self._get_client()
        client.download_file(self.bucket, object_key, str(dest_path))


class AzureBlobFrameSource(CloudStorageFrameSource):
    def __init__(self, camera_id: str, container: str, connection_string: str, **kwargs):
        super().__init__(camera_id, **kwargs)
        self.container = container
        self.connection_string = connection_string
        self._client = None

    def _get_container_client(self):
        if self._client is None:
            from azure.storage.blob import ContainerClient
            self._client = ContainerClient.from_connection_string(
                self.connection_string, container_name=self.container)
        return self._client

    def _list_new_objects(self) -> list:
        client = self._get_container_client()
        results = []
        for blob in client.list_blobs(name_starts_with=self.prefix):
            results.append((blob.name, str(blob.etag)))
        return results

    def _download(self, object_key: str, dest_path: Path):
        client = self._get_container_client()
        with open(dest_path, "wb") as f:
            f.write(client.download_blob(object_key).readall())


class GcsFrameSource(CloudStorageFrameSource):
    def __init__(self, camera_id: str, bucket: str, credentials_path: Optional[str] = None, **kwargs):
        super().__init__(camera_id, **kwargs)
        self.bucket_name = bucket
        self.credentials_path = credentials_path
        self._bucket = None

    def _get_bucket(self):
        if self._bucket is None:
            from google.cloud import storage
            client = (storage.Client.from_service_account_json(self.credentials_path)
                      if self.credentials_path else storage.Client())
            self._bucket = client.bucket(self.bucket_name)
        return self._bucket

    def _list_new_objects(self) -> list:
        bucket = self._get_bucket()
        results = []
        for blob in bucket.list_blobs(prefix=self.prefix):
            results.append((blob.name, blob.etag))
        return results

    def _download(self, object_key: str, dest_path: Path):
        bucket = self._get_bucket()
        blob = bucket.blob(object_key)
        blob.download_to_filename(str(dest_path))


def build_cloud_storage_source(provider: str, camera_id: str, **kwargs) -> CloudStorageFrameSource:
    provider = provider.lower()
    if provider in ("s3", "aws"):
        return S3FrameSource(camera_id, **kwargs)
    if provider in ("azure", "azure_blob"):
        return AzureBlobFrameSource(camera_id, **kwargs)
    if provider in ("gcs", "gcp", "google"):
        return GcsFrameSource(camera_id, **kwargs)
    raise ValueError(f"Unknown cloud storage provider: {provider!r} (expected s3, azure, or gcs)")
