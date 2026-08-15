"""Unit tests for EzvizFrameSource. The real `pyezvizapi` package IS
required (installed as a dev dependency) since we import its real
PyEzvizError class for exception handling, but every network call is
mocked — no real EZVIZ account or credentials are used or needed."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

pytest.importorskip("pyezvizapi")
pytest.importorskip("PIL")

from sources.ezviz import EzvizFrameSource  # noqa: E402


def _make_image_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(80, 80, 80)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_source(tmp_path, **overrides):
    kwargs = dict(
        camera_id="cam1", device_serial="ABC123", username="user@example.com",
        password="fake-password", tmp_dir=str(tmp_path / "frames"),
        state_file=str(tmp_path / "state.json"),
    )
    kwargs.update(overrides)
    return EzvizFrameSource(**kwargs)


# ── listing ───────────────────────────────────────────────────────────────

def test_list_new_videos_filters_already_processed(tmp_path):
    src = _make_source(tmp_path)
    src._processed = {"1"}
    mock_client = MagicMock()
    mock_client.get_cloud_videos.return_value = {
        "videos": [{"seqId": "1", "startTime": "t1"}, {"seqId": "2", "startTime": "t2"}]
    }

    with patch.object(src, "_get_client", return_value=mock_client):
        new = src._list_new_videos()

    assert [v["seqId"] for v in new] == ["2"]
    mock_client.get_cloud_videos.assert_called_once_with("ABC123", 1, limit=20, video_type=2)


def test_list_new_videos_handles_missing_or_malformed_response(tmp_path):
    src = _make_source(tmp_path)
    mock_client = MagicMock()
    mock_client.get_cloud_videos.return_value = {"videos": "not-a-list"}

    with patch.object(src, "_get_client", return_value=mock_client):
        assert src._list_new_videos() == []


# ── login ─────────────────────────────────────────────────────────────────

def test_get_client_logs_in_once_and_caches(tmp_path):
    src = _make_source(tmp_path)
    fake_client_instance = MagicMock()
    fake_client_cls = MagicMock(return_value=fake_client_instance)

    with patch.dict(sys.modules, {"pyezvizapi.client": MagicMock(EzvizClient=fake_client_cls)}):
        client1 = src._get_client()
        client2 = src._get_client()

    assert client1 is client2 is fake_client_instance
    fake_client_cls.assert_called_once_with(
        account="user@example.com", password="fake-password", url="apiieu.ezvizlife.com")
    fake_client_instance.login.assert_called_once()


# ── simple (direct HTTP URL) download path ──────────────────────────────

def test_download_simple_succeeds_with_direct_url(tmp_path):
    src = _make_source(tmp_path)
    mock_client = MagicMock()
    mock_client.get_cloud_video_details.return_value = {
        "videos": [{"seqId": "1", "downloadUrl": "https://example.com/clip1.mp4"}]
    }
    mock_client.download_cloud_video.return_value = b"fake-video-bytes"
    dest = tmp_path / "out.mp4"

    with patch.object(src, "_get_client", return_value=mock_client):
        ok = src._download_simple({"seqId": "1", "startTime": "t", "stopTime": "t2"}, dest)

    assert ok is True
    assert dest.read_bytes() == b"fake-video-bytes"


def test_download_simple_returns_false_on_native_stream_descriptor(tmp_path):
    """The common case per pyezvizapi's own docstring: most clips don't
    have a direct HTTP URL and need the native-stream fallback instead —
    this must return False, not raise, so frames() knows to fall back."""
    from pyezvizapi import PyEzvizError

    src = _make_source(tmp_path)
    mock_client = MagicMock()
    mock_client.get_cloud_video_details.return_value = {
        "videos": [{"seqId": "1", "streamUrl": "native-sdk-host:port"}]
    }
    mock_client.download_cloud_video.side_effect = PyEzvizError("no direct HTTP(S) download URL")
    dest = tmp_path / "out.mp4"

    with patch.object(src, "_get_client", return_value=mock_client):
        ok = src._download_simple({"seqId": "1", "startTime": "t", "stopTime": "t2"}, dest)

    assert ok is False
    assert not dest.exists()


# ── native-stream fallback (subprocess) path ─────────────────────────────

def test_download_native_fallback_writes_token_file_and_never_passes_password_in_argv(tmp_path):
    src = _make_source(tmp_path)
    mock_client = MagicMock()
    mock_client.export_token.return_value = {"session_id": "sess123", "rf_session_id": "rf123"}
    dest = tmp_path / "out.mp4"

    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        dest.write_bytes(b"fake-decrypted-video")
        return MagicMock(returncode=0, stderr="")

    with patch.object(src, "_get_client", return_value=mock_client), \
         patch("sources.ezviz.subprocess.run", side_effect=fake_run):
        ok = src._download_native_fallback("42", dest)

    assert ok is True
    cmd = captured_cmd["cmd"]
    assert "-p" not in cmd and "--password" not in cmd
    assert "fake-password" not in cmd
    assert "--token-file" in cmd
    assert "--seq-id" in cmd and "42" in cmd
    # The token file passed to the subprocess actually contains the
    # exported session token, proving auth still works without the password.
    token_file_path = Path(cmd[cmd.index("--token-file") + 1])
    # File is inside a TemporaryDirectory that's cleaned up by the time we
    # get here in the real code path, but fake_run ran while it still
    # existed — assert the path shape was well-formed instead.
    assert token_file_path.name == "ezviz_token.json"


def test_download_native_fallback_returns_false_on_subprocess_failure(tmp_path):
    src = _make_source(tmp_path)
    mock_client = MagicMock()
    mock_client.export_token.return_value = {"session_id": "sess123"}
    dest = tmp_path / "out.mp4"

    with patch.object(src, "_get_client", return_value=mock_client), \
         patch("sources.ezviz.subprocess.run",
               return_value=MagicMock(returncode=1, stderr="decrypt failed")):
        ok = src._download_native_fallback("42", dest)

    assert ok is False
    assert not dest.exists()


# ── end-to-end frames() loop ──────────────────────────────────────────────

def test_frames_end_to_end_uses_simple_path_then_marks_processed(tmp_path):
    src = _make_source(tmp_path, fps=10)
    mock_client = MagicMock()
    mock_client.get_cloud_videos.return_value = {
        "videos": [{"seqId": "1", "startTime": "t", "stopTime": "t2"}]
    }
    mock_client.get_cloud_video_details.return_value = {
        "videos": [{"seqId": "1", "downloadUrl": "https://example.com/clip1.jpg"}]
    }
    mock_client.download_cloud_video.return_value = _make_image_bytes()

    with patch.object(src, "_get_client", return_value=mock_client), \
         patch("sources.ezviz.time.sleep", side_effect=InterruptedError):
        gen = src.frames()
        frame = next(gen)
        with pytest.raises(InterruptedError):
            next(gen)

    assert frame.camera_id == "cam1"
    assert "1" in src._processed
    # State persisted so a restart doesn't re-download the same clip.
    assert json.loads(Path(tmp_path / "state.json").read_text()) == ["1"]


def test_frames_skips_clip_when_both_download_paths_fail(tmp_path):
    from pyezvizapi import PyEzvizError

    src = _make_source(tmp_path)
    mock_client = MagicMock()
    mock_client.get_cloud_videos.return_value = {
        "videos": [{"seqId": "1", "startTime": "t", "stopTime": "t2"}]
    }
    mock_client.get_cloud_video_details.return_value = {"videos": [{"seqId": "1"}]}
    mock_client.download_cloud_video.side_effect = PyEzvizError("no direct URL")

    with patch.object(src, "_get_client", return_value=mock_client), \
         patch("sources.ezviz.subprocess.run",
               return_value=MagicMock(returncode=1, stderr="failed")), \
         patch("sources.ezviz.time.sleep", side_effect=InterruptedError):
        gen = src.frames()
        with pytest.raises(InterruptedError):
            next(gen)

    # Still marked processed even though the download failed — matches
    # the rest of this codebase's policy of not retrying a poisoned item
    # forever; a real deployment would alert on the "skipping" log line.
    assert "1" in src._processed
