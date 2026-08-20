"""Unit tests for backend/compliance.py."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.compliance import (
    ConditionalRequestHeaders,
    RateLimiter,
    RobotsTxtChecker,
    _robots_cache,
    validate_attribution,
)


# ---------------------------------------------------------------------------
# RobotsTxtChecker
# ---------------------------------------------------------------------------


class TestRobotsTxtChecker:
    """Tests for RobotsTxtChecker."""

    def setup_method(self) -> None:
        _robots_cache.clear()

    @patch("requests.get")
    @patch("backend.compliance.RobotFileParser")
    def test_is_allowed_happy_path(
        self, mock_parser_cls: MagicMock, mock_get: MagicMock
    ) -> None:
        """robots.txt allows the URL."""
        mock_get.return_value = self._response()
        parser_instance = MagicMock()
        parser_instance.can_fetch.return_value = True
        mock_parser_cls.return_value = parser_instance

        checker = RobotsTxtChecker("https://example.com")
        assert checker.is_allowed("https://example.com/article/1") is True

    @patch("requests.get")
    @patch("backend.compliance.RobotFileParser")
    def test_is_allowed_disallowed(
        self, mock_parser_cls: MagicMock, mock_get: MagicMock
    ) -> None:
        """robots.txt disallows the URL."""
        mock_get.return_value = self._response()
        parser_instance = MagicMock()
        parser_instance.can_fetch.return_value = False
        mock_parser_cls.return_value = parser_instance

        checker = RobotsTxtChecker("https://example.com")
        assert checker.is_allowed("https://example.com/private") is False

    @staticmethod
    def _response(status: int = 200, text: str = "User-agent: *\nAllow: /\n") -> MagicMock:
        """A stand-in for a requests Response, minimal on purpose."""
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        return resp

    @patch("requests.get")
    def test_fetch_failure_denies_by_default(self, mock_get: MagicMock) -> None:
        """FAIL-CLOSED: unreachable robots.txt denies the fetch (A9).

        Previously this returned True. That meant a DNS blip promoted us from
        a compliant crawler to one ignoring robots.txt entirely.
        """
        mock_get.side_effect = Exception("network error")

        checker = RobotsTxtChecker("https://example.com")
        assert checker.is_allowed("https://example.com/article") is False

    @patch("requests.get")
    def test_fetch_failure_allows_when_fail_closed_disabled(
        self, mock_get: MagicMock
    ) -> None:
        """The permissive fallback remains available as an explicit opt-in."""
        mock_get.side_effect = Exception("network error")

        checker = RobotsTxtChecker("https://example.com", fail_closed=False)
        assert checker.is_allowed("https://example.com/article") is True

    @patch("backend.compliance.RobotFileParser")
    @patch("requests.get")
    def test_parse_error_denies(
        self, mock_get: MagicMock, mock_parser_cls: MagicMock
    ) -> None:
        """A parser that raises on can_fetch is treated as unknown -> deny."""
        mock_get.return_value = self._response()
        parser_instance = MagicMock()
        parser_instance.can_fetch.side_effect = Exception("malformed robots.txt")
        mock_parser_cls.return_value = parser_instance

        checker = RobotsTxtChecker("https://example.com")
        assert checker.is_allowed("https://example.com/article") is False

    @patch("requests.get")
    def test_missing_robots_txt_is_allowed(self, mock_get: MagicMock) -> None:
        """404 means 'no restrictions', not 'unknown' — it must still allow.

        Fail-closed must not become fail-paranoid: a site with no robots.txt
        at all is explicitly permitted by the standard, and denying those
        would silently drop a large fraction of legitimate sources.
        """
        mock_get.return_value = self._response(status=404, text="Not Found")

        checker = RobotsTxtChecker("https://example.com")
        assert checker.is_allowed("https://example.com/article") is True

    @patch("requests.get")
    def test_unauthorised_robots_txt_denies_everything(
        self, mock_get: MagicMock
    ) -> None:
        """401/403 on robots.txt means the whole site is off limits (RFC 9309).

        This is the one 4xx that is not permission to proceed, and it is easy
        to lose when hand-rolling the status handling that RobotFileParser.read
        used to do internally.
        """
        for status in (401, 403):
            _robots_cache.clear()
            mock_get.return_value = self._response(status=status, text="")
            checker = RobotsTxtChecker("https://example.com")
            assert checker.is_allowed("https://example.com/article") is False

    @patch("requests.get")
    def test_server_error_denies(self, mock_get: MagicMock) -> None:
        """A 5xx is an unknown answer, not a permissive one — deny."""
        mock_get.return_value = self._response(status=503, text="")

        checker = RobotsTxtChecker("https://example.com")
        assert checker.is_allowed("https://example.com/article") is False

    @patch("requests.get")
    def test_disallow_is_honoured_from_real_parse(self, mock_get: MagicMock) -> None:
        """End-to-end through the real parser: a Disallow rule must bite.

        The other tests mock RobotFileParser, so none of them would notice if
        the fetched body never reached parse() at all.
        """
        mock_get.return_value = self._response(
            text="User-agent: *\nDisallow: /private\n"
        )

        checker = RobotsTxtChecker("https://example.com")
        assert checker.is_allowed("https://example.com/private/x") is False
        assert checker.is_allowed("https://example.com/public/x") is True

    @patch("requests.get")
    def test_identifies_itself(self, mock_get: MagicMock) -> None:
        """We send our own User-Agent, not urllib's default."""
        mock_get.return_value = self._response()

        RobotsTxtChecker("https://example.com").is_allowed("https://example.com/a")

        assert mock_get.call_args.kwargs["headers"]["User-Agent"].startswith(
            "ClarityFeedBot/"
        )

    @patch("requests.get")
    def test_cache_hit(self, mock_get: MagicMock) -> None:
        """Second call uses the cached parser (no second fetch)."""
        mock_get.return_value = self._response()

        checker = RobotsTxtChecker("https://example.com")
        checker.is_allowed("https://example.com/a")
        checker.is_allowed("https://example.com/b")

        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """Tests for per-domain token-bucket RateLimiter."""

    def test_acquire_basic(self) -> None:
        """Acquiring a token should succeed without sleeping for burst."""
        limiter = RateLimiter(default_rps=1.0, burst=3)
        # Should not raise or sleep for burst-sized calls
        for _ in range(3):
            asyncio.get_event_loop().run_until_complete(
                limiter.acquire("https://example.com/feed")
            )

    def test_per_domain_isolation(self) -> None:
        """Different domains should have separate token buckets."""
        limiter = RateLimiter(default_rps=1.0, burst=1)

        asyncio.get_event_loop().run_until_complete(
            limiter.acquire("https://a.com/feed")
        )
        # Second domain should also succeed immediately
        asyncio.get_event_loop().run_until_complete(
            limiter.acquire("https://b.com/feed")
        )

    def test_overrides(self) -> None:
        """Custom rate limit overrides for specific domains."""
        limiter = RateLimiter(
            default_rps=1.0,
            burst=3,
            overrides={"fast.com": 10.0},
        )
        # fast.com should use the override rate
        asyncio.get_event_loop().run_until_complete(
            limiter.acquire("https://fast.com/feed")
        )


