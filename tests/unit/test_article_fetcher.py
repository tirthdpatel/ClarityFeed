"""Unit tests for backend/fetcher/article_fetcher.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import PipelineStatus, RawArticleRecord
from backend.database.orm_models import Base, CleanedArticle, RawArticle, Source
from backend.fetcher.article_fetcher import ArticleFetcher, FetchBatchResult


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    source = Source(
        id=1,
        name="Test Source",
        url="https://example.com",
        feed_url="https://example.com/rss.xml",
    )
    session.add(source)
    session.commit()

    yield session
    session.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestArticleFetcher:
    """Tests for the article fetcher."""

    @patch("backend.fetcher.article_fetcher.RobotsTxtChecker")
    @patch("backend.fetcher.article_fetcher.RateLimiter")
    @patch("backend.fetcher.article_fetcher.httpx.AsyncClient")
    def test_successful_fetch_layer1(
        self, mock_client_cls, mock_rate_cls, mock_robots_cls, db_session
    ) -> None:
        """Successful fetch and extraction via Layer 1 (newspaper3k)."""
        mock_robots_cls.return_value.is_allowed.return_value = True
        mock_limiter = AsyncMock()
        mock_rate_cls.return_value = mock_limiter

        raw = RawArticle(
            id=1,
            source_id=1,
            url="https://example.com/article/1",
            title="Test Article",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.commit()

        html = '<html><body><article><p>Word ' + ' '.join(['x'] * 60) + '</p></article></body></html>'
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        # NOTE: this previously also wrapped everything in
        #   patch("backend.fetcher.article_fetcher.Article._extract_body")
        # which targeted newspaper3k's Article class — that class has no
        # `_extract_body` method, so the patch raised AttributeError and the
        # test had been failing. `_extract_body` belongs to ArticleFetcher,
        # which the inner patch already handles correctly.
        with patch.object(ArticleFetcher, "_extract_body") as mock_extract:
            mock_extract.return_value = " ".join(["word"] * 60)

            fetcher = ArticleFetcher()
            result = _run(
                fetcher.fetch_and_extract_batch(
                    db_session,
                    [RawArticleRecord(id=1, source_id=1, url="https://example.com/article/1", title="Test", status=PipelineStatus.PENDING)],
                )
            )

        assert isinstance(result, FetchBatchResult)
        # Assert the actual outcome. These were `>= 0`, which is true of every
        # possible result and therefore asserted nothing.
        assert result.fetched == 1
        assert result.extracted == 1
        assert result.failed == 0

        cleaned = db_session.query(CleanedArticle).filter(
            CleanedArticle.raw_article_id == 1
        ).first()
        assert cleaned is not None, "extraction should have written a cleaned_articles row"
        assert cleaned.word_count == 60

    @patch("backend.fetcher.article_fetcher.RobotsTxtChecker")
    @patch("backend.fetcher.article_fetcher.RateLimiter")
    @patch("backend.fetcher.article_fetcher.httpx.AsyncClient")
    def test_404_marks_failed(
        self, mock_client_cls, mock_rate_cls, mock_robots_cls, db_session
    ) -> None:
        """404 HTTP response marks article as FAILED."""
        mock_robots_cls.return_value.is_allowed.return_value = True
        mock_limiter = AsyncMock()
        mock_rate_cls.return_value = mock_limiter

        raw = RawArticle(
            id=2,
            source_id=1,
            url="https://example.com/article/404",
            title="Missing",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.commit()

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        fetcher = ArticleFetcher()
        result = _run(
            fetcher.fetch_and_extract_batch(
                db_session,
                [RawArticleRecord(id=2, source_id=1, url="https://example.com/article/404", title="Missing", status=PipelineStatus.PENDING)],
            )
        )

        assert result.failed >= 1 or result.extracted == 0
        db_session.refresh(raw)
        art = db_session.query(RawArticle).filter(RawArticle.id == 2).first()
        if art:
            assert art.status == PipelineStatus.FAILED

    @patch("backend.fetcher.article_fetcher.RobotsTxtChecker")
    def test_robots_disallow_skipped(self, mock_robots_cls, db_session) -> None:
        """robots.txt disallow skips article, no HTTP request."""
        mock_robots_cls.return_value.is_allowed.return_value = False

        raw = RawArticle(
            id=3,
            source_id=1,
            url="https://example.com/article/3",
            title="Blocked",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.commit()

        with patch("backend.fetcher.article_fetcher.httpx.AsyncClient") as mock_client_cls:
            fetcher = ArticleFetcher()
            _run(
                fetcher.fetch_and_extract_batch(
                    db_session,
                    [RawArticleRecord(id=3, source_id=1, url="https://example.com/article/3", title="Blocked", status=PipelineStatus.PENDING)],
                )
            )
            mock_client_cls.assert_not_called()

    def test_idempotency_skipped(self, db_session) -> None:
        """Article already in cleaned_articles is skipped."""
        raw = RawArticle(
            id=4,
            source_id=1,
            url="https://example.com/article/4",
            title="Already Cleaned",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        cleaned = CleanedArticle(raw_article_id=raw.id, clean_text="existing text", word_count=2)
        db_session.add(cleaned)
        db_session.commit()

        with patch("backend.fetcher.article_fetcher.RobotsTxtChecker") as mock_robots:
            mock_robots.return_value.is_allowed.return_value = True
            with patch("backend.fetcher.article_fetcher.RateLimiter") as mock_rate:
                mock_rate.return_value.acquire = AsyncMock()
                with patch("backend.fetcher.article_fetcher.httpx.AsyncClient"):
                    fetcher = ArticleFetcher()
                    result = _run(
                        fetcher.fetch_and_extract_batch(
                            db_session,
                            [RawArticleRecord(id=4, source_id=1, url="https://example.com/article/4", title="Already Cleaned", status=PipelineStatus.PENDING)],
                        )
                    )
        assert result.skipped >= 1
