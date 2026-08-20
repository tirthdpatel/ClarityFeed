"""Unit tests for backend/summarizer/summarizer.py."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import Base, CleanedArticle, RawArticle, Source, Summary
from backend.summarizer.summarizer import Summarizer
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


class TestSummarizer:
    """Tests for the summarizer."""

    @patch.object(GroqLLMClient, "complete", new_callable=AsyncMock)
    def test_valid_json_produces_summary(
        self, mock_complete, db_session
    ) -> None:
        """Valid JSON response produces correct Summary shape."""
        mock_complete.return_value = '{"tldr": "A summary.", "bullets": ["Point 1.", "Point 2.", "Point 3."]}'

        raw = RawArticle(
            id=1,
            source_id=1,
            url="https://example.com/1",
            title="Test",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        cleaned = CleanedArticle(
            raw_article_id=1,
            clean_text="Article text here " + " word" * 50,
            word_count=55,
        )
        db_session.add(cleaned)
        db_session.commit()

        summarizer = Summarizer(GroqLLMClient())
        result = _run(
            summarizer.summarize_batch(
                db_session,
                [CleanedArticleRecord(
                    id=1,
                    raw_article_id=1,
                    clean_text="Article text " + " word" * 50,
                    word_count=55,
                )],
            )
        )
        assert result.summarized >= 1
        s = db_session.query(Summary).filter(Summary.raw_article_id == 1).first()
        assert s is not None
        assert s.summary_text == "A summary."

    @patch.object(GroqLLMClient, "complete", new_callable=AsyncMock)
    def test_malformed_json_marks_failed(self, mock_complete, db_session) -> None:
        """Malformed JSON response marks article FAILED."""
        mock_complete.return_value = "not valid json {"

        raw = RawArticle(
            id=2,
            source_id=1,
            url="https://example.com/2",
            title="Test",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        cleaned = CleanedArticle(raw_article_id=2, clean_text="text " + "x" * 100, word_count=30)
        db_session.add(cleaned)
        db_session.commit()

        summarizer = Summarizer(GroqLLMClient())
        _run(
            summarizer.summarize_batch(
                db_session,
                [CleanedArticleRecord(id=2, raw_article_id=2, clean_text="text", word_count=30)],
            )
        )
        r = db_session.query(RawArticle).filter(RawArticle.id == 2).first()
        assert r.status == PipelineStatus.FAILED

    @patch.object(GroqLLMClient, "complete", new_callable=AsyncMock)
    def test_idempotency_skipped(self, mock_complete, db_session) -> None:
        """Article already in summaries is skipped."""
        mock_complete.return_value = '{"tldr": "Sum.", "bullets": ["B1."]}'

        raw = RawArticle(
            id=3,
            source_id=1,
            url="https://example.com/3",
            title="Test",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        cleaned = CleanedArticle(raw_article_id=3, clean_text="text", word_count=1)
        db_session.add(cleaned)
        summary = Summary(raw_article_id=3, summary_text="Existing", tldr="Existing", bullet_points='["B1"]')
        db_session.add(summary)
        db_session.commit()

        summarizer = Summarizer(GroqLLMClient())
        result = _run(
            summarizer.summarize_batch(
                db_session,
                [CleanedArticleRecord(id=3, raw_article_id=3, clean_text="text", word_count=1)],
            )
        )
        assert result.skipped >= 1
        mock_complete.assert_not_called()
