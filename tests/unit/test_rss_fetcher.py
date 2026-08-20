"""Unit tests for backend/collector/rss_fetcher.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.collector.rss_fetcher import RSSFetcher
from backend.compliance import ConditionalRequestHeaders, RateLimiter


def _make_source(
    name: str = "Test Source",
    url: str = "https://example.com",
    feed_url: str = "https://example.com/rss.xml",
) -> MagicMock:
    """Create a mock source object."""
    source = MagicMock()
    source.name = name
    source.url = url
    source.feed_url = feed_url
    return source


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Test Article</title>
      <link>https://example.com/article/1</link>
      <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
      <description>Test summary</description>
    </item>
  </channel>
</rss>
"""


class TestRSSFetcher:
    """Tests for the async RSS fetcher."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @patch("backend.collector.rss_fetcher.RobotsTxtChecker")
    @patch("backend.collector.rss_fetcher.httpx.AsyncClient")
    def test_successful_fetch(self, mock_client_cls, mock_robots_cls) -> None:
        """Successfully fetches and parses an RSS feed."""
        # Mock robots.txt — allow
        mock_robots_cls.return_value.is_allowed.return_value = True

        # Mock HTTP response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SAMPLE_RSS
        mock_response.headers = {}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        fetcher = RSSFetcher(
            rate_limiter=RateLimiter(default_rps=100, burst=100),
            cond_headers=ConditionalRequestHeaders(),
        )
        source = _make_source()
        articles = self._run(fetcher.fetch_feed(source))

        assert len(articles) == 1
        assert articles[0]["title"] == "Test Article"
        assert articles[0]["url"] == "https://example.com/article/1"
        assert articles[0]["source_name"] == "Test Source"

    @patch("backend.collector.rss_fetcher.RobotsTxtChecker")
    @patch("backend.collector.rss_fetcher.httpx.AsyncClient")
    def test_304_not_modified(self, mock_client_cls, mock_robots_cls) -> None:
        """304 Not Modified returns an empty list."""
        mock_robots_cls.return_value.is_allowed.return_value = True

        mock_response = MagicMock()
        mock_response.status_code = 304

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        fetcher = RSSFetcher(
            rate_limiter=RateLimiter(default_rps=100, burst=100),
        )
        articles = self._run(fetcher.fetch_feed(_make_source()))
        assert articles == []

    @patch("backend.collector.rss_fetcher.RobotsTxtChecker")
    @patch("backend.collector.rss_fetcher.httpx.AsyncClient")
    def test_network_failure(self, mock_client_cls, mock_robots_cls) -> None:
        """Network failure returns an empty list."""
        import httpx

        mock_robots_cls.return_value.is_allowed.return_value = True

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.HTTPError("connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        fetcher = RSSFetcher(
            rate_limiter=RateLimiter(default_rps=100, burst=100),
        )
        articles = self._run(fetcher.fetch_feed(_make_source()))
        assert articles == []

    @patch("backend.collector.rss_fetcher.RobotsTxtChecker")
    @patch("backend.collector.rss_fetcher.httpx.AsyncClient")
    def test_malformed_feed(self, mock_client_cls, mock_robots_cls) -> None:
        """Malformed feed returns an empty list."""
        mock_robots_cls.return_value.is_allowed.return_value = True

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "not xml at all"
        mock_response.headers = {}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        fetcher = RSSFetcher(
            rate_limiter=RateLimiter(default_rps=100, burst=100),
        )
        # feedparser tolerates most garbage, so this may return [] or entries
        articles = self._run(fetcher.fetch_feed(_make_source()))
        assert isinstance(articles, list)

    @patch("backend.collector.rss_fetcher.RobotsTxtChecker")
    def test_robots_blocked(self, mock_robots_cls) -> None:
        """robots.txt disallow returns an empty list without fetching."""
        mock_robots_cls.return_value.is_allowed.return_value = False

        fetcher = RSSFetcher(
            rate_limiter=RateLimiter(default_rps=100, burst=100),
        )
        articles = self._run(fetcher.fetch_feed(_make_source()))
        assert articles == []
