"""
Async RSS feed fetcher with full compliance integration.

Uses ``httpx.AsyncClient`` to fetch RSS/Atom feeds. Every request passes
through:
 1. ``RobotsTxtChecker`` — robots.txt compliance
 2. ``RateLimiter`` — per-domain rate limiting
 3. ``ConditionalRequestHeaders`` — ETag / Last-Modified caching
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import feedparser
import httpx

from backend.compliance import (
    ConditionalRequestHeaders,
    RateLimiter,
    RobotsTxtChecker,
)

logger = logging.getLogger("news.collector.rss_fetcher")


class RSSFetcher:
    """Async fetcher for RSS/Atom feeds with compliance integration.

    Each ``fetch_feed`` call checks robots.txt, acquires a rate-limiter
    token, attaches conditional request headers, and parses the feed.
    """

    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        cond_headers: ConditionalRequestHeaders | None = None,
    ) -> None:
        self._rate_limiter = rate_limiter or RateLimiter()
        self._cond_headers = cond_headers or ConditionalRequestHeaders()

    async def fetch_feed(
        self,
        source: Any,
    ) -> List[Dict[str, Any]]:
        """Fetch and parse a single RSS/Atom feed.

        Parameters
        ----------
        source:
            An object (ORM model or dataclass) with ``feed_url``, ``url``,
            and ``name`` attributes.

        Returns
        -------
        list[dict]
            Parsed entries with keys: ``title``, ``url``, ``published``,
            ``summary``, ``source_name``. Returns an empty list on failure.
        """
        feed_url: str = source.feed_url
        base_url = self._base_url(source.url or feed_url)

        # 1. robots.txt check
        checker = RobotsTxtChecker(base_url)
        if not checker.is_allowed(feed_url):
            logger.warning("robots.txt disallows %s — skipping", feed_url)
            return []

        # 2. Rate limit
        await self._rate_limiter.acquire(feed_url)

        # 3. Conditional headers
        headers = self._cond_headers.get_headers(feed_url)
        headers["User-Agent"] = "ClarityFeed/0.1 (+https://github.com/ClarityFeed)"

        # 4. Fetch
        #
        # follow_redirects is on deliberately. Publishers move feed URLs and
        # leave a 301 behind rather than a copy; with redirects off, such a
        # feed reports "Non-200 status 301" and then "empty feed", which reads
        # as a dead source and is not. SCMP did exactly this.
        #
        # max_redirects is bounded, and a redirect that leaves the original
        # host has its robots.txt re-checked below — otherwise a redirect
        # would be a way around the check we just performed.
        try:
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True, max_redirects=5
            ) as client:
                response = await client.get(feed_url, headers=headers)
        except httpx.TimeoutException:
            logger.error("Timeout fetching %s", feed_url)
            return []
        except httpx.HTTPError as exc:
            logger.error("HTTP error fetching %s: %s", feed_url, exc)
            return []
        except Exception:
            logger.error("Unexpected error fetching %s", feed_url, exc_info=True)
            return []

        # 4b. A cross-host redirect is a new publisher as far as robots.txt
        # is concerned. Re-check before reading the body.
        #
        # Compare against the *feed* host, not `base_url`: base_url is derived
        # from the source's homepage, and a feed legitimately lives on a
        # different host from the site it belongs to (feeds.bbci.co.uk vs
        # www.bbc.com). Comparing against it reports a redirect on every such
        # source when nothing redirected at all.
        final_url = str(response.url)
        if self._base_url(final_url) != self._base_url(feed_url):
            logger.info("%s redirected to %s — re-checking robots.txt",
                        feed_url, final_url)
            if not RobotsTxtChecker(self._base_url(final_url)).is_allowed(final_url):
                logger.warning(
                    "robots.txt at the redirect target disallows %s — skipping",
                    final_url,
                )
                return []

        # 5. Handle 304 Not Modified
        if response.status_code == 304:
            logger.debug("304 Not Modified for %s", feed_url)
            return []

        if response.status_code != 200:
            logger.warning("Non-200 status %d for %s", response.status_code, feed_url)
            return []

        # Update conditional headers for next fetch
        self._cond_headers.update(feed_url, dict(response.headers))

        # 6. Parse feed
        try:
            feed = feedparser.parse(response.text)
        except Exception:
            logger.error("Failed to parse feed %s", feed_url, exc_info=True)
            return []

        if feed.bozo and not feed.entries:
            logger.warning("Malformed feed (bozo) with no entries: %s", feed_url)
            return []

        # 7. Extract entries
        articles: List[Dict[str, Any]] = []
        source_name: str = getattr(source, "name", "")
        for entry in feed.entries:
            article = self._parse_entry(entry, source_name)
            if article:
                articles.append(article)

        logger.info("Fetched %d entries from %s", len(articles), feed_url)
        return articles

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _base_url(url: str) -> str:
        """Extract the base URL (scheme + netloc) from a full URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _parse_entry(entry: Any, source_name: str) -> Optional[Dict[str, Any]]:
        """Parse a single feedparser entry into a normalised dict."""
        title = getattr(entry, "title", None)
        link = getattr(entry, "link", None)
        if not title or not link:
            return None

        published: Optional[datetime] = None
        published_parsed = getattr(entry, "published_parsed", None)
        if published_parsed:
            try:
                published = datetime(*published_parsed[:6])
            except Exception:
                pass

        summary = getattr(entry, "summary", None) or ""

        return {
            "title": title.strip(),
            "url": link.strip(),
            "published": published,
            "summary": summary.strip() if summary else "",
            "source_name": source_name,
        }
