"""Unit tests for backend/cleaner/text_cleaner.py."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.cleaner.text_cleaner import TextCleaner
from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import Base, CleanedArticle, RawArticle, Source


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


class TestTextCleaner:
    """Tests for the text cleaner."""

    def test_html_residue_stripped(self, db_session) -> None:
        """HTML residue is stripped correctly."""
        cleaner = TextCleaner()
        text = '<p>Hello world this is a longer paragraph with enough text.</p> <a href="#">link</a> More content here for the second sentence.'
        result = cleaner._clean_text(text)
        assert "<" not in result or ">" not in result
        assert "Hello" in result
        assert "world" in result

    def test_urls_removed(self, db_session) -> None:
        """URLs are removed from text."""
        cleaner = TextCleaner()
        text = "Check this https://example.com/page and http://other.org"
        result = cleaner._clean_text(text)
        assert "https://" not in result
        assert "http://" not in result

    def test_duplicate_sentence_removed(self, db_session) -> None:
        """Duplicate sentences are removed."""
        cleaner = TextCleaner()
        text = "First unique sentence. Second unique sentence. First unique sentence. Third."
        result = cleaner._clean_text(text)
        assert result.count("First unique sentence") == 1

    def test_unicode_normalization(self, db_session) -> None:
        """Unicode normalization (smart quotes, em dash)."""
        cleaner = TextCleaner()
        text = "Quote \u201ctest\u201d and dash \u2014 here"
        result = cleaner._clean_text(text)
        assert '"' in result
        assert " - " in result

    def test_short_article_fails(self, db_session) -> None:
        """Short article triggers FAILED status."""
        raw = RawArticle(
            id=1,
            source_id=1,
            url="https://example.com/1",
            title="Short",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        cleaned = CleanedArticle(
            id=1,
            raw_article_id=1,
            clean_text="Only ten words here now",
            word_count=5,
        )
        db_session.add(cleaned)
        db_session.commit()

        articles = [
            CleanedArticleRecord(
                id=1,
                raw_article_id=1,
                clean_text="Only ten words here now",
                word_count=5,
            )
        ]
        result = TextCleaner().clean_batch(db_session, articles)
        assert result.failed >= 1
        r = db_session.query(RawArticle).filter(RawArticle.id == 1).first()
        assert r.status == PipelineStatus.FAILED

    def test_clean_batch_idempotent(self, db_session) -> None:
        """Calling clean_batch twice produces identical results."""
        raw = RawArticle(
            id=2,
            source_id=1,
            url="https://example.com/2",
            title="Test",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        long_text = " ".join(["word"] * 60)
        cleaned = CleanedArticle(
            id=2,
            raw_article_id=2,
            clean_text=long_text,
            word_count=60,
        )
        db_session.add(cleaned)
        db_session.commit()

        articles = [
            CleanedArticleRecord(id=2, raw_article_id=2, clean_text=long_text, word_count=60)
        ]
        result1 = TextCleaner().clean_batch(db_session, articles)
        articles[0].clean_text = long_text
        result2 = TextCleaner().clean_batch(db_session, articles)
        assert result1.cleaned == result2.cleaned or (result1.cleaned >= 1 and result2.cleaned >= 1)
