"""
Semantic deduplication engine.

Generates embeddings via HuggingFace API, stores them, and marks articles
as DUPLICATE when cosine similarity exceeds the threshold against the
comparison window.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import List

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import Session

from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import Embedding, RawArticle
from backend.deduplicator.embedding_client import EmbeddingClient
from config.settings import settings

logger = logging.getLogger("news.deduplicator.deduplicator")


@dataclass
class DeduplicationResult:
    """Result of a batch deduplication run."""

    unique: int
    duplicates: int
    failed_embedding: int
    duration_seconds: float


class Deduplicator:
    """Marks semantically duplicate articles using embedding similarity."""

    def __init__(self, embedding_client: EmbeddingClient) -> None:
        self._client = embedding_client

    async def process_batch(
        self,
        db: Session,
        articles: List[CleanedArticleRecord],
    ) -> DeduplicationResult:
        """Embed articles, store vectors, and mark duplicates."""
        start = time.monotonic()
        unique = 0
        duplicates = 0
        failed_embedding = 0

        if not articles:
            return DeduplicationResult(
                unique=0,
                duplicates=0,
                failed_embedding=0,
                duration_seconds=0.0,
            )

        texts = [a.clean_text for a in articles]
        embeddings = await self._client.embed_batch(texts)

        # Filter out failed embeddings
        valid_articles: List[CleanedArticleRecord] = []
        valid_embeddings: List[List[float]] = []
        for i, (article, emb) in enumerate(zip(articles, embeddings)):
            if emb is None:
                logger.error("Embedding failed for article %s", article.raw_article_id)
                self._mark_failed(db, article.raw_article_id)
                failed_embedding += 1
            else:
                valid_articles.append(article)
                valid_embeddings.append(emb)

        if not valid_articles:
            duration = time.monotonic() - start
            return DeduplicationResult(
                unique=0,
                duplicates=0,
                failed_embedding=failed_embedding,
                duration_seconds=round(duration, 2),
            )

        # Step 2 — Store new embeddings (idempotency: skip if already exists)
        inserted_ids: List[int] = []
        for article, vector in zip(valid_articles, valid_embeddings):
            try:
                existing = (
                    db.query(Embedding)
                    .filter(Embedding.raw_article_id == article.raw_article_id)
                    .first()
                )
                if existing is not None:
                    continue  # idempotency
                emb_row = Embedding(
                    raw_article_id=article.raw_article_id,
                    vector_json=json.dumps(vector),
                )
                db.add(emb_row)
                db.flush()
                inserted_ids.append(article.raw_article_id)
            except Exception as e:
                logger.error("Failed to store embedding for %s: %s", article.raw_article_id, e)
                self._mark_failed(db, article.raw_article_id)
                failed_embedding += 1

        db.commit()

        # Step 3 — Load comparison window (excluding all articles in this batch)
        exclude_ids = [a.raw_article_id for a in valid_articles]
        window_vectors, window_article_ids = self._load_comparison_window(db, exclude_ids)

        # Edge case: empty window (first-ever run)
        if len(window_vectors) == 0:
            duration = time.monotonic() - start
            return DeduplicationResult(
                unique=len(valid_articles),
                duplicates=0,
                failed_embedding=failed_embedding,
                duration_seconds=round(duration, 2),
            )

        # Step 4 — Compute cosine similarity
        new_matrix = np.array(valid_embeddings, dtype=np.float64)
        window_matrix = np.array(window_vectors, dtype=np.float64)
        sim = cosine_similarity(new_matrix, window_matrix)

        for i, article in enumerate(valid_articles):
            max_sim = float(np.max(sim[i]))
            if max_sim >= settings.SIMILARITY_THRESHOLD:
                # Find matched article ID (from window)
                match_idx = int(np.argmax(sim[i]))
                matched_id = window_article_ids[match_idx]
                logger.debug(
                    "Duplicate: article %s matches %s (sim=%.3f)",
                    article.raw_article_id,
                    matched_id,
                    max_sim,
                )
                self._mark_duplicate(db, article.raw_article_id)
                duplicates += 1
            else:
                unique += 1

        duration = time.monotonic() - start
        return DeduplicationResult(
            unique=unique,
            duplicates=duplicates,
            failed_embedding=failed_embedding,
            duration_seconds=round(duration, 2),
        )

    def _load_comparison_window(
        self,
        db: Session,
        exclude_raw_ids: List[int],
    ) -> tuple[List[List[float]], List[int]]:
        """Load the most recent N embeddings, excluding the given IDs."""
        from sqlalchemy import desc

        query = (
            db.query(Embedding)
            .filter(Embedding.raw_article_id.notin_(exclude_raw_ids))
            .order_by(desc(Embedding.created_at))
            .limit(settings.DUPLICATE_WINDOW_ARTICLES)
        )
        rows = query.all()
        vectors: List[List[float]] = []
        article_ids: List[int] = []
        for row in rows:
            if row.vector_json:
                try:
                    vec = json.loads(row.vector_json)
                    vectors.append(vec)
                    article_ids.append(row.raw_article_id)
                except (json.JSONDecodeError, TypeError):
                    continue
        return vectors, article_ids

    @staticmethod
    def _mark_failed(db: Session, raw_article_id: int) -> None:
        try:
            db.query(RawArticle).filter(RawArticle.id == raw_article_id).update(
                {"status": PipelineStatus.FAILED}
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def _mark_duplicate(db: Session, raw_article_id: int) -> None:
        try:
            db.query(RawArticle).filter(RawArticle.id == raw_article_id).update(
                {"status": PipelineStatus.DUPLICATE}
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
