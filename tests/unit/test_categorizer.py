"""Unit tests for backend/categorizer/categorizer.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.categorizer.categorizer import Categorizer
from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import Base, Category, RawArticle, Source, Summary
from backend.summarizer.groq_client import GroqLLMClient


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    source = Source(
        id=1,
        name="Test",
        url="https://example.com",
        feed_url="https://example.com/rss.xml",
    )
    session.add(source)
    session.commit()
    yield session
    session.close()


class TestCategorizer:
    """Tests for the categorizer."""

    @pytest.fixture
    def setup_article_with_summary(self, db_session):
        """Create raw article and summary."""
        raw = RawArticle(
            id=1,
            source_id=1,
            url="https://example.com/1",
            title="Tech News",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        summary = Summary(
            raw_article_id=1,
            summary_text="AI advances",
            tldr="AI advances",
            bullet_points='["Point 1"]',
        )
        db_session.add(summary)
        db_session.commit()
        return raw, summary

    @patch.object(GroqLLMClient, "complete", new_callable=AsyncMock)
    def test_valid_category_produces_record(
        self, mock_complete, db_session, setup_article_with_summary
    ) -> None:
        """Valid category response produces correct CategoryRecord."""
        mock_complete.return_value = '{"category": "Technology", "confidence": 0.92}'

        raw, summary = setup_article_with_summary
        summaries_map = {1: summary}

        categorizer = Categorizer(GroqLLMClient())
        result = _run(
            categorizer.categorize_batch(
                db_session,
                [CleanedArticleRecord(id=1, raw_article_id=1, clean_text="text", word_count=1)],
                summaries_map,
            )
        )
        assert result.categorized >= 1
        c = db_session.query(Category).filter(Category.raw_article_id == 1).first()
        assert c is not None
        assert c.primary_category == "Technology"
        r = db_session.query(RawArticle).filter(RawArticle.id == 1).first()
        assert r.status == PipelineStatus.PROCESSED

    @patch.object(GroqLLMClient, "complete", new_callable=AsyncMock)
    def test_invalid_category_coerced_to_world(
        self, mock_complete, db_session, setup_article_with_summary
    ) -> None:
        """Invalid category from LLM is marked Uncategorized, not World.

        The code previously claimed to coerce to "World" but the branch was
        dead — it set confidence=0.0, which always tripped the low-confidence
        check and overwrote it. "Uncategorized" is also correct on the merits:
        World is a real reader-facing feed and should not collect garbage.
        """
        mock_complete.return_value = '{"category": "InvalidLabel", "confidence": 0.5}'

        raw, summary = setup_article_with_summary
        summaries_map = {1: summary}

        categorizer = Categorizer(GroqLLMClient())
        _run(
            categorizer.categorize_batch(
                db_session,
                [CleanedArticleRecord(id=1, raw_article_id=1, clean_text="text", word_count=1)],
                summaries_map,
            )
        )
        c = db_session.query(Category).filter(Category.raw_article_id == 1).first()
        assert c is not None
        assert c.primary_category == "Uncategorized"

    @patch.object(GroqLLMClient, "complete", new_callable=AsyncMock)
    def test_idempotency_skipped(
        self, mock_complete, db_session, setup_article_with_summary
    ) -> None:
        """Article already in categories is skipped."""
        mock_complete.return_value = '{"category": "Technology", "confidence": 0.9}'

        raw, summary = setup_article_with_summary
        cat = Category(raw_article_id=1, primary_category="Technology", confidence=0.9)
        db_session.add(cat)
        db_session.commit()
        summaries_map = {1: summary}

        categorizer = Categorizer(GroqLLMClient())
        result = _run(
            categorizer.categorize_batch(
                db_session,
                [CleanedArticleRecord(id=1, raw_article_id=1, clean_text="text", word_count=1)],
                summaries_map,
            )
        )
        assert result.skipped >= 1
        mock_complete.assert_not_called()