# ---------------------------------------------------------------------------
# ConditionalRequestHeaders
# ---------------------------------------------------------------------------


class TestConditionalRequestHeaders:
    """Tests for ConditionalRequestHeaders."""

    def test_empty_headers_for_unknown_url(self) -> None:
        """Unknown URLs should return empty headers."""
        headers = ConditionalRequestHeaders()
        assert headers.get_headers("https://example.com/feed") == {}

    def test_etag_handling(self) -> None:
        """After update with ETag, get_headers returns If-None-Match."""
        headers = ConditionalRequestHeaders()
        headers.update(
            "https://example.com/feed",
            {"ETag": '"abc123"'},
        )
        result = headers.get_headers("https://example.com/feed")
        assert result["If-None-Match"] == '"abc123"'

    def test_last_modified_handling(self) -> None:
        """After update with Last-Modified, get_headers returns If-Modified-Since."""
        headers = ConditionalRequestHeaders()
        headers.update(
            "https://example.com/feed",
            {"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
        )
        result = headers.get_headers("https://example.com/feed")
        assert result["If-Modified-Since"] == "Mon, 01 Jan 2024 00:00:00 GMT"

    def test_both_etag_and_last_modified(self) -> None:
        """Both headers should be returned when both are present."""
        headers = ConditionalRequestHeaders()
        headers.update(
            "https://example.com/feed",
            {"ETag": '"xyz"', "Last-Modified": "Tue, 02 Jan 2024 00:00:00 GMT"},
        )
        result = headers.get_headers("https://example.com/feed")
        assert "If-None-Match" in result
        assert "If-Modified-Since" in result


# ---------------------------------------------------------------------------
# validate_attribution
# ---------------------------------------------------------------------------


class TestValidateAttribution:
    """Tests for the validate_attribution function."""

    def test_valid_article(self) -> None:
        """Article with all required fields passes."""
        article = {"url": "https://x.com/1", "source_name": "Test", "title": "Hello"}
        assert validate_attribution(article) is True

    def test_missing_url(self) -> None:
        """Article missing url fails."""
        article = {"source_name": "Test", "title": "Hello"}
        assert validate_attribution(article) is False

    def test_empty_title(self) -> None:
        """Article with empty title fails."""
        article = {"url": "https://x.com/1", "source_name": "Test", "title": ""}
        assert validate_attribution(article) is False

    def test_missing_source_name(self) -> None:
        """Article missing source_name fails."""
        article = {"url": "https://x.com/1", "title": "Hello"}
        assert validate_attribution(article) is False
