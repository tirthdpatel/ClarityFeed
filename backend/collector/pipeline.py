"""
RSS collection pipeline orchestrator.

Coordinates the full collection cycle: fetches all active sources, parses
their feeds, and inserts new articles. A single source failure does not
abort the entire cycle.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict

from sqlalchemy.orm import Session

from backend.collector.article_inserter import ArticleInserter
from backend.collector.feed_sources import FeedSourceRepository
from backend.collector.rss_fetcher import RSSFetcher

logger = logging.getLogger("news.collector.pipeline")


class CollectorPipeline:
    """Orchestrates a single RSS collection cycle.

    Usage::

        pipeline = CollectorPipeline()
        result = await pipeline.run_collection_cycle(db)
        # result: {"sources_processed": 10, "new_articles": 42,
        #          "errors": 0, "duration_seconds": 12.3}
    """

    def __init__(self) -> None:
        self._fetcher = RSSFetcher()
        self._inserter = ArticleInserter()

    async def run_collection_cycle(self, db: Session) -> Dict[str, Any]:
        """Run a complete collection cycle over all active sources.

        Returns a summary dict with keys:
         - ``sources_processed``: number of sources attempted
         - ``new_articles``: total newly inserted articles
         - ``errors``: number of sources that failed
         - ``duration_seconds``: wall-clock time for the cycle
        """
        start = time.time()
        repo = FeedSourceRepository(db)
        sources = repo.get_active_sources()

        sources_processed = 0
        new_articles = 0
        errors = 0

        for source in sources:
            try:
                articles = await self._fetcher.fetch_feed(source)
                if articles:
                    count = self._inserter.insert_new_articles(
                        db, articles, source.id
                    )
                    new_articles += count
                sources_processed += 1
            except Exception:
                errors += 1
                logger.error(
                    "Collection failed for source %s (id=%s)",
                    source.name,
                    source.id,
                    exc_info=True,
                )

        duration = round(time.time() - start, 2)
        summary: Dict[str, Any] = {
            "sources_processed": sources_processed,
            "new_articles": new_articles,
            "errors": errors,
            "duration_seconds": duration,
        }
        logger.info("Collection cycle complete: %s", summary)
        return summary
