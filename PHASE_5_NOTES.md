# Phase 5 Notes — Supervisor Intelligence Layer (RAG + Vector DB + MCP)

## What was built

A new standalone service, [`services/floorwatch-intelligence/`](services/floorwatch-intelligence/), read-only with respect to the live system built in Phases 1–4:

1. **Vector store** (`vector_store.py`) — `PgVectorStore` (real Postgres+pgvector, brief's intended path) and `SqliteVectorStore` (fallback actually used here — sqlite3 + numpy cosine similarity). `build_vector_store()` tries Postgres first, falls back automatically.
2. **Embedding pipeline** (`embeddings.py`, `ingest.py`) — `VoyageEmbeddingProvider` (real, Voyage AI — Anthropic's recommended embeddings partner since Claude has no embeddings endpoint) and `TfidfEmbeddingProvider` (fallback — scikit-learn `HashingVectorizer`, fixed-dimension, needs no corpus-wide fitting so it can embed new digest entries/notes incrementally as they arrive). `ingest_new_records()` is idempotent — safe to re-run on every poll.
3. **Supervisor incident notes** (`incident_notes.py`) — a small append-only store this phase owns and freely writes to; not a Global-Constraint-7 concern (writing a shift-log annotation has no effect on live coverage/effort tracking).
4. **Semantic retrieval** (`retrieval.py`) — `semantic_search()` with per-hit citation strings (`[shift digest, 2026-07-24, zone=concession, zone_gap]`) that the LLM layer is required to quote.
5. **MCP server** (`mcp_server.py`, `tools.py`) — exactly three tools (`get_current_zone_status`, `get_current_task_status`, `historical_semantic_search`), each declared `read_only_hint=True` at the MCP protocol level, all wrapping one shared `ReadOnlyTools` implementation that only ever issues HTTP GET.
6. **LLM integration** (`llm.py`) — real Anthropic SDK tool-use loop. Tool definitions and execution are derived directly from the MCP server's own `list_tools()`/`call_tool()` — no second, hand-rolled tool set that could drift from the MCP server's read-only guarantee.
7. **Chat UI** (`dashboard/floorwatch_chat.html`) + REST surface (`main.py`) — no button or endpoint anywhere in this service's own code can mutate zone state, task state, or the notification system; proven structurally, not just by omission (see below).
8. **Adversarial guardrail tests** — [`GUARDRAIL_TEST_RESULTS.md`](GUARDRAIL_TEST_RESULTS.md) at the repo root, 8 scenarios, all pass.

**67 tests pass** in this service alone (243 total across the whole Floorwatch codebase since Phase 1).

## Deviations from the brief — flagged explicitly

- **No Postgres/Timescale instance exists anywhere in this repo.** The brief's task 1 says "add pgvector to the existing Postgres/Timescale instance used for event storage" — but this codebase's actual event store through Phases 1–4 is Redis Streams (transient) plus a flat JSONL file (`shift_digest.jsonl`), not a database. There was never a Postgres instance to add pgvector *to*. Docker Desktop also still isn't runnable headlessly in this sandbox (same limitation since `PHASE_1_NOTES.md`), so standing one up wasn't an option either. `PgVectorStore` is real, schema-complete code for when a real instance is available; every test in this phase runs against the `SqliteVectorStore` fallback instead.
- **No Voyage API key, no Anthropic API key available to the application itself.** (Distinct from whatever access the coding agent building this has to Claude — this is about the *application's own* runtime credentials, which are unset here.) Embeddings fall back to a lexical hashing-trick vectorizer (real semantic similarity, not available); the LLM integration is real code exercised only against a mocked Anthropic client in tests. `/api/chat` correctly returns a 503 with a clear message rather than pretending to work — verified live in-browser, not just in tests.
- **The multi-step acceptance-criteria scenario** ("a supervisor can ask a question spanning both historical patterns and live status and get a grounded, cited answer") **is proven with a mocked model**, not the live Claude API — `test_multi_step_query_combines_history_and_live_status` in `test_llm.py` scripts a two-turn tool-use exchange (retrieval + live-status call) and asserts the final answer cites both. The tool-use *mechanism* is real; the *quality* of a real Claude response to a real ambiguous question hasn't been observed.
- **Guardrail tests simulate a "successfully manipulated" model** rather than testing whether the real Claude API refuses adversarial prompts gracefully — because there's no API key to send them to. This is arguably the more important guarantee to have (safety that doesn't depend on the model's judgment), but it does mean the *UX* of refusal (does Claude politely decline vs. this test suite's proxy of "the attempt raises an exception") is unverified.

## What's needed from you before this is real

1. **A real Postgres instance with pgvector** (or explicit sign-off that sqlite is fine at this pilot's scale — plausible for a single cineplex, worth a deliberate decision either way).
2. **A Voyage AI API key** (or another embedding provider decision) to get real semantic search instead of lexical hashing.
3. **An `ANTHROPIC_API_KEY` for this application** — nothing in Phase 5 can actually answer a supervisor's question without one.
4. **Re-run the guardrail scenarios in `GUARDRAIL_TEST_RESULTS.md` informally against the live API once a key exists** — not to re-verify safety (the structural guarantee doesn't depend on it), but to sanity-check that real Claude's refusals read as clear and helpful to an actual supervisor.
5. Everything still outstanding from Phases 1–4 (real camera access, real Redis/Postgres validation, real pose model, real Twilio/FCM credentials, dashboard `renderEmpPreview` discrepancy) remains open.

## Closing the loop across all five phases

With this, every phase in the original build brief has a working
implementation in this repo, verified as far as this sandbox's
constraints allow (no Docker, no internet to arbitrary hosts, no real
credentials for any third-party service), with every substitution
flagged rather than silently assumed. The recurring pattern across all
five phases — real integration code paired with an honest, tested
fallback, and a `PHASE_N_NOTES.md` naming exactly what's unverified — is
deliberate and consistent, so picking this up for a real pilot means
working through each phase's "what's needed from you" list rather than
re-deriving what was actually built versus stubbed.
