"""
Categorization engine — Groq-powered zero-shot classification.

Assigns one of seven fixed categories to each article using the TLDR and title.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import Category, RawArticle, Summary
from backend.llm.base import LLMProvider
from config.settings import settings

logger = logging.getLogger("news.categorizer.categorizer")

CATEGORIZATION_SYSTEM_PROMPT = """You are a news article classifier. Your only task is to assign exactly one category
from this fixed list to the given article title and summary:

Politics, Technology, Business, Science, World, Health, Sports

Rules:
1. You must respond ONLY with a JSON object in this exact format:
   {"category": "Technology", "confidence": 0.92}
2. The category field must be exactly one of the seven labels listed above.
3. The confidence field must be a float between 0.0 and 1.0.
4. Do not add any explanation, preamble, or text outside the JSON.
5. If the article does not clearly fit any category, use "World" as the default."""


@dataclass
class CategorizeBatchResult:
    """Result of a batch categorization run."""

    categorized: int
    failed: int
    skipped: int
    low_confidence: int
    duration_seconds: float


class Categorizer:
    """Assigns category labels via Groq LLM."""

    def __init__(self, llm_client: LLMProvider) -> None:
        self._llm = llm_client
        self._valid_categories = list(settings.VALID_CATEGORIES)

    async def categorize_batch(
        self,
        db: Session,
        articles: List[CleanedArticleRecord],
        summaries: Dict[int, Any],
    ) -> CategorizeBatchResult:
        """Categorize a batch of articles. Idempotent: skips already categorized."""
        start = time.monotonic()
        categorized = 0
        failed = 0
        skipped = 0
        low_confidence = 0

        for article in articles:
            if article.raw_article_id is None:
                continue

            # Idempotency: skip if category already exists
            existing = (
                db.query(Category)
                .filter(Category.raw_article_id == article.raw_article_id)
                .first()
            )
            if existing is not None:
                skipped += 1
                continue

            summary = summaries.get(article.raw_article_id)
            if summary is None:
                continue  # No summary (article failed in summarization)

            tldr = getattr(summary, "tldr", None) or (
                summary.get("tldr") if isinstance(summary, dict) else None
            )
            if not tldr:
                tldr = getattr(summary, "summary_text", None) or ""

            # Fetch raw article for title
            raw = db.query(RawArticle).filter(RawArticle.id == article.raw_article_id).first()
            title = raw.title if raw else ""

            user_prompt = f"Article title: {title}\nArticle summary: {tldr}"

            response = await self._llm.complete(
                CATEGORIZATION_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=settings.LLM_MAX_TOKENS,
                temperature=settings.LLM_TEMPERATURE,
                expect_json=True,
            )

            if response is None:
                failed += 1
                continue

            try:
                parsed = json.loads(response)
                category = (parsed.get("category") or "").strip()
                confidence = float(parsed.get("confidence", 0.0))
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.warning("Malformed categorization JSON for article %s: %s", article.raw_article_id, e)
                failed += 1
                continue

            # PHASE 0 FIX: this branch used to read
            #     category = "World"; confidence = 0.0
            # which was dead code — setting confidence to 0.0 always tripped
            # the MIN_CONFIDENCE_SCORE check below, immediately overwriting
            # "World" with "Uncategorized". The World fallback could never
            # actually take effect.
            #
            # "Uncategorized" is also the correct outcome on the merits: an
            # unparseable label means we do not know the category, and World
            # is a real user-facing feed. Filing garbage there pollutes a
            # category readers actually browse. Keeping it unclassified sends
            # it to the review queue instead (ARCHITECTURE_V2 §14).
            if category not in self._valid_categories:
                logger.warning(
                    "Invalid category '%s' from LLM for article %s — marking Uncategorized",
                    category,
                    article.raw_article_id,
                )
                category = "Uncategorized"
                confidence = 0.0
                low_confidence += 1
            elif confidence < settings.MIN_CONFIDENCE_SCORE:
                category = "Uncategorized"
                low_confidence += 1

            try:
                cat_row = Category(
                    raw_article_id=article.raw_article_id,
                    primary_category=category,
                    confidence=confidence,
                    model_name=settings.GROQ_MODEL_PRIMARY,
                )
                db.add(cat_row)
                db.flush()

                # Final stage: mark as PROCESSED
                db.query(RawArticle).filter(RawArticle.id == article.raw_article_id).update(
                    {"status": PipelineStatus.PROCESSED}
                )
                db.commit()
                categorized += 1
            except Exception as e:
                db.rollback()
                logger.error("Failed to insert category for %s: %s", article.raw_article_id, e)
                failed += 1

        duration = time.monotonic() - start
        return CategorizeBatchResult(
            categorized=categorized,
            failed=failed,
            skipped=skipped,
            low_confidence=low_confidence,
            duration_seconds=round(duration, 2),
        )
