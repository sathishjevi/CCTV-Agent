# Detection Skill Protocol

Communication protocol for DeepCamera detection skills integrated with SharpAI Aegis.

## Transport

- **stdin** (Aegis → Skill): frame events and commands
- **stdout** (Skill → Aegis): detection results, ready/error events
- **stderr**: logging only — ignored by Aegis data parser

Format: **JSON Lines** (one JSON object per line, newline-delimited).

## Events

### Ready (Skill → Aegis)

Emitted after model loads successfully. `fps` reflects the skill's configured processing rate. `available_sizes` lists the model variants the skill supports.

```jsonl
{"event": "ready", "model": "yolo2026n", "device": "mps", "classes": 80, "fps": 5, "available_sizes": ["nano", "small", "medium", "large"]}
```

### Frame (Aegis → Skill)

Instruction to analyze a specific frame. `frame_id` is an incrementing integer used to correlate request/response.

```jsonl
{"event": "frame", "frame_id": 42, "camera_id": "front_door", "timestamp": "2026-03-01T14:30:00Z", "frame_path": "/tmp/aegis_detection/frame_front_door.jpg", "width": 1920, "height": 1080}
```

### Detections (Skill → Aegis)

Results of frame analysis. Must echo the same `frame_id` received in the frame event.

```jsonl
{"event": "detections", "frame_id": 42, "camera_id": "front_door", "timestamp": "2026-03-01T14:30:00Z", "objects": [
  {"class": "person", "confidence": 0.92, "bbox": [100, 50, 300, 400]},
  {"class": "car",    "confidence": 0.87, "bbox": [500, 200, 900, 500]}
]}
```

### Error (Skill → Aegis)

Indicates a processing error. `retriable: true` means Aegis can send the next frame.

```jsonl
{"event": "error", "frame_id": 42, "message": "Inference error: ...", "retriable": true}
```

### Stop (Aegis → Skill)

Graceful shutdown command.

```jsonl
{"command": "stop"}
```

## Data Formats

### Bounding Boxes

**Format**: `[x_min, y_min, x_max, y_max]` — pixel coordinates (xyxy).

| Field | Type | Description |
|-------|------|-------------|
| `x_min` | int | Left edge (pixels) |
| `y_min` | int | Top edge (pixels) |
| `x_max` | int | Right edge (pixels) |
| `y_max` | int | Bottom edge (pixels) |

Coordinates are in the original image space (not normalized).

### Timestamps

ISO 8601 format: `2026-03-01T14:30:00Z`

### Frame Transfer

Frames are written to `/tmp/aegis_detection/frame_{camera_id}.jpg` as JPEG files with recycled per-camera filenames (overwritten each cycle). The `frame_path` in the frame event is the absolute path to the JPEG file.

## FPS Presets

| Preset | FPS | Use Case |
|--------|-----|----------|
| Ultra Low | 0.2 | Battery saver |
| Low | 0.5 | Passive surveillance |
| Normal | 1 | Standard monitoring |
| Active | 3 | Active area monitoring |
| High | 5 | Security-critical zones |
| Real-time | 15 | Live tracking |

## Backpressure

The protocol is **request-response**: Aegis sends one frame, waits for the detection result, then sends the next. This provides natural backpressure — if the skill is slow, Aegis automatically drops frames (always uses the latest available frame).
