"""Unit tests for backend/collector/article_inserter.py."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Override DATABASE_URL before importing application code
os.environ["DATABASE_URL"] = "sqlite:///test_inserter.db"

from backend.collector.article_inserter import ArticleInserter
from backend.database.models import PipelineStatus
from backend.database.orm_models import Base, RawArticle, Source


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Insert a test source
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


class TestArticleInserter:
    """Tests for conflict-safe article insertion."""

    def test_insert_new_articles(self, db_session) -> None:
        """New articles are inserted and count is returned."""
        inserter = ArticleInserter()
        articles = [
            {
                "title": "Article 1",
                "url": "https://example.com/article/1",
                "published": None,
                "summary": "Summary 1",
                "source_name": "Test Source",
            },
            {
                "title": "Article 2",
                "url": "https://example.com/article/2",
                "published": None,
                "summary": "Summary 2",
                "source_name": "Test Source",
            },
        ]
        count = inserter.insert_new_articles(db_session, articles, source_id=1)
        assert count == 2

        # Verify in database
        all_articles = db_session.query(RawArticle).all()
        assert len(all_articles) == 2

    def test_duplicate_url_skipped(self, db_session) -> None:
        """Duplicate URLs are skipped silently."""
        inserter = ArticleInserter()
        articles = [
            {
                "title": "Article 1",
                "url": "https://example.com/article/1",
                "published": None,
                "summary": "",
                "source_name": "Test Source",
            },
        ]
        # Insert once
        count1 = inserter.insert_new_articles(db_session, articles, source_id=1)
        assert count1 == 1

        # Insert same again
        count2 = inserter.insert_new_articles(db_session, articles, source_id=1)
        assert count2 == 0

    def test_missing_attribution_skipped(self, db_session) -> None:
        """Articles with missing attribution are skipped."""
        inserter = ArticleInserter()
        articles = [
            {
                "title": "",  # empty title
                "url": "https://example.com/article/3",
                "published": None,
                "summary": "",
                "source_name": "Test Source",
            },
        ]
        count = inserter.insert_new_articles(db_session, articles, source_id=1)
        assert count == 0

    def test_count_return_value(self, db_session) -> None:
        """Return value matches actual inserted count."""
        inserter = ArticleInserter()
        articles = [
            {"title": f"Art {i}", "url": f"https://example.com/{i}", "published": None, "summary": "", "source_name": "Test"}
            for i in range(5)
        ]
        count = inserter.insert_new_articles(db_session, articles, source_id=1)
        assert count == 5
        assert db_session.query(RawArticle).count() == 5
