# Floorwatch Zone Calibration Tool

Standalone canvas web tool for drawing per-camera work-zone polygons. No
build step, no framework — plain HTML5 canvas + a stdlib Python HTTP server.

## Usage

1. Drop one still frame per camera into `frames/<camera_id>.jpg`. The frame
   **must be the same resolution** as the live feed the upstream detection
   skill (`yolo-detection-2026`) receives — zone polygons are pixel
   coordinates in that frame space.
2. Start the server:
   ```bash
   python calibration_server.py --port 8765
   ```
3. Open `http://localhost:8765/` in a browser, pick a camera, click to draw
   each zone polygon (3+ points), name it and tag its role, then "Close &
   Save Zone" and finally "Save all zones to server".
4. Zones are written to `../zones/<camera_id>.json` — exactly what
   `scripts/main.py` reads at runtime. No video/frame data is persisted by
   this tool beyond the single calibration still you dropped in `frames/`.

## Zone file format

```json
{
  "camera_id": "lobby_cam_1",
  "zones": [
    { "zone_id": "concession_a", "role_tag": "concession", "polygon": [[120,80],[420,80],[420,360],[120,360]] }
  ]
}
```
