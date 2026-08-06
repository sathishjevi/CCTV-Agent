"""
Embedding provider abstraction — build brief Phase 5 task 2: "Build an
embedding pipeline that embeds shift digests and any supervisor-written
incident notes as they're created, storing vectors alongside a reference
to the source record."

Real path: `VoyageEmbeddingProvider`, using Voyage AI's SDK — Anthropic
recommends Voyage for embeddings since Claude itself has no embeddings
endpoint. Requires `VOYAGE_API_KEY`, which is not available in this dev
sandbox (same "real integration code, no credentials to exercise it live"
pattern as `notifications.py`'s Twilio/FCM integrations in Phase 4).

Fallback path used for all development/testing here:
`TfidfEmbeddingProvider`, built on scikit-learn's `HashingVectorizer` — a
stateless, fixed-dimension feature hash (not a fitted TF-IDF in the
strict sense; named for familiarity) that needs no corpus-wide fitting
step, so new documents can be embedded one at a time as they arrive
(shift digests continuously, incident notes ad hoc) without re-fitting
over the whole corpus. It captures lexical/keyword overlap, not deep
semantic meaning — enough to prove the retrieval/RAG pipeline works
end-to-end, not a substitute for a real embedding model. Flagged in
PHASE_5_NOTES.md.
"""

import sys
from typing import List


def log(msg: str):
    print(f"[embeddings] {msg}", file=sys.stderr, flush=True)


class TfidfEmbeddingProvider:
    """Deterministic, dependency-light fallback. Fixed-dimension hashed
    bag-of-words vector, L2-normalized so cosine similarity behaves
    sensibly regardless of document length."""

    def __init__(self, dim: int = 512):
        from sklearn.feature_extraction.text import HashingVectorizer
        self.dim = dim
        self._vectorizer = HashingVectorizer(n_features=dim, norm="l2", alternate_sign=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        matrix = self._vectorizer.transform(texts)
        return matrix.toarray().tolist()

    def embed_one(self, text: str) -> List[float]:
        return self.embed([text])[0]


class VoyageEmbeddingProvider:
    """Real embedding provider (voyageai SDK) — the brief's intended path.
    Not exercised against the live Voyage API in this sandbox; see module
    docstring."""

    def __init__(self, api_key: str, model: str = "voyage-3"):
        import voyageai
        self._client = voyageai.Client(api_key=api_key)
        self.model = model

    def embed(self, texts: List[str]) -> List[List[float]]:
        result = self._client.embed(texts, model=self.model, input_type="document")
        return result.embeddings

    def embed_one(self, text: str) -> List[float]:
        return self.embed([text])[0]


def build_embedding_provider(config):
    if config.EMBEDDING_PROVIDER == "voyage" and config.VOYAGE_API_KEY:
        try:
            provider = VoyageEmbeddingProvider(config.VOYAGE_API_KEY, config.VOYAGE_MODEL)
            log(f"Using VoyageEmbeddingProvider (model={config.VOYAGE_MODEL})")
            return provider
        except Exception as e:
            log(f"WARNING: could not initialize Voyage embeddings ({e}) — "
                f"falling back to TfidfEmbeddingProvider")
    else:
        log("Using TfidfEmbeddingProvider (dev/pilot fallback — no VOYAGE_API_KEY configured).")
    return TfidfEmbeddingProvider(dim=config.EMBEDDING_DIM)
