# Floorwatch Pipeline (Railway service)

Containerized `tools/run_pipeline.py` — ingestion → detection → coverage/pose → Redis, as one always-on Railway service instead of a manually-launched local script. This is the third Floorwatch Railway service, alongside `floorwatch-rules-engine` and `floorwatch-intelligence`.

**Only cloud-reachable CCTV sources work here** — `s3`, `azure_blob`, `gcs`, `http_api`, `ezviz`. This container has no path to a client's local camera network or local filesystem, so `rtsp` and `local_folder` cameras need a different machine that actually has that access (on-prem, or a local box at the client's site) running `ingest.py`/`run_pipeline.py` directly, publishing to the same Redis instance this service uses. You can mix both in one deployment — this service just won't be the one handling the on-prem cameras.

## Deploy settings (same pattern as the other two services — see `RAILWAY_DEPLOYMENT.md`)

- **Root Directory**: blank (repo root)
- **Builder**: `Dockerfile`
- **Dockerfile Path**: `services/floorwatch-pipeline/Dockerfile`
- **Config File Path**: `services/floorwatch-pipeline/railway.json`
- **Custom Start Command**: blank (the Dockerfile's `CMD` runs `entrypoint.sh`)

## Environment variables

`cameras.json` is site-specific and gitignored — never baked into the image. Provide it as a Railway variable:

| Variable | Required? | Notes |
|---|---|---|
| `FLOORWATCH_CAMERAS_JSON` | **Yes** (unless using a mounted volume instead) | The raw JSON content of your `cameras.json` (see `cameras.json.template`), pasted as one variable's value. `entrypoint.sh` writes it to disk at container start and validates it's real JSON before starting the pipeline. |
| `FLOORWATCH_REDIS_URL` | Yes | Same Redis instance `floorwatch-rules-engine` uses — this is how the two services connect. |
| `FLOORWATCH_EZVIZ_USERNAME` / `FLOORWATCH_EZVIZ_PASSWORD` | If any camera uses `source_type: "ezviz"` | See `config/secrets.env.template` and `sources/ezviz.py`'s module docstring before using this one — real account password, not a scoped key. |
| `FLOORWATCH_AWS_ACCESS_KEY_ID` / `FLOORWATCH_AWS_SECRET_ACCESS_KEY` | If any camera uses `source_type: "s3"` and isn't using an IAM role | |
| `FLOORWATCH_AZURE_STORAGE_CONNECTION_STRING` | If any camera uses `source_type: "azure_blob"` | Referenced from `cameras.json` as `${FLOORWATCH_AZURE_STORAGE_CONNECTION_STRING}` |
| `FLOORWATCH_GCS_CREDENTIALS_PATH` | If any camera uses `source_type: "gcs"` and isn't using Application Default Credentials | This needs the actual service-account JSON file present in the container too — not supported by this Dockerfile as written; use a Railway volume, or omit this to use ADC instead if the deployment environment supports it |
| `FLOORWATCH_ZONES_DIR` | Optional | Path to calibrated zone files. Without a mounted volume, this resets on every redeploy — same ephemeral-storage caveat as `RAILWAY_DEPLOYMENT.md` §3 |
| `FLOORWATCH_MODEL_SIZE` | Optional | `nano` (default) / `small` / `medium` / `large` |
| `FLOORWATCH_DETECT_CONFIDENCE` | Optional | Default `0.8` |
| `FLOORWATCH_DETECT_DEVICE` | Optional | Default `auto` — resolves to `cpu` automatically on Railway (no GPU) |
| `FLOORWATCH_DETECT_FPS` / `FLOORWATCH_POSE_FPS` | Optional | Defaults `5` / `1` |
| `FLOORWATCH_SKIP_POSE` | Optional | `true` to run detection+coverage only |
| `FLOORWATCH_REDIS_STREAM` / `FLOORWATCH_REDIS_MOTION_STREAM` | Optional | Defaults `floorwatch:events` / `floorwatch:motion` |

## Known limitations, stated plainly

- **Not build-tested against a real Docker daemon** in the environment this was built in (see `RAILWAY_DEPLOYMENT.md`'s own note on this) — verify with a real Railway deploy before relying on it for a client.
- **No persistence without a volume**: per-camera processed-item state (which clips have already been ingested) resets on every redeploy without a mounted Railway volume, meaning a redeploy could re-process recent clips. Not harmful (idempotent downstream), just wasted work.
- **`FLOORWATCH_GCS_CREDENTIALS_PATH`** needs an actual file present in the container, which this Dockerfile doesn't provision — use ADC instead, or add a volume, if a client needs GCS specifically.
- **Image size**: this bundles `ultralytics`/`torch` (detection), `mediapipe` (pose), and four cloud SDKs unconditionally, since which ones a given `cameras.json` needs isn't known at Docker build time (only at runtime, once the real manifest is provided). Expect a large image (multiple GB) — this is inherent to bundling the full detection stack in one container, not a regression.
