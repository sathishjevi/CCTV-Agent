---
name: floorwatch-ingest
description: "Floorwatch Ingest — pulls frames from wherever a client's CCTV footage actually lives (RTSP camera, local folder, cloud storage, a third-party provider's API, or EZVIZ) and feeds them into the standard detection pipeline"
version: 0.1.0
icon: assets/icon.png
entry: scripts/ingest.py
deploy: deploy.sh

requirements:
  python: ">=3.9"
  platforms: ["linux", "macos", "windows"]

parameters:
  - name: auto_start
    label: "Auto Start"
    type: boolean
    default: false
    description: "Start this skill automatically when Aegis launches"
    group: Lifecycle

  - name: cameras_config_path
    label: "Cameras Manifest Path"
    type: string
    default: "cameras.json"
    description: "Path to the multi-camera source manifest (see cameras.json.template) — one entry per camera, each with its own source type and settings"
    group: Sources

capabilities:
  frame_ingestion:
    script: scripts/ingest.py
    description: "Pulls frames from any configured source type and emits the standard frame JSONL protocol, unmodified from what Aegis itself would produce"
---

# Floorwatch Ingest

Every detection skill in this repo (`yolo-detection-2026`, `floorwatch-coverage`, `floorwatch-pose`) only ever consumes one thing: a `frame` JSONL event pointing at a JPEG file
(see `docs/detection-protocol.md`). None of them care where that JPEG came from — that assumption is what makes this skill possible without touching any of them.

**Why this exists**: the original build brief only ever described one ingestion shape — a live RTSP/ONVIF camera feed via DeepCamera/Aegis. Real client deployments don't all look like that. A given client's CCTV footage will be in exactly one of:

1. **A live camera / NVR exposing RTSP or ONVIF** — the brief's original assumption
2. **A local folder** an existing NVR/DVR already writes recordings into
3. **Cloud storage** (AWS S3, Azure Blob Storage, or Google Cloud Storage)
4. **A third-party surveillance platform's own API**
5. **EZVIZ** — a specific, real vendor with cloud-only cameras and no official partner API path taken. **Read `sources/ezviz.py`'s module docstring before using this one** — it's meaningfully different from scenarios 3/4: it authenticates with the customer's real EZVIZ account password against an unofficial API, not a scoped credential. That was an explicit, informed decision for this project, not an oversight — see `CCTV_INTEGRATION_SETUP.md`'s EZVIZ section for the full tradeoff.

Different clients will have different ones — this skill supports all five behind one config choice per camera, so the detection pipeline downstream never needs to know or care which one is in play.

## Architecture

```
skills/detection/floorwatch-ingest/scripts/
  ingest.py              # main loop: reads cameras.json, runs one FrameSource per camera, emits JSONL
  sources/
    base.py              # FrameSource interface + Frame dataclass
    local_folder.py       # scenario 2
    rtsp.py                # scenario 1
    cloud_storage.py       # scenario 3 (S3 / Azure Blob / GCS, one shared base + one subclass each)
    http_api.py             # scenario 4 (generic template — see its docstring for what's genuinely vendor-specific)
    ezviz.py                # scenario 5 (EZVIZ specifically — unofficial API, real account password, see its docstring)
    media_sampling.py        # shared "turn a video/image file into Frame objects" logic, used by folder/cloud/http/ezviz
```

Every source implementation honors **Global Constraint 3** ("no new video storage"): each writes to a single reusable per-camera temp frame path (overwritten every sample, matching Aegis's own `/tmp/aegis_detection/frame_{camera_id}.jpg` convention), and cloud/HTTP sources delete their temporary downloaded copy immediately after sampling it — nothing here ever accumulates a second video archive. None of the source implementations delete, move, or modify anything in the client's own storage (local folder or cloud bucket) — read-only with respect to their data, always.

## `cameras.json` — the multi-camera manifest

One entry per camera; `source_type` picks which `FrameSource` implementation handles it — **different cameras in the same deployment can use different source types** (matches a client who has some cameras on RTSP and older footage sitting in a folder). See `cameras.json.template` for the full field reference per source type.

```json
{
  "cameras": [
    { "camera_id": "lobby_cam_1", "source_type": "rtsp", "fps": 1,
      "rtsp": { "url": "rtsp://user:pass@192.168.1.50:554/stream1" } },
    { "camera_id": "concession_cam", "source_type": "local_folder", "fps": 1,
      "local_folder": { "folder": "C:\\CCTV\\concession" } },
    { "camera_id": "entrance_cam", "source_type": "s3", "fps": 0.5,
      "s3": { "bucket": "client-cctv-footage", "prefix": "entrance/", "region": "us-east-1" } }
  ]
}
```

## Two ways to run it

**As a standalone Aegis replacement** — `ingest.py` emits the exact `frame` JSONL protocol to stdout, so it can be piped directly into `yolo-detection-2026/scripts/detect.py`'s stdin exactly as Aegis would:
```bash
python scripts/ingest.py --cameras cameras.json | python ../yolo-detection-2026/scripts/detect.py
```

**As part of the all-in-one local pipeline** — `run_pipeline.py` (repo root `tools/`) wires ingestion → detection → coverage/pose → Redis in one command, for a client who doesn't want to hand-assemble a shell pipe. See `CCTV_INTEGRATION_SETUP.md` at the repo root.

## Local testing

```bash
python scripts/ingest.py --cameras tests/fixtures/cameras_local_folder_example.json
```

## Installation

```bash
./deploy.sh
```

Core dependencies (opencv-python, numpy, httpx) install always; cloud SDKs (boto3/azure-storage-blob/google-cloud-storage) install only for the source types actually configured in `cameras.json` — a client using only RTSP doesn't need any cloud package installed at all.
