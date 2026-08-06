"""Unit tests for HttpApiFrameSource — client scenario 3 (third-party
surveillance provider), plus the query-string filename bugs it exposed in
the shared cloud_storage/media_sampling code (both fixed alongside this
adapter, not just here)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sources.http_api import HttpApiFrameSource  # noqa: E402
from sources.media_sampling import is_media, is_video  # noqa: E402

pytest.importorskip("httpx")
pytest.importorskip("PIL")


def _make_image_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(100, 100, 100)).save(buf, format="JPEG")
    return buf.getvalue()


# ── the underlying bug this adapter exposed ──────────────────────────────

def test_is_media_handles_url_with_query_string():
    assert is_video(Path("https://api.example.com/clips/123.mp4?token=abc&exp=999")) is True
    assert is_media(Path("https://api.example.com/clips/123.mp4?token=abc")) is True


def test_is_media_handles_url_with_fragment():
    assert is_video(Path("https://api.example.com/clips/123.mp4#t=10")) is True


# ── _list_new_objects: field-name mapping ────────────────────────────────

def test_list_new_objects_default_field_names(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips", tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"id": "clip1", "url": "https://api.example.com/dl/clip1.mp4"}]
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp):
        objects = src._list_new_objects()

    assert objects == [("https://api.example.com/dl/clip1.mp4", "clip1")]


def test_list_new_objects_custom_field_names(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips",
                              id_field="clip_id", download_url_field="download_link",
                              tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"clip_id": "abc", "download_link": "https://x/abc.mp4"}]
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp):
        objects = src._list_new_objects()

    assert objects == [("https://x/abc.mp4", "abc")]


def test_list_new_objects_unwraps_common_wrapper_keys(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips", tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"results": [{"id": "clip1", "url": "https://x/clip1.mp4"}]}
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp):
        objects = src._list_new_objects()

    assert objects == [("https://x/clip1.mp4", "clip1")]


def test_list_new_objects_unrecognized_shape_raises(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips", tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"totally_unexpected": "shape"}
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp):
        with pytest.raises(ValueError):
            src._list_new_objects()


def test_list_new_objects_skips_items_missing_download_url(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips", tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"id": "clip1"}]  # no "url" field
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp):
        objects = src._list_new_objects()

    assert objects == []


def test_list_new_objects_uses_separate_version_field_when_configured(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips",
                              version_field="updated_at", tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"id": "clip1", "url": "https://x/clip1.mp4", "updated_at": "2026-08-01T00:00:00Z"}]
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp):
        objects = src._list_new_objects()

    assert objects == [("https://x/clip1.mp4", "2026-08-01T00:00:00Z")]


# ── auth header ───────────────────────────────────────────────────────────

def test_auth_header_sent_when_configured(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips",
                              auth_header_name="Authorization", auth_header_value="Bearer secrettoken",
                              tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        src._list_new_objects()

    _, kwargs = mock_get.call_args
    assert kwargs["headers"] == {"Authorization": "Bearer secrettoken"}


def test_no_auth_header_when_not_configured(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips", tmp_dir=str(tmp_path / "frames"))
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status.return_value = None

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        src._list_new_objects()

    _, kwargs = mock_get.call_args
    assert kwargs["headers"] == {}


# ── download ──────────────────────────────────────────────────────────────

def test_download_streams_response_to_file(tmp_path):
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips", tmp_dir=str(tmp_path / "frames"))
    dest = tmp_path / "out.jpg"
    image_bytes = _make_image_bytes()

    mock_stream_resp = MagicMock()
    mock_stream_resp.raise_for_status.return_value = None
    mock_stream_resp.iter_bytes.return_value = [image_bytes]
    mock_stream_cm = MagicMock()
    mock_stream_cm.__enter__.return_value = mock_stream_resp
    mock_stream_cm.__exit__.return_value = False

    with patch("httpx.stream", return_value=mock_stream_cm):
        src._download("https://api.example.com/dl/clip1.jpg?token=abc", dest)

    assert dest.read_bytes() == image_bytes


# ── end-to-end via the shared frames() loop ──────────────────────────────

def test_frames_end_to_end_with_query_string_url(tmp_path):
    """Reproduces the exact bug scenario: a download URL with a query
    string, flowing through list -> download -> sample without the
    filename ever containing an invalid '?' character."""
    src = HttpApiFrameSource("cam1", list_url="https://api.example.com/clips", tmp_dir=str(tmp_path / "frames"))
    image_bytes = _make_image_bytes()

    mock_list_resp = MagicMock()
    mock_list_resp.json.return_value = [
        {"id": "snap1", "url": "https://api.example.com/dl/snap1.jpg?token=abc&exp=123"}
    ]
    mock_list_resp.raise_for_status.return_value = None

    mock_stream_resp = MagicMock()
    mock_stream_resp.raise_for_status.return_value = None
    mock_stream_resp.iter_bytes.return_value = [image_bytes]
    mock_stream_cm = MagicMock()
    mock_stream_cm.__enter__.return_value = mock_stream_resp
    mock_stream_cm.__exit__.return_value = False

    with patch("httpx.get", return_value=mock_list_resp), \
         patch("httpx.stream", return_value=mock_stream_cm):
        frame = next(src.frames())

    assert frame.camera_id == "cam1"
    assert Path(frame.frame_path).exists()
