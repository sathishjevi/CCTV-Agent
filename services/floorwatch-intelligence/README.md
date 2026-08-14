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
supervisor question ─> llm.py (any provider) <──tool calls──> mcp_server.py ──> tools.py ──GET──> floorwatch-rules-engine
                          │                                    │
                     grounded, cited                  historical_semantic_search
                       answer                          (reads vector_store directly)
```

## Run

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...          # required for /api/chat (default provider = anthropic)
# or: export FLOORWATCH_LLM_PROVIDER=openai; export FLOORWATCH_LLM_API_KEY=...; export FLOORWATCH_LLM_MODEL=gpt-4o
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
| `FLOORWATCH_LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `gemini` — see `app/llm.py`'s module docstring |
| `ANTHROPIC_API_KEY` | *(empty)* | Required for `/api/chat` when provider is `anthropic` (the default) |
| `FLOORWATCH_LLM_API_KEY` | *(empty)* | Required for `/api/chat` when provider is `openai` or `gemini` |
| `FLOORWATCH_LLM_MODEL` | *(empty)* | Required for `openai`/`gemini` — no safe default exists across vendors |
| `FLOORWATCH_LLM_BASE_URL` | *(empty)* | Only for provider `openai` — point at an OpenAI-compatible host (Kimi/Moonshot, DeepSeek, Groq/Together/Ollama-hosted Llama) instead of real ChatGPT |
| `FLOORWATCH_ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Legacy name for `FLOORWATCH_LLM_MODEL` when provider is `anthropic` |

**Any AI model/vendor works, not just Claude.** `FLOORWATCH_LLM_PROVIDER=openai` covers real ChatGPT *and* anything that speaks the same OpenAI-compatible wire format behind `FLOORWATCH_LLM_BASE_URL` — that includes Kimi/Moonshot, DeepSeek, and Llama hosted via Groq/Together/Fireworks/Ollama. `FLOORWATCH_LLM_PROVIDER=gemini` covers Google Gemini via its own API. Install the matching SDK (`pip install openai` or `pip install google-genai`, see `requirements.txt`) — only the SDK for the provider you actually use is required.

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
