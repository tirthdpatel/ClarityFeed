"""
Conflict-safe article inserter.

Inserts new articles from parsed RSS entries while gracefully handling
duplicate URLs via the unique constraint on ``raw_articles.url_hash``
(see backend/urls.py and ARCHITECTURE_V2.md §A4).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from backend.compliance import validate_attribution
from backend.database.models import PipelineStatus
from backend.database.orm_models import RawArticle
from backend.urls import url_hash

logger = logging.getLogger("news.collector.article_inserter")


class ArticleInserter:
    """Inserts new articles into the ``raw_articles`` table.

    Uses conflict-safe insertion: if a URL already exists, the entry is
    silently skipped. Returns the count of newly inserted articles.
    """

    def insert_new_articles(
        self,
        db: Session,
        articles: List[Dict[str, Any]],
        source_id: int,
    ) -> int:
        """Insert articles that don't already exist in the database.

        Parameters
        ----------
        db:
            Active SQLAlchemy session.
        articles:
            List of dicts with keys: ``title``, ``url``, ``published``,
            ``summary``, ``source_name``.
        source_id:
            Foreign key pointing to the source that produced these articles.

        Returns
        -------
        int
            Number of newly inserted articles.
        """
        inserted = 0
        for article in articles:
            # Attribution check
            article_with_source = {**article, "source_name": article.get("source_name", "")}
            if not validate_attribution(article_with_source):
                logger.warning("Skipping article with missing attribution: %s", article.get("url"))
                continue

            url = article.get("url", "")
            if not url:
                continue

            # A4: dedup on the canonical URL hash, not the raw URL. This also
            # collapses tracking-parameter variants of the same article, which
            # the raw-string comparison used to store as separate rows.
            article_hash = url_hash(url)

            existing = (
                db.query(RawArticle).filter(RawArticle.url_hash == article_hash).first()
            )
            if existing is not None:
                logger.debug("Duplicate URL (hash %s), skipping: %s", article_hash[:12], url)
                continue

            try:
                raw_article = RawArticle(
                    source_id=source_id,
                    url=url,
                    url_hash=article_hash,
                    title=article.get("title", ""),
                    published_at=article.get("published"),
                    summary_from_feed=article.get("summary", ""),
                    status=PipelineStatus.PENDING,
                )
                db.add(raw_article)
                db.flush()
                inserted += 1
            except Exception:
                db.rollback()
                logger.error("Failed to insert article %s", url, exc_info=True)

        if inserted:
            db.commit()

        logger.info("Inserted %d new articles for source_id=%d", inserted, source_id)
        return inserted
