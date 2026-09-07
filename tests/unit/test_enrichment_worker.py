"""Unit tests for backend/enrichment/worker.py.

The behaviour under test is the claim/complete cycle and, above all, the
guarantee the publish barrier exists to provide: nothing in this module may
change ingest_status. A summary can be lost; a published article cannot.

These run on SQLite, so SKIP LOCKED is not exercised here — see
_supports_skip_locked() for why that degradation is safe. The concurrency
property it provides is a PostgreSQL guarantee, not something a single-process
SQLite test could demonstrate anyway.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.orm_models import (
    Base,
    CleanedArticle,
    RawArticle,
    Source,
    SourcePermission,
)
from backend.enrichment.worker import (
    ENRICH_DONE,
    ENRICH_FAILED,
    ENRICH_PENDING,
    ENRICH_SKIPPED,
    INGEST_PUBLISHED,
    EnrichmentWorker,
    claim_pending,
    set_status,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        Source(id=1, name="Test", url="https://example.com", feed_url="https://example.com/rss")
    )
    session.commit()
    yield session
    session.close()


def add_article(
    session,
    article_id: int,
    *,
    ingest_status: str = INGEST_PUBLISHED,
    enrichment_status: str = ENRICH_PENDING,
    cleaned: bool = True,
    source_id: int = 1,
) -> None:
    session.add(
        RawArticle(
            id=article_id,
            source_id=source_id,
            title=f"Article {article_id}",
            url=f"https://example.com/{article_id}",
            url_hash=f"hash{article_id}",
            ingest_status=ingest_status,
            enrichment_status=enrichment_status,
        )
    )
    if cleaned:
        session.add(
            CleanedArticle(
                id=article_id,
                raw_article_id=article_id,
                clean_text="A cleaned body long enough to summarise.",
                word_count=7,
            )
        )
    session.commit()


def allow_summaries(session, source_id: int = 1, allowed: bool = True) -> None:
    session.add(SourcePermission(source_id=source_id, can_generate_summary=allowed))
    session.commit()


# ---------------------------------------------------------------------------
# claiming
# ---------------------------------------------------------------------------


class TestClaimPending:
    def test_claims_published_pending_articles(self, db_session):
        add_article(db_session, 1)
        add_article(db_session, 2)
        claimed = claim_pending(db_session, limit=10)
        assert [c[0] for c in claimed] == [1, 2]

    def test_respects_the_limit(self, db_session):
        for i in range(1, 6):
            add_article(db_session, i)
        assert len(claim_pending(db_session, limit=3)) == 3

    def test_claims_oldest_first(self, db_session):
        # A backlog should drain in the order it accumulated.
        for i in (3, 1, 2):
            add_article(db_session, i)
        assert [c[0] for c in claim_pending(db_session, limit=10)] == [1, 2, 3]

    def test_ignores_articles_that_have_not_cleared_the_barrier(self, db_session):
        add_article(db_session, 1, ingest_status="CLEANED")
        assert claim_pending(db_session, limit=10) == []

    def test_ignores_already_enriched_articles(self, db_session):
        add_article(db_session, 1, enrichment_status=ENRICH_DONE)
        add_article(db_session, 2, enrichment_status=ENRICH_SKIPPED)
        add_article(db_session, 3, enrichment_status=ENRICH_FAILED)
        assert claim_pending(db_session, limit=10) == []

    def test_ignores_articles_with_no_cleaned_body(self, db_session):
        # Nothing to summarise: most sources do not licence the full text.
        add_article(db_session, 1, cleaned=False)
        assert claim_pending(db_session, limit=10) == []


# ---------------------------------------------------------------------------
# processing
# ---------------------------------------------------------------------------


class TestProcessBatch:
    def test_successful_batch_marks_done(self, db_session):
        add_article(db_session, 1)
        allow_summaries(db_session)

        summarizer = MagicMock()
        summarizer.summarize_batch = MagicMock(
            return_value=_completed(summarized=1, failed=0)
        )
        worker = EnrichmentWorker(summarizer=summarizer)

        with patch("asyncio.run", side_effect=lambda coro: coro):
            assert worker.process_batch(db_session) == 1

        db_session.commit()
        article = db_session.get(RawArticle, 1)
        assert article.enrichment_status == ENRICH_DONE
        assert worker.stats.summarized == 1

    def test_source_without_permission_is_skipped_not_summarized(self, db_session):
        add_article(db_session, 1)
        allow_summaries(db_session, allowed=False)

        summarizer = MagicMock()
        worker = EnrichmentWorker(summarizer=summarizer)
        worker.process_batch(db_session)
        db_session.commit()

        assert db_session.get(RawArticle, 1).enrichment_status == ENRICH_SKIPPED
        summarizer.summarize_batch.assert_not_called()

    def test_source_with_no_permission_row_falls_back_to_defaults(self, db_session):
        """A missing row resolves to Permissions.restrictive().

        Note that "restrictive" still permits summarising: the class docstring
        states the defaults deliberately mirror the DB column defaults, and
        can_generate_summary defaults to True. What an unreviewed source is
        denied is full-text storage, not summarisation. This test pins that
        behaviour so the worker cannot quietly diverge from the runner, which
        resolves permissions the same way.
        """
        add_article(db_session, 1)  # no SourcePermission row

        summarizer = MagicMock()
        summarizer.summarize_batch = MagicMock(
            return_value=_completed(summarized=1, failed=0)
        )
        worker = EnrichmentWorker(summarizer=summarizer)
        with patch("asyncio.run", side_effect=lambda coro: coro):
            worker.process_batch(db_session)
        db_session.commit()

        assert db_session.get(RawArticle, 1).enrichment_status == ENRICH_DONE
        summarizer.summarize_batch.assert_called_once()

    def test_llm_failure_marks_failed_and_does_not_raise(self, db_session):
        add_article(db_session, 1)
        allow_summaries(db_session)

        summarizer = MagicMock()
        worker = EnrichmentWorker(summarizer=summarizer)

        with patch("asyncio.run", side_effect=RuntimeError("provider is down")):
            claimed = worker.process_batch(db_session)

        assert claimed == 1
        db_session.commit()
        assert db_session.get(RawArticle, 1).enrichment_status == ENRICH_FAILED
        assert worker.stats.errors == 1

    def test_llm_failure_leaves_the_article_published(self, db_session):
        """The barrier guarantee. An LLM outage costs a summary, not an article."""
        add_article(db_session, 1)
        allow_summaries(db_session)

        worker = EnrichmentWorker(summarizer=MagicMock())
        with patch("asyncio.run", side_effect=RuntimeError("provider is down")):
            worker.process_batch(db_session)
        db_session.commit()

        assert db_session.get(RawArticle, 1).ingest_status == INGEST_PUBLISHED

    def test_empty_queue_returns_zero(self, db_session):
        worker = EnrichmentWorker(summarizer=MagicMock())
        assert worker.process_batch(db_session) == 0

    def test_batch_size_is_honoured(self, db_session):
        for i in range(1, 6):
            add_article(db_session, i)
        allow_summaries(db_session)

        summarizer = MagicMock()
        summarizer.summarize_batch = MagicMock(
            return_value=_completed(summarized=2, failed=0)
        )
        worker = EnrichmentWorker(summarizer=summarizer, batch_size=2)
        with patch("asyncio.run", side_effect=lambda coro: coro):
            assert worker.process_batch(db_session) == 2


# ---------------------------------------------------------------------------
# status helper
# ---------------------------------------------------------------------------


class TestSetStatus:
    def test_sets_status_on_many_rows(self, db_session):
        add_article(db_session, 1)
        add_article(db_session, 2)
        set_status(db_session, [1, 2], ENRICH_DONE)
        db_session.commit()
        assert db_session.get(RawArticle, 1).enrichment_status == ENRICH_DONE
        assert db_session.get(RawArticle, 2).enrichment_status == ENRICH_DONE

    def test_empty_id_list_is_a_no_op(self, db_session):
        set_status(db_session, [], ENRICH_DONE)  # must not raise


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_request_shutdown_stops_the_loop(self, db_session):
        worker = EnrichmentWorker(summarizer=MagicMock())
        assert not worker.stopping
        worker.request_shutdown()
        assert worker.stopping

    def test_run_forever_exits_when_asked_to_stop(self):
        from contextlib import contextmanager

        worker = EnrichmentWorker(summarizer=MagicMock())
        calls = {"n": 0}

        @contextmanager
        def factory():
            calls["n"] += 1
            if calls["n"] >= 2:
                worker.request_shutdown()
            db = MagicMock()
            db.bind.dialect.name = "sqlite"
            yield db

        with patch.object(worker, "process_batch", return_value=0):
            with patch.object(worker._shutdown, "wait", return_value=None):
                worker.run_forever(factory)

        assert worker.stopping
        assert calls["n"] >= 2


def _completed(*, summarized: int, failed: int):
    """A stand-in for the coroutine summarize_batch returns.

    process_batch calls asyncio.run() on it, which the tests patch to the
    identity function, so this object is what comes back.
    """
    result = MagicMock()
    result.summarized = summarized
    result.failed = failed
    return result
