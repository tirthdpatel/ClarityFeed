"""
Feed source repository — manages RSS source records in the database.

Provides ``get_active_sources()`` to query active sources and
``seed_default_sources()`` to populate the database with an internationally
diverse initial set of RSS feeds on first deployment.
"""
from __future__ import annotations

import logging
from typing import List

from sqlalchemy.orm import Session

from backend.database.orm_models import Source

logger = logging.getLogger("news.collector.feed_sources")

# ---------------------------------------------------------------------------
# Default seed sources — internationally diverse, currently active RSS feeds
#
# REMOVED IN PHASE 0 (ARCHITECTURE_V2 §A8):
#
#   "Reuters Top News"  https://www.reutersagency.com/feed/?taxonomy=best-topics
#       The Reuters Agency feed was retired. Reuters does not publish a free
#       public RSS feed; wire-service content requires a paid licence, which
#       §14 consciously declines. Do not re-add a Reuters feed without one.
#
#   "Associated Press"  https://rsshub.app/apnews/topics/apf-topnews
#       Not an AP feed. RSSHub is a third-party scraper, so this ingested AP
#       content through an intermediary with no licence to redistribute it —
#       precisely the posture §11 exists to avoid. Removed on legal grounds,
#       not merely reliability ones.
#
# Both left every article they produced attributed to a publisher that had
# not agreed to be there. Eight verified first-party feeds remain below.
# ---------------------------------------------------------------------------

DEFAULT_SOURCES: list[dict[str, str]] = [
    {
        "name": "BBC World News",
        "url": "https://www.bbc.com/news/world",
        "feed_url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "language": "en",
        "country": "UK",
        "category": "general",
    },
    {
        "name": "Al Jazeera English",
        "url": "https://www.aljazeera.com",
        "feed_url": "https://www.aljazeera.com/xml/rss/all.xml",
        "language": "en",
        "country": "QA",
        "category": "general",
    },
    {
        "name": "Deutsche Welle",
        "url": "https://www.dw.com",
        "feed_url": "https://rss.dw.com/rdf/rss-en-all",
        "language": "en",
        "country": "DE",
        "category": "general",
    },
    {
        "name": "France 24 English",
        "url": "https://www.france24.com/en",
        "feed_url": "https://www.france24.com/en/rss",
        "language": "en",
        "country": "FR",
        "category": "general",
    },
    {
        "name": "NPR News",
        "url": "https://www.npr.org",
        "feed_url": "https://feeds.npr.org/1001/rss.xml",
        "language": "en",
        "country": "US",
        "category": "general",
    },
    {
        "name": "The Guardian World",
        "url": "https://www.theguardian.com/world",
        "feed_url": "https://www.theguardian.com/world/rss",
        "language": "en",
        "country": "UK",
        "category": "general",
    },
    {
        "name": "South China Morning Post",
        "url": "https://www.scmp.com",
        "feed_url": "https://www.scmp.com/rss/91/feed",
        "language": "en",
        "country": "HK",
        "category": "general",
    },
    {
        "name": "Times of India",
        "url": "https://timesofindia.indiatimes.com",
        "feed_url": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "language": "en",
        "country": "IN",
        "category": "general",
    },
]


# ---------------------------------------------------------------------------
# Candidate sources for the Phase 1 country expansion — NOT SEEDED
# ---------------------------------------------------------------------------
#
# These extend coverage toward the twelve launch countries in
# ARCHITECTURE_V3.md Part H. They are deliberately NOT in DEFAULT_SOURCES,
# because every URL here is unverified: the two feeds Phase 0 just removed
# were themselves once "obviously fine" entries in a seed list, and replacing
# dead feeds with unverified ones repeats exactly that mistake.
#
# Promotion process (Phase 1, requires the admin "Test" action from §14):
#   1. Feed returns HTTP 200 and parses with >0 entries
#   2. robots.txt permits our user-agent (now fail-closed — see compliance.py)
#   3. A source_permissions row is created and reviewed
#   4. Only then move the entry into DEFAULT_SOURCES
#
# Anything still in this list has completed none of those steps.
CANDIDATE_SOURCES: list[dict[str, str]] = [
    {"name": "The Hindu", "country": "IN", "language": "en",
     "url": "https://www.thehindu.com", "feed_url": "", "category": "general"},
    {"name": "NDTV", "country": "IN", "language": "en",
     "url": "https://www.ndtv.com", "feed_url": "", "category": "general"},
    {"name": "CBC News", "country": "CA", "language": "en",
     "url": "https://www.cbc.ca", "feed_url": "", "category": "general"},
    {"name": "ABC News Australia", "country": "AU", "language": "en",
     "url": "https://www.abc.net.au/news", "feed_url": "", "category": "general"},
    {"name": "The Japan Times", "country": "JP", "language": "en",
     "url": "https://www.japantimes.co.jp", "feed_url": "", "category": "general"},
    {"name": "Channel News Asia", "country": "SG", "language": "en",
     "url": "https://www.channelnewsasia.com", "feed_url": "", "category": "general"},
    {"name": "G1 Globo", "country": "BR", "language": "pt",
     "url": "https://g1.globo.com", "feed_url": "", "category": "general"},
    {"name": "News24", "country": "ZA", "language": "en",
     "url": "https://www.news24.com", "feed_url": "", "category": "general"},
]


class FeedSourceRepository:
    """Data-access layer for RSS feed sources."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def get_active_sources(self) -> List[Source]:
        """Return all sources where ``is_active`` is True."""
        return self._db.query(Source).filter(Source.is_active.is_(True)).all()

    def seed_default_sources(self) -> int:
        """Insert the default international RSS sources if the table is empty.

        Returns the number of sources inserted.  If the table already contains
        data the method is a no-op and returns 0.
        """
        existing = self._db.query(Source).count()
        if existing > 0:
            logger.info("Sources table already has %d rows — skipping seed.", existing)
            return 0

        inserted = 0
        for src in DEFAULT_SOURCES:
            try:
                source = Source(
                    name=src["name"],
                    url=src["url"],
                    feed_url=src["feed_url"],
                    language=src.get("language", "en"),
                    country=src.get("country", ""),
                    category=src.get("category", "general"),
                    is_active=True,
                )
                self._db.add(source)
                self._db.flush()
                inserted += 1
            except Exception:
                self._db.rollback()
                logger.error("Failed to seed source %s", src["name"], exc_info=True)

        if inserted:
            self._db.commit()
            logger.info("Seeded %d default RSS sources.", inserted)
        return inserted
