"""HttpApiFrameSource — client scenario 3: a third-party surveillance
platform/storage provider exposes its own REST API rather than raw
RTSP/ONVIF or a plain cloud bucket. This is a TEMPLATE, not a finished
integration — every such vendor's API shape differs (auth scheme, field
names, pagination, whether it returns clips or single snapshots), so this
genuinely cannot be finished generically. What CAN be built generically:
the common shape most such APIs share (a "list recent items" endpoint
returning JSON, each item with an ID and a download URL, and a way to
authenticate), with the vendor-specific bits — field names, auth header
format, base URL — pulled out as config rather than hardcoded.

Reuses CloudStorageFrameSource's polling/state/download/sampling loop
(identical shape: list new items, download one, sample frames, clean up)
— only _list_new_objects/_download differ, so this file is deliberately
small.

To integrate a REAL third-party provider once you have their docs: point
`list_url`/`id_field`/`download_url_field`/`auth_header` at what their API
actually returns, or — if their API shape doesn't fit this "list then
download" pattern at all (e.g. it's webhook-push rather than poll, or
paginated differently) — subclass HttpApiFrameSource and override
`_list_new_objects`/`_download` directly; the rest of the class (state
tracking, temp-file cleanup, frame sampling) still applies unchanged.
"""

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.cloud_storage import CloudStorageFrameSource  # noqa: E402


def log(msg: str):
    print(f"[floorwatch-ingest:http_api] {msg}", file=sys.stderr, flush=True)


class HttpApiFrameSource(CloudStorageFrameSource):
    def __init__(self, camera_id: str, list_url: str,
                 id_field: str = "id", download_url_field: str = "url",
                 version_field: Optional[str] = None,
                 auth_header_name: Optional[str] = None, auth_header_value: Optional[str] = None,
                 **kwargs):
        super().__init__(camera_id, **kwargs)
        self.list_url = list_url
        self.id_field = id_field
        self.download_url_field = download_url_field
        self.version_field = version_field  # e.g. "updated_at" — falls back to id_field if unset
        self.auth_header_name = auth_header_name
        self.auth_header_value = auth_header_value

    def _headers(self) -> dict:
        if self.auth_header_name and self.auth_header_value:
            return {self.auth_header_name: self.auth_header_value}
        return {}

    def _list_new_objects(self) -> list:
        import httpx
        resp = httpx.get(self.list_url, headers=self._headers(), timeout=10.0)
        resp.raise_for_status()
        items = resp.json()
        if not isinstance(items, list):
            # Some APIs wrap the array, e.g. {"results": [...]} or {"clips": [...]}.
            # Try the common wrapper keys before giving up — still generic,
            # a genuinely different shape needs a subclass override.
            for key in ("results", "items", "clips", "data"):
                if isinstance(items, dict) and key in items and isinstance(items[key], list):
                    items = items[key]
                    break
            else:
                raise ValueError(f"Unexpected response shape from {self.list_url}: {type(items)}")

        results = []
        for item in items:
            download_url = item.get(self.download_url_field)
            if not download_url:
                continue
            version = item.get(self.version_field) if self.version_field else item.get(self.id_field)
            results.append((download_url, version))
        return results

    def _download(self, object_key: str, dest_path: Path):
        """Here `object_key` is the full download URL — see module
        docstring for why this base class's (key, version) shape maps
        naturally onto (download_url, version_marker) for an HTTP API."""
        import httpx
        with httpx.stream("GET", object_key, headers=self._headers(), timeout=30.0) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
