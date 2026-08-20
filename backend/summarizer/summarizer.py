"""
Summarization engine — Groq-powered structured summaries.

Generates TLDR and bullet points with hallucination-control prompts.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import List

from sqlalchemy.orm import Session

from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import RawArticle, Summary
from backend.llm.base import LLMProvider
from config.settings import settings

logger = logging.getLogger("news.summarizer.summarizer")

SUMMARIZATION_SYSTEM_PROMPT = """You are a factual news summarization engine. Your task is to summarize news articles
with strict accuracy. You must follow these rules without exception:

1. Never add information not present in the provided article text.
2. Never invent names, statistics, dates, or quotes.
3. Use neutral, journalistic language. Do not express opinions.
4. If the article text is too short or unclear to summarize, return the JSON with
   tldr set to "Insufficient content" and bullets as an empty array.
5. Respond ONLY with a valid JSON object in this exact format:
   {"tldr": "One sentence summary.", "bullets": ["Point 1.", "Point 2.", "Point 3."]}
6. The tldr must be one sentence. Bullets must be 3 to 5 items. Each bullet is one sentence."""


@dataclass
class SummarizeBatchResult:
    """Result of a batch summarization run."""

    summarized: int
    failed: int
    skipped: int
    duration_seconds: float


class Summarizer:
    """Generates structured summaries via Groq LLM."""

    def __init__(self, llm_client: LLMProvider) -> None:
        self._llm = llm_client

    async def summarize_batch(
        self,
        db: Session,
        articles: List[CleanedArticleRecord],
    ) -> SummarizeBatchResult:
        """Summarize a batch of articles. Idempotent: skips already-summarized."""
        start = time.monotonic()
        summarized = 0
        failed = 0
        skipped = 0

        for article in articles:
            if article.raw_article_id is None:
                continue

            # Idempotency: skip if summary already exists
            existing = (
                db.query(Summary)
                .filter(Summary.raw_article_id == article.raw_article_id)
                .first()
            )
            if existing is not None:
                skipped += 1
                continue

            # Truncate: character-based approximation (Groq will handle token boundary)
            text = article.clean_text or ""
            max_chars = settings.MAX_ARTICLE_TOKENS * 4
            truncated = text[:max_chars] if len(text) > max_chars else text

            user_prompt = f"Summarize this news article:\n\n{truncated}"

            response = await self._llm.complete(
                SUMMARIZATION_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=settings.LLM_MAX_TOKENS,
                temperature=settings.LLM_TEMPERATURE,
                expect_json=True,
            )

            if response is None:
                self._mark_failed(db, article.raw_article_id)
                failed += 1
                continue

            try:
                parsed = json.loads(response)
                tldr = (parsed.get("tldr") or "").strip()
                bullets = parsed.get("bullets", [])
                if not isinstance(bullets, list):
                    bullets = []
                bullets = [str(b).strip() for b in bullets if b]
            except (json.JSONDecodeError, AttributeError) as e:
                logger.warning("Malformed JSON from summarizer for article %s: %s", article.raw_article_id, e)
                self._mark_failed(db, article.raw_article_id)
                failed += 1
                continue

            if not tldr or "Insufficient content" in tldr:
                logger.warning("Insufficient content or empty TLDR for article %s", article.raw_article_id)
                self._mark_failed(db, article.raw_article_id)
                failed += 1
                continue

            if len(bullets) < 1:
                logger.warning("Empty bullets for article %s", article.raw_article_id)
                self._mark_failed(db, article.raw_article_id)
                failed += 1
                continue

            try:
                summary = Summary(
                    raw_article_id=article.raw_article_id,
                    summary_text=tldr,
                    tldr=tldr,
                    bullet_points=json.dumps(bullets),
                    model_name=settings.GROQ_MODEL_PRIMARY,
                )
                db.add(summary)
                db.commit()
                summarized += 1
            except Exception as e:
                db.rollback()
                logger.error("Failed to insert summary for %s: %s", article.raw_article_id, e)
                self._mark_failed(db, article.raw_article_id)
                failed += 1

        duration = time.monotonic() - start
        return SummarizeBatchResult(
            summarized=summarized,
            failed=failed,
            skipped=skipped,
            duration_seconds=round(duration, 2),
        )

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
