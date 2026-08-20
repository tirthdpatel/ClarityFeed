"""Tests for source health tracking and the circuit breaker — V2 §A8.

The finding that motivated this: two of ten seed sources were dead and
nothing noticed. The failure mode is a feed that answers HTTP 200 with zero
entries forever, so these tests centre on that case rather than on timeouts.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.database.orm_models import Base, Source, SourceHealth
from backend.source_health import (
    BREAKER_THRESHOLD, WARN_THRESHOLD, FetchOutcome, failing_sources, record_outcome,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Source(id=1, name="Test Feed", url="https://t.example",
                     feed_url="https://t.example/rss", is_active=True,
                     consecutive_failures=0))
        s.commit()
        yield s


class TestEffectiveFailure:
    def test_http_200_with_zero_articles_is_a_failure(self):
        """The whole point of A8 — this is what killed the Reuters feed
        without anyone noticing."""
        assert FetchOutcome(1, ok=True, http_status=200, articles_returned=0).is_effective_failure

    def test_http_200_with_articles_is_success(self):
        assert not FetchOutcome(1, ok=True, http_status=200, articles_returned=12).is_effective_failure

    def test_transport_error_is_a_failure(self):
        assert FetchOutcome(1, ok=False, error_type="timeout").is_effective_failure


class TestBreaker:
    def _fail(self, db, n):
        for _ in range(n):
            record_outcome(db, FetchOutcome(1, ok=True, http_status=200, articles_returned=0))

    def test_counts_consecutive_failures(self, db):
        self._fail(db, 2)
        assert db.get(Source, 1).consecutive_failures == 2
        assert db.get(Source, 1).is_active is True

    def test_disables_at_threshold(self, db):
        self._fail(db, BREAKER_THRESHOLD)
        src = db.get(Source, 1)
        assert src.is_active is False
        assert src.consecutive_failures >= BREAKER_THRESHOLD

    def test_reports_when_it_disables(self, db):
        for i in range(BREAKER_THRESHOLD - 1):
            assert record_outcome(db, FetchOutcome(1, ok=True, articles_returned=0)) is False
        assert record_outcome(db, FetchOutcome(1, ok=True, articles_returned=0)) is True

    def test_success_resets_the_counter(self, db):
        """A feed that works again is healthy. Carrying old failures forward
        would eventually trip the breaker on a source that has been fine for
        weeks."""
        self._fail(db, BREAKER_THRESHOLD - 1)
        record_outcome(db, FetchOutcome(1, ok=True, http_status=200, articles_returned=5))
        src = db.get(Source, 1)
        assert src.consecutive_failures == 0
        assert src.is_active is True
        assert src.last_success_at is not None

    def test_records_the_reason(self, db):
        record_outcome(db, FetchOutcome(1, ok=True, http_status=200, articles_returned=0))
        assert "0 articles" in db.get(Source, 1).last_error

    def test_writes_a_health_row_each_time(self, db):
        self._fail(db, 3)
        rows = list(db.scalars(select(SourceHealth).where(SourceHealth.source_id == 1)))
        assert len(rows) == 3
        assert all(r.error_type == "empty_feed" for r in rows)

    def test_failing_sources_view(self, db):
        self._fail(db, WARN_THRESHOLD)
        assert [s.id for s in failing_sources(db)] == [1]

    def test_unknown_source_does_not_raise(self, db):
        assert record_outcome(db, FetchOutcome(999, ok=False)) is False
