#!/usr/bin/env python3
"""
Floorwatch zone-calibration server — tiny stdlib HTTP server.

Serves the canvas-based calibration UI (index.html), a still frame per
camera from frames/<camera_id>.jpg, and a JSON API to load/save zone
polygons to ../zones/<camera_id>.json — the exact file `scripts/main.py`
reads at runtime.

No video/frame data is written anywhere new: frames/ here holds only the
single calibration still images an operator drops in by hand, and is not
touched by the live detection pipeline.

Usage:
  python calibration_server.py [--port 8765]
  Then open http://localhost:8765/
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CALIBRATION_DIR = Path(__file__).resolve().parent
SKILL_DIR = CALIBRATION_DIR.parent
ZONES_DIR = SKILL_DIR / "zones"
FRAMES_DIR = CALIBRATION_DIR / "frames"

CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _safe_camera_id(camera_id: str) -> bool:
    return bool(CAMERA_ID_RE.match(camera_id))


class CalibrationHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[calibration-server] {fmt % args}")

    def _send_json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_file(CALIBRATION_DIR / "index.html")
            return

        if path == "/api/cameras":
            cameras = sorted(p.stem for p in FRAMES_DIR.glob("*.jpg")) if FRAMES_DIR.exists() else []
            self._send_json(200, {"cameras": cameras})
            return

        if path.startswith("/frames/"):
            camera_id = path[len("/frames/"):].rsplit(".", 1)[0]
            if not _safe_camera_id(camera_id):
                self._send_json(400, {"error": "invalid camera_id"})
                return
            self._send_file(FRAMES_DIR / f"{camera_id}.jpg")
            return

        if path.startswith("/api/zones/"):
            camera_id = path[len("/api/zones/"):]
            if not _safe_camera_id(camera_id):
                self._send_json(400, {"error": "invalid camera_id"})
                return
            zone_file = ZONES_DIR / f"{camera_id}.json"
            if zone_file.exists():
                self._send_json(200, json.loads(zone_file.read_text()))
            else:
                self._send_json(200, {"camera_id": camera_id, "zones": []})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/zones/"):
            camera_id = path[len("/api/zones/"):]
            if not _safe_camera_id(camera_id):
                self._send_json(400, {"error": "invalid camera_id"})
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return

            if data.get("camera_id") != camera_id:
                self._send_json(400, {"error": "camera_id mismatch"})
                return
            if not isinstance(data.get("zones"), list):
                self._send_json(400, {"error": "'zones' must be a list"})
                return
            for z in data["zones"]:
                if not all(k in z for k in ("zone_id", "role_tag", "polygon")):
                    self._send_json(400, {"error": "each zone needs zone_id, role_tag, polygon"})
                    return
                if len(z["polygon"]) < 3:
                    self._send_json(400, {"error": f"zone {z['zone_id']} needs at least 3 polygon points"})
                    return

            ZONES_DIR.mkdir(parents=True, exist_ok=True)
            zone_file = ZONES_DIR / f"{camera_id}.json"
            zone_file.write_text(json.dumps(data, indent=2))
            self._send_json(200, {"ok": True, "saved_to": str(zone_file)})
            return

        self._send_json(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description="Floorwatch zone-calibration server")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    ZONES_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), CalibrationHandler)
    print(f"[calibration-server] Serving on http://127.0.0.1:{args.port}/")
    print(f"[calibration-server] Drop still frames into {FRAMES_DIR}/<camera_id>.jpg")
    print(f"[calibration-server] Zones save to {ZONES_DIR}/<camera_id>.json")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
