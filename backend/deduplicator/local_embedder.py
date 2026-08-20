"""Local sentence-transformers embeddings — replaces the HuggingFace API.

ARCHITECTURE_V2 §2 and §A5.

The old design called the HuggingFace Inference API because the Render free
tier's 512 MB could not hold a torch model. Moving ingestion into GitHub
Actions removes that constraint entirely — the runner has 7 GB — and with it
three separate problems:

  * the HF API quota, and 503s while the model cold-starts on their side
  * a network round trip per batch, from a runner to a third-party service
  * the JSON-text embedding storage that the memory ceiling forced

all-MiniLM-L6-v2 is ~90 MB and produces 384-dimensional vectors. On a runner
it embeds a few hundred short texts per second on CPU, which is far more than
an hourly ingest needs.

The model is loaded lazily and cached, because importing sentence-transformers
pulls in torch and costs several seconds — a cost the API server should never
pay, since it never embeds anything.
"""
from __future__ import annotations

import logging
from typing import Iterable, Sequence

logger = logging.getLogger("news.deduplicator.local_embedder")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
DEFAULT_BATCH_SIZE = 64

_model = None
_model_name: str | None = None


def _load(model_name: str):
    """Load and cache the model. Import is deliberately function-local."""
    global _model, _model_name
    if _model is not None and _model_name == model_name:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "sentence-transformers is not installed. It is required for "
            "ingestion (GitHub Actions) but deliberately NOT a dependency of "
            "the read API, which never embeds anything. "
            "Install with: pip install -r requirements-ingest.txt"
        ) from exc

    logger.info("Loading embedding model %s (first call only)", model_name)
    _model = SentenceTransformer(model_name)
    _model_name = model_name
    return _model


class LocalEmbedder:
    """Embeds text locally. Same shape as the old EmbeddingClient so callers
    do not care which one they were handed."""

    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self.model_name = model_name
        self.batch_size = batch_size

    def embed_batch(self, texts: Sequence[str]) -> list[list[float] | None]:
        """Return one 384-dim vector per input, or None where input was empty.

        Never raises for a single bad input: a failure here must degrade
        deduplication, not stop the ingestion run.
        """
        if not texts:
            return []

        model = _load(self.model_name)

        # Preserve positions — the caller maps results back to article ids.
        indexed = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
        results: list[list[float] | None] = [None] * len(texts)
        if not indexed:
            return results

        try:
            vectors = model.encode(
                [t for _, t in indexed],
                batch_size=self.batch_size,
                normalize_embeddings=True,  # cosine similarity becomes a dot product
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception:
            logger.error("Local embedding failed for %d texts", len(indexed), exc_info=True)
            return results

        for (orig_index, _), vec in zip(indexed, vectors):
            results[orig_index] = [float(x) for x in vec]
        return results

    def embed_one(self, text: str) -> list[float] | None:
        return self.embed_batch([text])[0]


def format_for_pgvector(vector: Sequence[float] | None) -> str | None:
    """Render a vector in pgvector's literal form: '[0.1,0.2,...]'.

    psycopg2 has no adapter for halfvec, so the value is passed as text and
    cast in SQL. Doing the formatting here keeps that detail in one place.
    """
    if vector is None:
        return None
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
