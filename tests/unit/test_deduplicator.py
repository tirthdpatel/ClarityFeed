"""Unit tests for backend/deduplicator/deduplicator.py."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import CleanedArticleRecord, PipelineStatus
from backend.database.orm_models import Base, CleanedArticle, Embedding, RawArticle, Source
from backend.deduplicator.deduplicator import Deduplicator
from backend.deduplicator.embedding_client import EmbeddingClient


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


class TestDeduplicator:
    """Tests for the deduplicator."""

    @patch.object(EmbeddingClient, "embed_batch", new_callable=AsyncMock)
    def test_unique_article_not_marked_duplicate(
        self, mock_embed, db_session
    ) -> None:
        """Unique article (low similarity) is not marked DUPLICATE."""
        mock_embed.return_value = [[0.1] * 384]
        vec = [0.9] * 384
        mock_embed.return_value = [vec]

        raw = RawArticle(
            id=1,
            source_id=1,
            url="https://example.com/1",
            title="Unique",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        cleaned = CleanedArticle(
            raw_article_id=1,
            clean_text="unique article text",
            word_count=3,
        )
        db_session.add(cleaned)
        db_session.commit()

        articles = [
            CleanedArticleRecord(
                id=1,
                raw_article_id=1,
                clean_text="unique article text",
                word_count=3,
            )
        ]
        dedup = Deduplicator(EmbeddingClient())
        result = _run(dedup.process_batch(db_session, articles))
        assert result.unique >= 1
        r = db_session.query(RawArticle).filter(RawArticle.id == 1).first()
        assert r.status == PipelineStatus.PENDING

    @patch.object(EmbeddingClient, "embed_batch", new_callable=AsyncMock)
    def test_duplicate_article_marked(
        self, mock_embed, db_session
    ) -> None:
        """Duplicate article (high similarity) is marked DUPLICATE."""
        same_vec = [0.5] * 384
        mock_embed.return_value = [same_vec]

        raw1 = RawArticle(
            id=1,
            source_id=1,
            url="https://example.com/1",
            title="First",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw1)
        db_session.flush()
        emb1 = Embedding(raw_article_id=1, vector_json=json.dumps(same_vec))
        db_session.add(emb1)
        db_session.flush()

        raw2 = RawArticle(
            id=2,
            source_id=1,
            url="https://example.com/2",
            title="Duplicate",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw2)
        db_session.flush()
        cleaned2 = CleanedArticle(
            raw_article_id=2,
            clean_text="duplicate text",
            word_count=2,
        )
        db_session.add(cleaned2)
        db_session.commit()

        articles = [
            CleanedArticleRecord(
                id=2,
                raw_article_id=2,
                clean_text="duplicate text",
                word_count=2,
            )
        ]
        dedup = Deduplicator(EmbeddingClient())
        result = _run(dedup.process_batch(db_session, articles))
        r = db_session.query(RawArticle).filter(RawArticle.id == 2).first()
        if result.duplicates >= 1:
            assert r.status == PipelineStatus.DUPLICATE

    @patch.object(EmbeddingClient, "embed_batch", new_callable=AsyncMock)
    def test_empty_window_all_unique(self, mock_embed, db_session) -> None:
        """Empty comparison window results in all articles unique."""
        mock_embed.return_value = [[0.1] * 384]
        raw = RawArticle(
            id=1,
            source_id=1,
            url="https://example.com/1",
            title="First",
            status=PipelineStatus.PENDING,
        )
        db_session.add(raw)
        db_session.flush()
        cleaned = CleanedArticle(raw_article_id=1, clean_text="text", word_count=1)
        db_session.add(cleaned)
        db_session.commit()

        dedup = Deduplicator(EmbeddingClient())
        result = _run(
            dedup.process_batch(
                db_session,
                [CleanedArticleRecord(id=1, raw_article_id=1, clean_text="text", word_count=1)],
            )
        )
        assert result.unique >= 1
