# DeepCamera Source Code

This directory contains the original DeepCamera processing pipeline, built over 4+ years of development. These modules handle everything from camera capture to AI-powered detection and re-identification.

> **🆕 Looking for the new skill system?** See [`skills/`](../skills/) for the pluggable AI skill architecture.

## Module Map

| Module | Purpose | Status |
|--------|---------|--------|
| **`yolov7_person_detector/`** | Real-time person detection using YOLOv7 | ✅ Production |
| **`yolov7_reid/`** | Person re-identification — tracks individuals across cameras | ✅ Production |
| **`face_detection/`** | Face detection & recognition pipeline (InsightFace) | ✅ Production |
| **`embedding/`** | Feature extraction for visual similarity search | ✅ Production |
| **`camera/`** | Camera capture utilities, RTSP/USB handlers | ✅ Production |
| **`home-assistant-py/`** | Home Assistant integration (Python) | ✅ Production |
| **`home-assistant-nodejs/`** | Home Assistant integration (Node.js) | ✅ Production |
| **`fall_detection/`** | Fall detection for elderly care | 🧪 Experimental |
| **`laptop_monitor/`** | Screen activity monitoring | 🧪 Experimental |
| **`yolov2_parking/`** | Parking space detection (YOLOv2) | 📦 Legacy |
| **`yolov3_parking/`** | Parking space detection (YOLOv3) | 📦 Legacy |
| **`label-studio/`** | Label Studio annotation config | 📦 Legacy |
| **`milvus/`** | Milvus vector database config | 📦 Legacy |
| **`minio/`** | MinIO object storage config | 📦 Legacy |
| **`flower/`** | Celery task monitor | 📦 Legacy |
| **`monitor/`** | System monitoring utilities | 📦 Legacy |
| **`build/`** | Build scripts for ARM/x86 platforms | 🔧 Build |
| **`env/`** | Environment configuration | 🔧 Build |
| **`unit_test/`** | Test suite | 🔧 Test |

## Architecture

```
Camera Feed → capture (camera/) → detection (yolov7/) → embedding (embedding/)
                                       ↓                        ↓
                                  face_detection/         yolov7_reid/
                                       ↓                        ↓
                                  Face Recognition      Person Re-ID
                                       └────────┬───────────────┘
                                            Notification
                                         (Home Assistant)
```

## Evolution

These modules are being wrapped as **skills** for the [SharpAI Aegis](https://github.com/SharpAI/Aegis-AI) desktop app. The skill architecture provides a standard interface (`SKILL.md`) so each capability can be independently installed, configured, and updated.

| Legacy Module | New Skill |
|--------------|-----------|
| `yolov7_person_detector/` | [`skills/detection/yolo-detection-2026/`](../skills/detection/yolo-detection-2026/) |
| `yolov7_reid/` | `skills/detection/person-recognition/` (planned) |
| `face_detection/` + `embedding/` | `skills/detection/face-recognition/` (planned) |
| `home-assistant-py/` | `skills/integrations/homeassistant-bridge/` (planned) |

See [`docs/skill-development.md`](../docs/skill-development.md) for how to create new skills.
