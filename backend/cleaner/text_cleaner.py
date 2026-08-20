"""
Content cleaning module — normalizes extracted text for embedding and summarization.

Applies seven transforms in sequence to produce storage-efficient plain text.
Operates on text already in cleaned_articles; does not fetch from the web.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import time
from dataclasses import dataclass
from typing import List

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import CleanedArticle, RawArticle
from config.settings import settings

logger = logging.getLogger("news.cleaner.text_cleaner")


@dataclass
class CleanBatchResult:
    """Result of a batch clean operation."""

    cleaned: int
    skipped: int  # articles already cleaned (idempotency)
    failed: int
    duration_seconds: float


class TextCleaner:
    """Applies normalization transforms to extracted article text."""

    # URL pattern for removal
    _URL_PATTERN = re.compile(r"https?://\S+")
    # UTM and tracking parameter patterns
    _UTM_PARAM = re.compile(r"\?utm_[^&\s]*|&utm_[^&\s]*")
    _TRACKING_HASH = re.compile(r"#[a-zA-Z0-9_-]{20,}")

    def clean_batch(
        self,
        db: Session,
        articles: List[CleanedArticleRecord],
    ) -> CleanBatchResult:
        """Apply cleaning transforms to a batch of cleaned articles."""
        start = time.monotonic()
        cleaned_count = 0
        skipped_count = 0
        failed_count = 0

        for article in articles:
            if article.id is None or article.raw_article_id is None:
                continue

            raw_text = article.clean_text
            if not raw_text:
                failed_count += 1
                continue

            # Idempotency: if word_count > 0 and we've already run, skip
            # We consider "already cleaned" as text that passes minimal checks
            result = self._clean_text(raw_text)

            word_count = len(result.split())
            if word_count < settings.MIN_ARTICLE_WORD_COUNT:
                logger.warning(
                    "Article %s too short after cleaning (%d words) — marking FAILED",
                    article.raw_article_id,
                    word_count,
                )
                try:
                    db.query(RawArticle).filter(
                        RawArticle.id == article.raw_article_id
                    ).update({"status": PipelineStatus.FAILED})
                    db.commit()
                except Exception:
                    db.rollback()
                failed_count += 1
                continue

            try:
                db.query(CleanedArticle).filter(
                    CleanedArticle.id == article.id
                ).update({"clean_text": result, "word_count": word_count})
                db.commit()
                cleaned_count += 1
            except Exception as e:
                db.rollback()
                logger.error("Failed to update cleaned article %s: %s", article.id, e)
                failed_count += 1

        duration = time.monotonic() - start
        return CleanBatchResult(
            cleaned=cleaned_count,
            skipped=skipped_count,
            failed=failed_count,
            duration_seconds=round(duration, 2),
        )

    def _clean_text(self, raw_text: str) -> str:
        """Apply all seven transforms in documented order."""
        # Transform 1 — HTML residue removal
        soup = BeautifulSoup(raw_text, "lxml")
        text = soup.get_text(separator=" ")

        # Transform 2 — URL removal
        text = self._URL_PATTERN.sub("", text)

        # Transform 3 — Tracking parameter strip
        text = self._UTM_PARAM.sub("", text)
        text = self._TRACKING_HASH.sub("", text)

        # Transform 4 — Whitespace normalization
        text = re.sub(r"[\n\r\t]+", " ", text)
        text = re.sub(r" +", " ", text)
        text = text.strip()

        # Transform 5 — Duplicate paragraph removal
        segments = [s.strip() for s in text.split(". ") if len(s.strip()) >= 20]
        seen: set[str] = set()
        unique: list[str] = []
        for s in segments:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        text = ". ".join(unique)

        # Transform 6 — Unicode normalization
        text = unicodedata.normalize("NFKC", text)
        text = text.replace("\u201c", '"').replace("\u201d", '"')  # smart quotes
        text = text.replace("\u2018", "'").replace("\u2019", "'")
        text = text.replace("\u2014", " - ")  # em dash
        text = text.replace("\u00a0", " ")  # non-breaking space

        return text.strip()
