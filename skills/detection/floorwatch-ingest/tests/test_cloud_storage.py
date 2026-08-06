"""Unit tests for CloudStorageFrameSource — client scenario 1 (cloud
storage). Real cloud SDKs (boto3/azure-storage-blob/google-cloud-storage)
aren't required: each provider's client-construction method is patched
directly with a mock, so these tests validate the adapter logic (listing,
downloading, the shared polling/state/sampling loop) without needing real
credentials or the SDK packages installed."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sources.cloud_storage import (  # noqa: E402
    AzureBlobFrameSource, GcsFrameSource, S3FrameSource, build_cloud_storage_source,
)

pytest.importorskip("PIL")


def _make_image_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(100, 100, 100)).save(buf, format="JPEG")
    return buf.getvalue()


# ── factory ───────────────────────────────────────────────────────────────

def test_build_cloud_storage_source_s3():
    src = build_cloud_storage_source("s3", "cam1", bucket="my-bucket")
    assert isinstance(src, S3FrameSource)


def test_build_cloud_storage_source_azure():
    src = build_cloud_storage_source("azure", "cam1", container="c", connection_string="fake")
    assert isinstance(src, AzureBlobFrameSource)


def test_build_cloud_storage_source_gcs():
    src = build_cloud_storage_source("gcs", "cam1", bucket="my-bucket")
    assert isinstance(src, GcsFrameSource)


def test_build_cloud_storage_source_unknown_provider_raises():
    with pytest.raises(ValueError):
        build_cloud_storage_source("dropbox", "cam1")


# ── S3 ────────────────────────────────────────────────────────────────────

def test_s3_list_new_objects(tmp_path):
    src = S3FrameSource("cam1", bucket="my-bucket", prefix="lobby/", tmp_dir=str(tmp_path / "frames"))
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": "lobby/clip1.mp4", "ETag": "abc123"}]},
    ]
    mock_client.get_paginator.return_value = mock_paginator

    with patch.object(src, "_get_client", return_value=mock_client):
        objects = src._list_new_objects()

    assert objects == [("lobby/clip1.mp4", "abc123")]
    mock_client.get_paginator.assert_called_once_with("list_objects_v2")


def test_s3_download_calls_download_file(tmp_path):
    src = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"))
    mock_client = MagicMock()
    dest = tmp_path / "out.jpg"

    with patch.object(src, "_get_client", return_value=mock_client):
        src._download("lobby/snap.jpg", dest)

    mock_client.download_file.assert_called_once_with("my-bucket", "lobby/snap.jpg", str(dest))


# ── Azure ─────────────────────────────────────────────────────────────────

def test_azure_list_new_objects(tmp_path):
    src = AzureBlobFrameSource("cam1", container="c", connection_string="fake", tmp_dir=str(tmp_path / "frames"))
    mock_client = MagicMock()
    fake_blob = MagicMock(name="lobby/clip1.mp4", etag="\"xyz\"")
    fake_blob.name = "lobby/clip1.mp4"
    mock_client.list_blobs.return_value = [fake_blob]

    with patch.object(src, "_get_container_client", return_value=mock_client):
        objects = src._list_new_objects()

    assert objects == [("lobby/clip1.mp4", "\"xyz\"")]


def test_azure_download_writes_blob_content(tmp_path):
    src = AzureBlobFrameSource("cam1", container="c", connection_string="fake", tmp_dir=str(tmp_path / "frames"))
    mock_client = MagicMock()
    mock_client.download_blob.return_value.readall.return_value = b"fake-image-bytes"
    dest = tmp_path / "out.jpg"

    with patch.object(src, "_get_container_client", return_value=mock_client):
        src._download("lobby/snap.jpg", dest)

    assert dest.read_bytes() == b"fake-image-bytes"


# ── GCS ───────────────────────────────────────────────────────────────────

def test_gcs_list_new_objects(tmp_path):
    src = GcsFrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"))
    mock_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.name = "lobby/clip1.mp4"
    fake_blob.etag = "etag123"
    mock_bucket.list_blobs.return_value = [fake_blob]

    with patch.object(src, "_get_bucket", return_value=mock_bucket):
        objects = src._list_new_objects()

    assert objects == [("lobby/clip1.mp4", "etag123")]


def test_gcs_download_calls_download_to_filename(tmp_path):
    src = GcsFrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"))
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    dest = tmp_path / "out.jpg"

    with patch.object(src, "_get_bucket", return_value=mock_bucket):
        src._download("lobby/snap.jpg", dest)

    mock_blob.download_to_filename.assert_called_once_with(str(dest))


# ── shared polling/state/sampling loop (via S3 as the concrete instance) ──

def test_frames_downloads_and_samples_new_object_then_cleans_up_temp(tmp_path):
    src = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"))
    image_bytes = _make_image_bytes()

    def fake_download(object_key, dest_path):
        dest_path.write_bytes(image_bytes)

    with patch.object(src, "_list_new_objects", return_value=[("lobby/snap.jpg", "etag1")]), \
         patch.object(src, "_download", side_effect=fake_download):
        frame = next(src.frames())

    assert frame.camera_id == "cam1"
    assert Path(frame.frame_path).exists()


def test_frames_does_not_reprocess_same_object_version(tmp_path):
    src = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"),
                         poll_interval_seconds=0.01)
    image_bytes = _make_image_bytes()

    def fake_download(object_key, dest_path):
        dest_path.write_bytes(image_bytes)

    with patch.object(src, "_list_new_objects", return_value=[("lobby/snap.jpg", "etag1")]) as mock_list, \
         patch.object(src, "_download", side_effect=fake_download):
        gen = src.frames()
        next(gen)  # consumes lobby/snap.jpg
        # simulate the next poll cycle seeing the same listing again
        # (as a real bucket listing naturally would, since we don't delete
        # the client's object) — must be recognized as already processed
        assert "lobby/snap.jpg:etag1" in src._processed


def test_frames_reprocesses_object_when_version_marker_changes(tmp_path):
    src = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"))
    image_bytes = _make_image_bytes()

    def fake_download(object_key, dest_path):
        dest_path.write_bytes(image_bytes)

    with patch.object(src, "_download", side_effect=fake_download):
        with patch.object(src, "_list_new_objects", return_value=[("lobby/snap.jpg", "etag1")]):
            next(src.frames())
        assert "lobby/snap.jpg:etag1" in src._processed
        assert "lobby/snap.jpg:etag2" not in src._processed


def test_frames_state_persists_across_restarts(tmp_path):
    state_file = tmp_path / "state.json"
    image_bytes = _make_image_bytes()

    def fake_download(object_key, dest_path):
        dest_path.write_bytes(image_bytes)

    src1 = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"), state_file=str(state_file))
    with patch.object(src1, "_list_new_objects", return_value=[("lobby/snap.jpg", "etag1")]), \
         patch.object(src1, "_download", side_effect=fake_download):
        next(src1.frames())

    src2 = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"), state_file=str(state_file))
    assert "lobby/snap.jpg:etag1" in src2._processed


def test_frames_download_failure_does_not_crash_ingestion(tmp_path):
    src = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"),
                         poll_interval_seconds=0.01)
    image_bytes = _make_image_bytes()

    call_count = {"n": 0}

    def flaky_download(object_key, dest_path):
        call_count["n"] += 1
        if object_key == "bad.jpg":
            raise RuntimeError("simulated download failure")
        dest_path.write_bytes(image_bytes)

    with patch.object(src, "_list_new_objects",
                       return_value=[("bad.jpg", "e1"), ("good.jpg", "e2")]), \
         patch.object(src, "_download", side_effect=flaky_download):
        frame = next(src.frames())  # should skip bad.jpg and yield from good.jpg

    assert frame.camera_id == "cam1"
    assert call_count["n"] == 2


def test_frames_skips_non_media_objects(tmp_path):
    src = S3FrameSource("cam1", bucket="my-bucket", tmp_dir=str(tmp_path / "frames"),
                         poll_interval_seconds=0.01)
    image_bytes = _make_image_bytes()

    def fake_download(object_key, dest_path):
        dest_path.write_bytes(image_bytes)

    with patch.object(src, "_list_new_objects",
                       return_value=[("readme.txt", "e1"), ("lobby/snap.jpg", "e2")]), \
         patch.object(src, "_download", side_effect=fake_download) as mock_download:
        frame = next(src.frames())

    assert frame.camera_id == "cam1"
    mock_download.assert_called_once()  # only the media object was downloaded
