"""
Semantic retrieval — build brief Phase 5 task 3: "Build retrieval logic:
semantic search over embedded digests/notes (e.g. 'find similar past
coverage gaps in this zone')."

Thin layer over vector_store + embeddings: embeds the query with the same
provider used at ingest time, searches, and normalizes each hit into a
citable shape (`format_citation`) — this is what the LLM layer (llm.py)
grounds its answers in and cites back to the supervisor.
"""

import json
from typing import Optional


def semantic_search(vector_store, embedding_provider, query: str, top_k: int = 5,
                     zone_id: Optional[str] = None) -> list:
    """Returns a list of {record_id, source_type, text, score, citation, metadata},
    ranked by similarity. If zone_id is given, results are filtered to that
    zone post-search (the sqlite/pgvector backends don't take a filter
    argument — fine at pilot scale to over-fetch and filter in Python)."""
    query_vector = embedding_provider.embed_one(query)
    raw_results = vector_store.search(query_vector, top_k=top_k * 3 if zone_id else top_k)

    results = []
    for r in raw_results:
        try:
            source_ref = json.loads(r["source_ref"])
        except (json.JSONDecodeError, TypeError):
            source_ref = {}

        if zone_id and source_ref.get("zone_id") != zone_id:
            continue

        results.append({
            "record_id": r["record_id"],
            "source_type": r["source_type"],
            "text": r["text"],
            "score": r["score"],
            "citation": format_citation(r["source_type"], source_ref),
            "metadata": r.get("metadata", {}),
        })
        if len(results) >= top_k:
            break

    return results


def format_citation(source_type: str, source_ref: dict) -> str:
    date = source_ref.get("date", "unknown date")
    zone = source_ref.get("zone_id", "unknown zone")
    if source_type == "shift_digest":
        event_type = source_ref.get("event_type", "event")
        return f"[shift digest, {date}, zone={zone}, {event_type}]"
    if source_type == "incident_note":
        return f"[supervisor note, {date}, zone={zone}]"
    return f"[{source_type}, {date}, zone={zone}]"
