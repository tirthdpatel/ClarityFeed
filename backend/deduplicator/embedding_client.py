"""
HuggingFace Inference API client for embedding generation.

Supports batch embedding, 503/429 retry with backoff, and optional
local sentence-transformers fallback when USE_LOCAL_EMBEDDING_FALLBACK is True.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import List, Optional

import httpx

from config.settings import settings

logger = logging.getLogger("news.deduplicator.embedding_client")

# Batch size cap for HuggingFace API
HF_BATCH_SIZE = 32


class EmbeddingClient:
    """Client for HuggingFace Inference API feature extraction."""

    def __init__(self) -> None:
        self._hf_url = (
            f"{settings.HF_API_BASE_URL}/pipeline/feature-extraction/"
            f"{settings.HF_EMBEDDING_MODEL}"
        )
        self._headers = {"Authorization": f"Bearer {settings.HF_API_TOKEN}"}

    async def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed a batch of texts. Returns 384-dim vectors or None for failures."""
        results: List[Optional[List[float]]] = []

        for i in range(0, len(texts), HF_BATCH_SIZE):
            batch = texts[i : i + HF_BATCH_SIZE]
            batch_results = await self._embed_single_batch(batch)
            results.extend(batch_results)

        return results

    async def _embed_single_batch(
        self,
        texts: List[str],
    ) -> List[Optional[List[float]]]:
        """Embed a single batch of up to 32 texts."""
        for attempt in range(settings.HF_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        self._hf_url,
                        headers=self._headers,
                        json={"inputs": texts},
                    )

                    if response.status_code == 200:
                        data = response.json()
                        if isinstance(data, list) and data and isinstance(data[0], list):
                            return [list(v) for v in data]
                        if isinstance(data, list) and data and isinstance(data[0], (int, float)):
                            return [list(data)] if len(texts) == 1 else [None] * len(texts)
                        return [None] * len(texts)

                    if response.status_code == 503:
                        body = response.json() if response.content else {}
                        est = body.get("estimated_time", settings.HF_RETRY_DELAY_SECONDS)
                        delay = settings.HF_RETRY_DELAY_SECONDS * (2**attempt)
                        logger.warning(
                            "HF 503 model loading, retry %d/%d in %.1fs (est: %s)",
                            attempt + 1,
                            settings.HF_MAX_RETRIES,
                            delay,
                            est,
                        )
                        await asyncio.sleep(delay)
                        continue

                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after else 60.0
                        logger.warning("HF 429 rate limit, waiting %.1fs then retrying", wait)
                        await asyncio.sleep(wait)
                        continue

            except Exception as e:
                logger.warning("Embedding request failed (attempt %d): %s", attempt + 1, e)
                if attempt < settings.HF_MAX_RETRIES:
                    delay = settings.HF_RETRY_DELAY_SECONDS * (2**attempt)
                    await asyncio.sleep(delay)
                    continue

        # All retries exhausted — try local fallback if enabled
        if settings.USE_LOCAL_EMBEDDING_FALLBACK:
            return await self._local_fallback(texts)

        logger.error("All embedding retries exhausted for batch of %d texts", len(texts))
        return [None] * len(texts)

    async def _local_fallback(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Use local sentence-transformers when API fails."""
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(settings.HF_EMBEDDING_MODEL)
            embeddings = model.encode(texts)
            if hasattr(embeddings, "tolist"):
                if embeddings.ndim == 1:
                    return [embeddings.tolist()]
                return [row.tolist() for row in embeddings]
            return [None] * len(texts)
        except ImportError:
            logger.error("sentence_transformers not installed; cannot use local fallback")
            return [None] * len(texts)
        except Exception as e:
            logger.error("Local embedding fallback failed: %s", e)
            return [None] * len(texts)
