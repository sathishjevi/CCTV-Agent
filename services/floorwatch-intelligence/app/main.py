"""
Floorwatch supervisor-intelligence service — FastAPI app, Phase 5.

Wires together: vector_store + embeddings + ingest (tasks 1-2), retrieval
(task 3), the MCP server (task 5), the LLM tool-use loop (tasks 4+6), and
the chat/incident-note HTTP surface (task 7).

Global Constraint 7: this entire service is read-only with respect to
Floorwatch's live system. Grep this file: there is no import of, or HTTP
call to, any mutating endpoint on floorwatch-rules-engine (no
POST /api/queue/..., no POST /api/tasks/.../complete, etc.) — the only
outbound calls to that service are the GETs inside tools.py. The only
writes this service performs are to its OWN data (vectors.sqlite3,
incident_notes.jsonl), which is this phase's explicitly-allowed scope
(see incident_notes.py's docstring).

Run:
  uvicorn main:app --port 8090
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402 (also inserts skills/lib onto sys.path — see config.py)
from incident_notes import IncidentNoteStore  # noqa: E402
from ingest import ingest_new_records  # noqa: E402
from llm import build_assistant  # noqa: E402
from mcp_server import build_mcp_server  # noqa: E402
from rate_limit import RateLimiter  # noqa: E402
from tools import ReadOnlyTools  # noqa: E402
from vector_store import build_vector_store  # noqa: E402
from embeddings import build_embedding_provider  # noqa: E402

from floorwatch_auth import make_auth_dependency  # noqa: E402

# Same auth used by floorwatch-rules-engine (shared secret) — a supervisor's
# login there works here too. See SECURITY_REVIEW.md finding AUTH-1.
require_auth = make_auth_dependency(config.AUTH_SECRET)
require_supervisor = make_auth_dependency(config.AUTH_SECRET, required_role="supervisor")


def log(msg: str):
    print(f"[intelligence] {msg}", file=sys.stderr, flush=True)


vector_store = build_vector_store(config)
embedding_provider = build_embedding_provider(config)
notes_store = IncidentNoteStore(config.INCIDENT_NOTES_PATH)
tools = ReadOnlyTools(config.RULES_ENGINE_BASE_URL, vector_store, embedding_provider,
                       service_token=config.SERVICE_TOKEN)
mcp_server = build_mcp_server(tools)
assistant = build_assistant(config, mcp_server)
chat_rate_limiter = RateLimiter(config.CHAT_RATE_LIMIT_PER_MINUTE, window_seconds=60.0)

INGEST_POLL_INTERVAL_SECONDS = 10


async def ingest_loop():
    while True:
        try:
            ingest_new_records(vector_store, embedding_provider, config.DIGEST_PATH, notes_store)
        except Exception as e:
            log(f"ingest loop error: {e}")
        await asyncio.sleep(INGEST_POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ingest_new_records(vector_store, embedding_provider, config.DIGEST_PATH, notes_store)
    task = asyncio.create_task(ingest_loop())
    log(f"Supervisor intelligence service started. Assistant available: {assistant is not None}")
    yield
    task.cancel()


app = FastAPI(
    title="Floorwatch Supervisor Intelligence",
    lifespan=lifespan,
    docs_url="/docs" if config.DOCS_ENABLED else None,
    redoc_url="/redoc" if config.DOCS_ENABLED else None,
    openapi_url="/openapi.json" if config.DOCS_ENABLED else None,
)
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ALLOWED_ORIGINS,
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "assistant_available": assistant is not None,
            "vector_store": type(vector_store).__name__, "indexed_records": len(vector_store.all_ids())}


@app.get("/api/tools")
async def list_tools(user=Depends(require_auth)):
    """Introspection endpoint — lists exactly what this service's chat
    layer can call. Used by guardrail tests to assert no write-shaped
    tool is ever present, and useful for a supervisor/auditor to see the
    read-only guarantee directly rather than take it on faith."""
    mcp_tools = await mcp_server.list_tools()
    return [
        {"name": t.name, "description": t.description,
         "read_only": t.annotations.read_only_hint if t.annotations else None}
        for t in mcp_tools
    ]


class ChatRequest(BaseModel):
    question: str


@app.post("/api/chat")
async def chat(body: ChatRequest, user=Depends(require_auth)):
    if not chat_rate_limiter.allow(user["sub"]):
        retry_after = chat_rate_limiter.retry_after_seconds(user["sub"])
        return JSONResponse(
            status_code=429,
            content={"error": f"Rate limit exceeded ({config.CHAT_RATE_LIMIT_PER_MINUTE}/min) — "
                               f"try again in {retry_after:.0f}s."},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    if assistant is None:
        return JSONResponse(status_code=503, content={
            "error": "Chat assistant unavailable — no ANTHROPIC_API_KEY configured for this service."})
    result = await assistant.answer(body.question)
    return result


class IncidentNoteRequest(BaseModel):
    text: str
    zone_id: str | None = None


@app.post("/api/incident-notes")
async def create_incident_note(body: IncidentNoteRequest, user=Depends(require_supervisor)):
    # `author` used to come from the request body — any caller could write a
    # note attributed to anyone (SECURITY_REVIEW.md, spoofable-author
    # finding). Now it's always the authenticated caller's own username.
    note = notes_store.add(body.text, zone_id=body.zone_id, author=user["sub"])
    ingest_new_records(vector_store, embedding_provider, config.DIGEST_PATH, notes_store)
    return note


@app.get("/api/incident-notes")
async def list_incident_notes(user=Depends(require_auth)):
    return notes_store.read_all()


CHAT_UI_PATH = config.REPO_ROOT / "dashboard" / "floorwatch_chat.html"


@app.get("/")
async def chat_ui():
    if CHAT_UI_PATH.exists():
        return FileResponse(str(CHAT_UI_PATH))
    return JSONResponse(status_code=404, content={"error": "chat UI not found"})
