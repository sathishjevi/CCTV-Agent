# Floorwatch — CCTV Integration Setup Guide

How to point Floorwatch at a specific client's actual CCTV footage, and how to test that connection before going live. Covers all five ingestion shapes Floorwatch supports — pick whichever matches this client, or mix them per-camera:

| # | Client's footage lives in... | `source_type` |
|---|---|---|
| 1 | A live camera/NVR exposing RTSP or ONVIF | `rtsp` |
| 2 | A local folder an existing NVR/DVR already writes recordings into | `local_folder` |
| 3 | Cloud storage — AWS S3, Azure Blob Storage, or Google Cloud Storage | `s3`, `azure_blob`, or `gcs` |
| 4 | A third-party surveillance platform's own API | `http_api` |
| 5 | EZVIZ specifically (no official partner API used — see §3, this one's different) | `ezviz` |

None of this requires touching the detection pipeline (`yolo-detection-2026`, `floorwatch-coverage`, `floorwatch-pose`) — they only ever consume the standard `frame` JSONL event (see `docs/detection-protocol.md`), regardless of which of the five shapes above produced it. That's the whole point of the `floorwatch-ingest` skill.

---

## 1. Prerequisites

- Python >=3.9, in the repo root (`C:\Product\CCTV Agent`)
- `config/deployment.env` and `config/secrets.env` already set up — if not, do that first (see `SETUP_AUTH_AND_CONFIG.md`)
- Install the ingest skill's core dependencies:

```powershell
cd "skills\detection\floorwatch-ingest"
pip install -r requirements.txt
```

- Install **only** the cloud SDK(s) this client actually needs (skip this if the client is RTSP or local-folder only):

```powershell
pip install boto3                  # AWS S3
pip install azure-storage-blob     # Azure Blob Storage
pip install google-cloud-storage   # Google Cloud Storage
```

`.\deploy.bat` (or `deploy.sh` on Linux/macOS) does both of the above for you automatically, by grepping your `cameras.json` for which source types are actually configured — run it once your manifest is filled in (step 3) instead of installing by hand.

---

## 2. Understand the manifest: `cameras.json`

Every camera — regardless of source type — gets one entry in `skills/detection/floorwatch-ingest/cameras.json`. **This file does not exist yet**; you create it from the template:

```powershell
Copy-Item "skills\detection\floorwatch-ingest\cameras.json.template" "skills\detection\floorwatch-ingest\cameras.json"
```

Open it and delete every example entry except the ones matching this client's cameras, then edit those to match. The template (`cameras.json.template`) has one fully-commented example per source type — treat it as the field reference.

A real client will often mix source types — e.g. newer cameras on RTSP, older footage still landing in a folder:

```json
{
  "cameras": [
    { "camera_id": "lobby_cam_1", "source_type": "rtsp", "fps": 1,
      "rtsp": { "url": "rtsp://user:pass@192.168.1.50:554/stream1" } },
    { "camera_id": "concession_cam", "source_type": "local_folder", "fps": 1,
      "local_folder": { "folder": "C:\\CCTV\\concession" } }
  ]
}
```

`cameras.json` itself is **not** committed to version control by default (add it to `.gitignore` if it contains anything site-specific you don't want tracked) — treat it like `config/deployment.env`: safe to keep locally, not something to hand around casually since it can reveal a client's internal folder paths, bucket names, or API endpoints.

### Secrets go in `secrets.env`, never in `cameras.json` directly

If a source needs a credential (Azure connection string, S3 static keys, a third-party API token), **do not paste the real value into `cameras.json`**. Instead:

1. Add the real value to `config/secrets.env` (copy from `config/secrets.env.template` if you haven't already — it now has a `CCTV source credentials` section)
2. Reference it in `cameras.json` as `${VAR_NAME}`

```json
"azure_blob": {
  "container": "cctv-footage",
  "connection_string": "${FLOORWATCH_AZURE_STORAGE_CONNECTION_STRING}"
}
```

`ingest.py` loads `config/deployment.env` + `config/secrets.env` at startup and substitutes every `${VAR_NAME}` in the manifest before connecting — this is the same secrets-out-of-config-files pattern used everywhere else in this repo (see `config/README.md`), applied to camera credentials too. If a referenced variable isn't set, you'll get a clear warning on stderr rather than a silent wrong connection.

---

## 3. Per-scenario setup notes

### Scenario 1 — RTSP camera/NVR

Get the RTSP URL from the client's camera/NVR admin panel (usually `rtsp://<ip>:554/<stream-path>`, sometimes with embedded credentials). Test it first with VLC or `ffprobe` before wiring it into Floorwatch — if VLC can't open it, Floorwatch can't either. Credentials embedded in the URL are automatically redacted from logs.

### Scenario 2 — Local folder

Point `folder` at wherever the client's NVR/DVR already writes recordings or snapshots. Floorwatch only **reads** — it never modifies, moves, or deletes anything there. It polls the folder every `poll_interval_seconds` (default 5s) for new video/image files it hasn't processed yet, tracked in a small state file (`state_file`) so a restart doesn't reprocess old footage.

### Scenario 3 — Cloud storage (S3 / Azure Blob / GCS)

Ask the client (or whoever manages their cloud account) for:
- Which bucket/container their CCTV footage/clips land in, and under what key prefix
- Read-only credentials scoped to that bucket/container specifically — **do not request account-wide credentials**; least-privilege read access to just the CCTV footage location is all Floorwatch ever needs

For AWS S3, prefer an IAM role or the default credential chain over static keys where the deployment environment supports it (leave `access_key_id`/`secret_access_key` out of `cameras.json` entirely in that case).

Floorwatch downloads each new object to a temporary local file, samples frames from it, then deletes the temporary copy immediately — it never builds a second copy of the client's footage archive (Global Constraint 3).

### Scenario 4 — Third-party surveillance provider's API

This is the one genuinely vendor-specific case — `http_api.py` is a **template**, not a finished integration, because every provider's API shape is different. You will need:

1. API documentation from the provider (or their support contact) covering: how to list new clips/recordings for a camera, and how to download one
2. The exact field names their "list clips" endpoint returns (`cameras.json.template`'s `id_field`/`download_url_field` map to whatever those are called)
3. Their auth scheme — usually a bearer token or API key header

If their API response shape doesn't match what `http_api.py` expects out of the box (a JSON array, or an object with a `results`/`items`/`clips`/`data` key), you'll need a small adapter — see the docstring in `skills/detection/floorwatch-ingest/scripts/sources/http_api.py` for exactly what to change and why. This is the one integration point in the whole pluggable-source design that can't be fully generic, because "any third-party CCTV vendor's API" isn't a fixed protocol the way RTSP or S3 are.

### Scenario 5 — EZVIZ (read this in full before using — it's not like the others)

EZVIZ cameras with cloud-only storage don't fit any of the above cleanly. Two integration paths exist, and this project uses the riskier one deliberately, with the tradeoff written down here for anyone revisiting this decision later:

**Path A — EZVIZ's official Open Platform (safer, not what's built).** EZVIZ has a real developer platform (register at `open.ys7.com`) that issues a scoped, revocable `AccessToken` via an OAuth-style consent flow — the customer authorizes your registered app on EZVIZ's own login page; you never see their raw password. This is the recommended pattern for any future vendor integration of this shape, but wasn't pursued here — building it needs the exact current API reference, which requires a registered developer account to access in full.

**Path B — credential replay against an unofficial API (what `source_type: "ezviz"` actually does).** `sources/ezviz.py` logs in with the customer's **real EZVIZ username and password** against EZVIZ's private, undocumented consumer-app API, via the community-maintained `pyezvizapi` package. This was an explicit, informed choice, not an oversight — made after being told directly that Path A wasn't the direction to take. Before using this for a new client, make sure whoever owns that decision understands:

- **Real Terms-of-Service risk.** Automating a private consumer API is not the same as using a documented partner API. EZVIZ can detect and restrict accounts doing this.
- **Fragility.** No compatibility promise exists for this API — it can change without notice on any EZVIZ app update. `pyezvizapi`'s own predecessor (`pyEzviz`) is already deprecated for exactly this reason.
- **A bigger secret than usual.** `FLOORWATCH_EZVIZ_PASSWORD` is the client's actual account password, not a scoped key — treat it with more care than any other credential in `config/secrets.env.template`, not the same care.
- **Two download paths, not guaranteed to cover everything.** Some EZVIZ cloud clips expose a direct HTTP URL (simple download); most instead need a native-SDK-stream decrypt sequence that `sources/ezviz.py` shells out to `pyezvizapi`'s own CLI for (never passing the password on the command line — a session token is exported and reused instead). Neither path has been exercised against a real EZVIZ account in this codebase — every request shape was verified against the installed library's actual source and its CLI's reference implementation, not guessed, but **test this against one real camera before relying on it for a client**, same as the "test with no real camera access yet" section below recommends for every other source type.

Setup: get the client's EZVIZ **username, password, device serial** (visible in the EZVIZ app's device settings), and **account region** (which regional API host their account uses — `apiieu.ezvizlife.com` for EU, `apiius.ezvizlife.com` for US, etc.; check the app or ask EZVIZ support if unsure). Set username/password in `config/secrets.env` (see `secrets.env.template`), everything else directly in `cameras.json` per `cameras.json.template`'s `ezviz` example.

```bash
pip install pyezvizapi
```

---

## 4. Test the connection before wiring up the full pipeline

Don't jump straight to the full detection pipeline — verify ingestion alone first, against just this client's `cameras.json`:

```powershell
cd "skills\detection\floorwatch-ingest"
python scripts\ingest.py --cameras cameras.json
```

Watch stdout. Within a few seconds (or up to `poll_interval_seconds` for folder/cloud sources) you should see:

```json
{"event": "ready", "cameras": ["lobby_cam_1", "concession_cam"], "count": 2}
{"event": "frame", "frame_id": 1, "camera_id": "lobby_cam_1", "timestamp": "...", "frame_path": "...", "width": 1920, "height": 1080}
```

- **A `frame` event per camera, repeating** — connection is working.
- **Only `ready`, no `frame` events** — the source connected but hasn't found new footage yet (normal for local_folder/cloud sources with no new files since the state file was last saved; add a fresh file to test, or delete the `state_file` to reprocess everything).
- **An `error` event** — read the `message`; combined with the `[floorwatch-ingest:...]` lines on stderr (not shown to Aegis, safe to read directly) this almost always says exactly what's wrong (bad URL, missing credential, bucket/container not found, folder doesn't exist).

Press `Ctrl+C` to stop. Do this once per camera type this client uses before moving on.

### Testing with no real camera access yet

If you don't have real credentials yet but want to prove the pipeline itself works, use `local_folder` pointed at any short test video/image files — this is exactly how the ingest skill's own test suite works (`skills/detection/floorwatch-ingest/tests/fixtures/`). Once real access arrives, switching a camera's `source_type` in `cameras.json` is the only change needed; nothing downstream changes.

---

## 5. Run the full pipeline

Once ingestion alone is confirmed working, run the all-in-one orchestrator, which wires ingestion → detection → coverage/pose → Redis in one command:

```powershell
python tools\run_pipeline.py --cameras "skills\detection\floorwatch-ingest\cameras.json"
```

This needs the dependencies of **every** stage installed in the same Python environment (`yolo-detection-2026`, `floorwatch-coverage`, `floorwatch-pose`, plus whatever `floorwatch-ingest` needs for this client's source types) and a calibrated zone file per camera under `skills/detection/floorwatch-coverage/zones/` (see that skill's calibration tool) — without calibration you'll see detections flowing but no `zone_covered`/`zone_gap` events.

Useful flags:
- `--skip-pose` — detection + coverage only, skip the motion/effort signal
- `--redis-url` — defaults to `FLOORWATCH_REDIS_URL` from `config/deployment.env` (`redis://localhost:6379/0`); if Redis isn't reachable, coverage/pose degrade to stdout-only rather than crashing, so you can still watch events locally without Redis running
- `--zones-dir` — defaults to `floorwatch-coverage`'s own `zones/` folder

Stop with `Ctrl+C` — it signals every stage to shut down cleanly rather than killing them abruptly.

### Running it Aegis's way instead

If Aegis/DeepCamera itself is doing the ingestion (i.e., this client's cameras are already registered in Aegis and you just want Floorwatch's detection skills attached the normal way), you don't need `ingest.py` or `run_pipeline.py` at all — install `yolo-detection-2026`, `floorwatch-coverage`, and `floorwatch-pose` as Aegis skills the standard way and Aegis handles frame delivery itself. `floorwatch-ingest` exists specifically for the case where a client's footage does **not** come through Aegis's own camera registration — see `REQUIREMENTS_STATUS.md` for why that gap existed.

---

## 6. Multiple clients / multiple sites

Each client gets its own `cameras.json` (and, if they use cloud/API sources, its own credential values in `secrets.env`, or better, a separate `secrets.env`-equivalent per deployment if you're running one Floorwatch instance per client rather than multi-tenant). Nothing in the ingest skill assumes a single client — `camera_id` values just need to be unique within one `cameras.json`.

---

## 7. Troubleshooting checklist

- **Nothing happens at all** — check `pip install -r requirements.txt` ran in `floorwatch-ingest`, and the right cloud SDK is installed if using S3/Azure/GCS.
- **`${VAR_NAME}` shows up literally in an error / connects with an empty credential** — the variable isn't set in `config/secrets.env` or the real environment; re-check spelling matches exactly (case-sensitive).
- **RTSP: connects then repeatedly drops** — normal on a flaky network; `rtsp.py` backs off and retries automatically (up to `max_backoff_seconds`), check stderr for the retry pattern rather than treating one drop as a failure.
- **Local folder: file never gets processed** — confirm the extension is one of the supported video (`.mp4`, `.avi`, `.mov`, `.mkv`) or image (`.jpg`, `.jpeg`, `.png`) types; anything else is silently skipped.
- **Cloud storage: "not found" / permission denied** — double check the credential is scoped to read the specific bucket/container/prefix, and that the prefix in `cameras.json` actually matches where footage lands (a trailing slash matters).
- **Third-party API: `ValueError` about response shape** — the provider's JSON doesn't match the `results`/`items`/`clips`/`data`/raw-array shapes `http_api.py` recognizes out of the box; you'll need the small vendor-specific adapter described in section 3.
- **EZVIZ: login fails** — double-check `region` matches the client's actual account region (wrong region is the most common cause), and confirm the username/password are correct by logging into the EZVIZ app directly first. A login that works in the app but fails here after a recent EZVIZ app update is the fragility risk flagged in section 3 — the unofficial API may have changed.
- **EZVIZ: clips list empty but the app shows recordings** — confirm `device_serial` and `channel` match exactly what the EZVIZ app shows for that camera, and that cloud storage (not just local SD card) is actually enabled for it.
- **EZVIZ: "Could not download cloud video ... via either path"** — both the simple and native-stream-fallback download attempts failed for that clip; check stderr for the specific subprocess error from the fallback path before assuming the clip is unrecoverable.
