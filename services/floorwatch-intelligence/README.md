# Floorwatch Supervisor Intelligence Service (Phase 5)

Read-only natural-language query layer over Floorwatch's historical
events and live status. Global Constraint 7: this service can never
mutate zone state, task state, or the notification system — see
`GUARDRAIL_TEST_RESULTS.md` at the repo root for adversarial proof of
that, verified at the API layer.

## Architecture

```
shift_digest.jsonl ─┐
incident_notes.jsonl ┴─> ingest.py ─> embeddings.py ─> vector_store.py
                                                              │
supervisor question ─> llm.py (Claude) <──tool calls──> mcp_server.py ──> tools.py ──GET──> floorwatch-rules-engine
                          │                                    │
                     grounded, cited                  historical_semantic_search
                       answer                          (reads vector_store directly)
```

## Run

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...          # required for /api/chat to work at all
export FLOORWATCH_RULES_ENGINE_URL=http://localhost:8080
cd app
uvicorn main:app --port 8090
```

**Security note**: no authentication exists on any endpoint of this
service either (see `SECURITY_REVIEW.md` at the repo root) — the
read-only guarantee is about what the chat/MCP layer can *do*, not about
who can *reach* it. Anyone who can hit this port can read all history
and write incident notes. Keep this bound to `127.0.0.1` (uvicorn's
default, used above) until auth is added.

Open `http://localhost:8090/` for the chat UI (also at
`dashboard/floorwatch_chat.html`).

## Config — the Postgres/Voyage/Anthropic deviations

**No real Postgres+pgvector, Voyage API key, or Anthropic API key is
available in this dev sandbox.** Every one of those integrations is real,
production code — none of it has been exercised against the live
service it targets. See `PHASE_5_NOTES.md` for the full writeup. In
short:

| Component | Real path (brief's intent) | Fallback actually used here |
|---|---|---|
| Vector store | `PgVectorStore` (Postgres + `pgvector` extension) | `SqliteVectorStore` (stdlib sqlite3 + numpy cosine similarity) |
| Embeddings | `VoyageEmbeddingProvider` (Voyage AI, Anthropic's recommended partner) | `TfidfEmbeddingProvider` (scikit-learn `HashingVectorizer`, no API key needed) |
| LLM | Claude via the real Anthropic SDK | Same code — just untestable live without a key; tests mock the client |

| Var | Default | Meaning |
|---|---|---|
| `FLOORWATCH_POSTGRES_DSN` | *(empty)* | If set, tries `PgVectorStore` first |
| `FLOORWATCH_SQLITE_VECTOR_DB_PATH` | `vectors.sqlite3` | Fallback vector store location |
| `FLOORWATCH_EMBEDDING_PROVIDER` | `tfidf` | `tfidf` \| `voyage` |
| `VOYAGE_API_KEY` | *(empty)* | Required for the real embedding path |
| `FLOORWATCH_DIGEST_PATH` | `../floorwatch-rules-engine/shift_digest.jsonl` | Source of shift-digest events to embed |
| `FLOORWATCH_INCIDENT_NOTES_PATH` | `incident_notes.jsonl` | This service's own supervisor-note store |
| `FLOORWATCH_RULES_ENGINE_URL` | `http://localhost:8080` | Base URL for the two read-only status GETs |
| `ANTHROPIC_API_KEY` | *(empty)* | Required for `/api/chat` — returns 503 with a clear message if unset |
| `FLOORWATCH_ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Model used for the chat assistant |

## Endpoints

- `GET /` — chat UI
- `GET /healthz` — liveness + which vector store/embedding fallback is active
- `GET /api/tools` — introspection: exactly which (read-only) tools the chat layer can call
- `POST /api/chat` `{question}` — grounded, cited answer (503 if no `ANTHROPIC_API_KEY`)
- `POST /api/incident-notes` `{text, zone_id?, author?}` — add and immediately index a supervisor note (this service's own writable scope — not a live-system mutation, see `incident_notes.py`)
- `GET /api/incident-notes` — list notes

## Tests

```bash
pip install -r requirements.txt -r tests/requirements-dev.txt
python -m pytest tests/ -v
```

67 tests, no external services required (sqlite fallback + mocked
Anthropic/Twilio-style clients throughout). Includes
`tests/test_guardrails.py` — see `GUARDRAIL_TEST_RESULTS.md`.
