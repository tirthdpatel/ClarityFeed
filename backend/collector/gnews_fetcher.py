"""Collect from the GNews API (gnews.io).

Presents the same interface as RSSFetcher — `fetch_feed_detailed(source)`
returning `(entries, meta)` — so the pipeline runner treats it as one more
source and none of the stages below it need to know the difference.

WHY THIS IS NOT JUST ANOTHER FEED

An RSS feed belongs to one publisher. A GNews query returns articles from
dozens, under one API key. This schema hangs both attribution and permissions
off the source row, so filing all of them under a single "GNews" row would
credit GNews for other people's reporting and give fifty publishers one shared
permission row — which is the whole thing backend/permissions.py exists to
prevent.

So each article is resolved to a `sources` row for the publisher that actually
wrote it, created on demand with kind='discovered' and is_active=False. Being
unreviewed, those rows resolve to Permissions.restrictive(): headline, short
excerpt, link. That is the correct posture for a publisher nobody has looked
at, and it is what the aggregator route would otherwise quietly bypass.

THE FREE TIER

100 requests/day, 10 articles per query, and a 12-hour delay on articles.
The delay is why this supplements the RSS feeds rather than replacing them:
those arrive within minutes. The budget below is a self-imposed ceiling well
under the provider's, on the same reasoning as LLM_DAILY_CALL_BUDGET — a
quota spent by accident at 3am is a quota unavailable when it matters.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.database.orm_models import Source
from config.settings import settings

logger = logging.getLogger("news.collector.gnews")

#: Marks a source row as belonging to a publisher discovered through an
#: aggregator rather than one with a feed of its own.
DISCOVERED_KIND = "discovered"
GNEWS_KIND = "gnews"

_SLUG = re.compile(r"[^a-z0-9]+")


def publisher_slug(name: str) -> str:
    """Stable identifier for a publisher name, used to build its feed_url."""
    return _SLUG.sub("-", (name or "").strip().lower()).strip("-") or "unknown"


class GNewsFetcher:
    """Fetches a GNews query and normalises it into pipeline entries."""

    def __init__(self, client: Any = None, db_factory: Any = None) -> None:
        self._client = client
        self._db_factory = db_factory

    # -- publisher resolution ------------------------------------------------

    def resolve_publisher(self, db: Session, name: str, url: str | None) -> Source | None:
        """Find or create the `sources` row for a publisher.

        Returns None when the article carries no usable publisher name, which
        makes it unattributable — and an unattributable article is dropped
        rather than filed under the aggregator.
        """
        name = (name or "").strip()
        if not name:
            return None

        feed_url = f"gnews://{publisher_slug(name)}"

        existing = db.query(Source).filter(Source.feed_url == feed_url).one_or_none()
        if existing is not None:
            return existing

        # Also match a publisher we already poll by RSS, so the New York Times
        # arriving via the aggregator lands on the same row — and therefore the
        # same permissions and the same takedown switch — as its own feed.
        by_name = (
            db.query(Source)
            .filter(Source.name == name)
            .filter(Source.kind != DISCOVERED_KIND)
            .first()
        )
        if by_name is not None:
            return by_name

        source = Source(
            name=name,
            url=(url or "").strip() or f"https://{publisher_slug(name)}.invalid",
            feed_url=feed_url,
            language="en",
            country="",
            category="general",
            # Never polled: there is no feed here. The row exists to own an
            # attribution and a permission row.
            is_active=False,
            kind=DISCOVERED_KIND,
        )
        db.add(source)
        db.flush()
        logger.info("Discovered publisher %r via GNews (source_id=%s)", name, source.id)
        return source

    # -- fetching ------------------------------------------------------------

    async def fetch_feed_detailed(self, source: Any) -> tuple[list[dict], dict]:
        """Run one GNews query. Returns (entries, transport metadata).

        Never raises: a dead aggregator must not end a run that the RSS feeds
        could still complete. The runner's circuit breaker reads `ok`.
        """
        if not settings.GNEWS_API_KEY:
            logger.warning("GNEWS_API_KEY is unset — skipping %s", source.name)
            return [], {"ok": False, "http_status": None, "error": "no api key"}

        # The query lives in the source row, so adding a second GNews source
        # with a different topic is a row rather than a deploy.
        query = (getattr(source, "category", "") or "general").strip()
        params = {
            "category": query,
            "lang": (getattr(source, "language", "") or "en").strip(),
            "max": settings.GNEWS_MAX_ARTICLES,
            "apikey": settings.GNEWS_API_KEY,
        }
        country = (getattr(source, "country", "") or "").strip()
        if country:
            params["country"] = country.lower()

        url = f"{settings.GNEWS_BASE_URL}/top-headlines"

        try:
            if self._client is not None:
                response = await self._client.get(url, params=params)
            else:
                async with httpx.AsyncClient(
                    timeout=settings.GNEWS_TIMEOUT_SECONDS
                ) as client:
                    response = await client.get(url, params=params)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.error("GNews request failed: %s", exc)
            return [], {"ok": False, "http_status": None, "error": str(exc)}

        status = getattr(response, "status_code", None)
        if status != 200:
            # 403 here almost always means the daily quota is gone. Logged at
            # warning rather than error: an exhausted free quota is expected
            # operation, not a fault, and paging on it would train everyone to
            # ignore the alert.
            level = logger.warning if status in (401, 403, 429) else logger.error
            level("GNews returned HTTP %s for %s", status, source.name)
            return [], {"ok": False, "http_status": status}

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("GNews returned unparseable JSON: %s", exc)
            return [], {"ok": False, "http_status": status, "error": "bad json"}

        entries = self._normalise(payload.get("articles") or [])
        return entries, {"ok": True, "http_status": status}

    # -- normalisation -------------------------------------------------------

    @staticmethod
    def _normalise(articles: list[dict]) -> list[dict]:
        """GNews articles -> the entry shape the pipeline already speaks."""
        out: list[dict] = []
        for article in articles:
            title = (article.get("title") or "").strip()
            link = (article.get("url") or "").strip()
            if not title or not link:
                continue

            publisher = article.get("source") or {}
            publisher_name = (publisher.get("name") or "").strip()
            if not publisher_name:
                # No publisher means no attribution, and this API is the one
                # place an article could otherwise be filed under whoever
                # relayed it. Drop it.
                logger.warning("GNews article has no publisher, dropping: %s", link)
                continue

            out.append(
                {
                    "title": title,
                    "url": link,
                    "published": _parse_published(article.get("publishedAt")),
                    "summary": (article.get("description") or "").strip(),
                    "source_name": publisher_name,
                    # Read by the runner to file the article under the real
                    # publisher rather than under the aggregator.
                    "publisher_name": publisher_name,
                    "publisher_url": (publisher.get("url") or "").strip(),
                }
            )
        return out


def _parse_published(value: Any) -> datetime | None:
    """GNews sends RFC 3339 with a trailing Z; store naive UTC like the rest."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
