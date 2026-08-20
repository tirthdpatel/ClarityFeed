"""
Internal trigger endpoint — replaces APScheduler entirely.

This FastAPI router provides a ``POST /internal/collect`` endpoint that
GitHub Actions calls every 15 minutes to trigger the RSS collection cycle.

WHY BACKGROUND TASK:
    The endpoint returns 200 immediately and runs the collection cycle as
    a FastAPI BackgroundTask. This is because GitHub Actions has a 60-second
    ``--max-time`` on the curl request, and a full collection cycle may take
    longer (especially with 10+ sources and rate limiting). Returning 200
    immediately signals successful triggering without waiting for completion.

RENDER COLD START BEHAVIOUR:
    When the Render service is sleeping and GitHub Actions sends the HTTP
    trigger, Render auto-wakes on the incoming request *before* the
    endpoint handler runs. So the first trigger after sleep always succeeds
    (after the cold-start delay). The GitHub Actions workflow handles the
    cold-start delay with up to 3 retries and 30-second waits.

HOW TO MANUALLY TRIGGER:
    curl -X POST https://your-render-url.onrender.com/internal/collect \\
         -H "X-Internal-Secret: your_secret_here"
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime  # PHASE 0 FIX: used at the /collect success path but never imported
from typing import Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.categorizer import Categorizer
from backend.collector.pipeline import CollectorPipeline
from backend.database.models import CleanedArticleRecord, RawArticleRecord
from backend.database.orm_models import CleanedArticle, RawArticle
from backend.deduplicator import Deduplicator, EmbeddingClient
from backend.fetcher import ArticleFetcher
from backend.cleaner import TextCleaner
from backend.database.session import get_db
from backend.summarizer import GroqLLMClient, Summarizer
from config.settings import settings
from backend.database.models import PipelineStatus

logger = logging.getLogger("news.api.internal")

router = APIRouter(prefix="/internal", tags=["internal"])


def _query_pending_articles(db: Session) -> List[RawArticleRecord]:
    """Return raw_articles with status PENDING."""
    try:
        rows = (
            db.query(RawArticle)
            .filter(RawArticle.status == PipelineStatus.PENDING)
            .all()
        )
        return [
            RawArticleRecord(
                id=r.id,
                source_id=r.source_id,
                title=r.title,
                url=r.url,
                published_at=r.published_at,
                raw_html=r.raw_html,
                summary_from_feed=r.summary_from_feed,
                status=r.status,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
    except Exception as e:
        logger.error("query_pending_articles failed: %s", e, exc_info=True)
        return []


def _query_cleaned_articles(
    db: Session,
    raw_ids: List[int],
) -> List[CleanedArticleRecord]:
    """Return cleaned_articles for the given raw_article_ids."""
    try:
        if not raw_ids:
            return []
        rows = (
            db.query(CleanedArticle)
            .filter(CleanedArticle.raw_article_id.in_(raw_ids))
            .all()
        )
        return [
            CleanedArticleRecord(
                id=r.id,
                raw_article_id=r.raw_article_id,
                clean_text=r.clean_text,
                word_count=r.word_count,
                language=r.language,
                created_at=r.created_at,
            )
            for r in rows
        ]
    except Exception as e:
        logger.error("query_cleaned_articles failed: %s", e, exc_info=True)
        return []


def _query_unique_cleaned_articles(
    db: Session,
    raw_ids: List[int],
) -> List[CleanedArticleRecord]:
    """Return cleaned_articles whose raw_article still has status PENDING (not DUPLICATE/FAILED)."""
    try:
        if not raw_ids:
            return []
        rows = (
            db.query(CleanedArticle)
            .join(RawArticle, CleanedArticle.raw_article_id == RawArticle.id)
            .filter(
                CleanedArticle.raw_article_id.in_(raw_ids),
                RawArticle.status == PipelineStatus.PENDING,
            )
            .all()
        )
        return [
            CleanedArticleRecord(
                id=r.id,
                raw_article_id=r.raw_article_id,
                clean_text=r.clean_text,
                word_count=r.word_count,
                language=r.language,
                created_at=r.created_at,
            )
            for r in rows
        ]
    except Exception as e:
        logger.error("query_unique_cleaned_articles failed: %s", e, exc_info=True)
        return []


def _query_summaries_map(
    db: Session,
    article_ids: List[int],
) -> Dict[int, object]:
    """Return a mapping of raw_article_id -> Summary (ORM or object with tldr)."""
    try:
        from backend.database.orm_models import Summary

        if not article_ids:
            return {}
        rows = (
            db.query(Summary)
            .filter(Summary.raw_article_id.in_(article_ids))
            .all()
        )
        return {r.raw_article_id: r for r in rows}
    except Exception as e:
        logger.error("query_summaries_map failed: %s", e, exc_info=True)
        return {}


async def run_collection_background(db: Session) -> None:
    """Run the full collection pipeline as a background task.

    Chains: RSS collection → fetch → clean → deduplicate → summarise → categorise.
    """
    start = time.monotonic()
    try:
        # Stage 1: RSS Collection (Phase 1)
        collection_result = await CollectorPipeline().run_collection_cycle(db)
        logger.info("Collection: %s", collection_result)

        pending = _query_pending_articles(db)
        if not pending:
            logger.info("No pending articles to process.")
            return

        logger.info("Processing %d pending articles through full pipeline", len(pending))

        # Stage 2: Article Fetching + Body Extraction
        fetch_result = await ArticleFetcher().fetch_and_extract_batch(db, pending)
        logger.info("Fetch: %s", fetch_result)

        raw_ids = [a.id for a in pending if a.id is not None]
        cleaned = _query_cleaned_articles(db, raw_ids)

        # Stage 3: Content Cleaning
        clean_result = TextCleaner().clean_batch(db, cleaned)
        logger.info("Clean: %s", clean_result)

        cleaned = _query_cleaned_articles(db, raw_ids)

        # Stage 4: Deduplication
        dedup_result = await Deduplicator(EmbeddingClient()).process_batch(db, cleaned)
        logger.info("Dedup: %s", dedup_result)

        unique_cleaned = _query_unique_cleaned_articles(db, raw_ids)

        # Stage 5: Summarization
        summarize_result = await Summarizer(GroqLLMClient()).summarize_batch(db, unique_cleaned)
        logger.info("Summarize: %s", summarize_result)

        unique_ids = [a.raw_article_id for a in unique_cleaned]
        summaries_map = _query_summaries_map(db, unique_ids)

        # Stage 6: Categorization + mark PROCESSED
        categorize_result = await Categorizer(GroqLLMClient()).categorize_batch(
            db, unique_cleaned, summaries_map
        )
        logger.info("Categorize: %s", categorize_result)

        elapsed = time.monotonic() - start
        logger.info("Full pipeline completed in %.1fs", elapsed)
    except Exception as e:
        logger.error("Pipeline orchestration failed: %s", e, exc_info=True)
    finally:
        db.close()


@router.post("/collect")
async def trigger_collection(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Trigger an RSS collection cycle.

    Protected by ``INTERNAL_SECRET`` header to prevent unauthorised triggers.
    Returns 200 immediately; the actual collection runs as a background task.
    """
    # Verify INTERNAL_SECRET header (constant-time comparison prevents timing attacks)
    provided_secret = request.headers.get("X-Internal-Secret", "")
    if not secrets.compare_digest(provided_secret, settings.INTERNAL_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")

    background_tasks.add_task(run_collection_background, db)

    return {
        "status": "collection_started",
        "timestamp": datetime.utcnow().isoformat(),
    }
