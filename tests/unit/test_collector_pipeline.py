"""Unit tests for backend/collector/pipeline.py."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Override DATABASE_URL before importing application code
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from backend.collector.pipeline import CollectorPipeline


def _make_source(source_id: int = 1, name: str = "Test") -> MagicMock:
    source = MagicMock()
    source.id = source_id
    source.name = name
    return source


class TestCollectorPipeline:
    """Tests for the RSS collection pipeline orchestrator."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @patch("backend.collector.pipeline.FeedSourceRepository")
    @patch("backend.collector.pipeline.ArticleInserter")
    @patch("backend.collector.pipeline.RSSFetcher")
    def test_full_cycle(self, mock_fetcher_cls, mock_inserter_cls, mock_repo_cls) -> None:
        """Full collection cycle processes all sources and returns summary."""
        # Setup mock sources
        sources = [_make_source(1, "Source A"), _make_source(2, "Source B")]
        mock_repo_cls.return_value.get_active_sources.return_value = sources

        # Setup mock fetcher — returns articles for each source
        fetcher_instance = MagicMock()
        fetcher_instance.fetch_feed = AsyncMock(return_value=[
            {"title": "Art", "url": "https://x.com/1", "published": None, "summary": "", "source_name": "S"},
        ])
        mock_fetcher_cls.return_value = fetcher_instance

        # Setup mock inserter
        mock_inserter_cls.return_value.insert_new_articles.return_value = 1

        db = MagicMock()
        pipeline = CollectorPipeline()
        result = self._run(pipeline.run_collection_cycle(db))

        assert result["sources_processed"] == 2
        assert result["new_articles"] == 2
        assert result["errors"] == 0
        assert "duration_seconds" in result

    @patch("backend.collector.pipeline.FeedSourceRepository")
    @patch("backend.collector.pipeline.ArticleInserter")
    @patch("backend.collector.pipeline.RSSFetcher")
    def test_single_source_failure_isolation(
        self, mock_fetcher_cls, mock_inserter_cls, mock_repo_cls
    ) -> None:
        """A single source failure does not abort the entire cycle."""
        sources = [_make_source(1, "Good"), _make_source(2, "Bad"), _make_source(3, "Good2")]
        mock_repo_cls.return_value.get_active_sources.return_value = sources

        # Fetcher: succeed, fail, succeed
        call_count = 0

        async def side_effect(source):
            nonlocal call_count
            call_count += 1
            if source.name == "Bad":
                raise Exception("Network error")
            return [{"title": "T", "url": f"https://x.com/{call_count}", "published": None, "summary": "", "source_name": "S"}]

        fetcher_instance = MagicMock()
        fetcher_instance.fetch_feed = AsyncMock(side_effect=side_effect)
        mock_fetcher_cls.return_value = fetcher_instance

        mock_inserter_cls.return_value.insert_new_articles.return_value = 1

        db = MagicMock()
        pipeline = CollectorPipeline()
        result = self._run(pipeline.run_collection_cycle(db))

        assert result["sources_processed"] == 2
        assert result["errors"] == 1
        assert result["new_articles"] == 2

    @patch("backend.collector.pipeline.FeedSourceRepository")
    @patch("backend.collector.pipeline.RSSFetcher")
    def test_summary_dict_structure(self, mock_fetcher_cls, mock_repo_cls) -> None:
        """Summary dict has all required keys."""
        mock_repo_cls.return_value.get_active_sources.return_value = []
        mock_fetcher_cls.return_value = MagicMock()

        db = MagicMock()
        pipeline = CollectorPipeline()
        result = self._run(pipeline.run_collection_cycle(db))

        assert "sources_processed" in result
        assert "new_articles" in result
        assert "errors" in result
        assert "duration_seconds" in result
        assert isinstance(result["duration_seconds"], float)
