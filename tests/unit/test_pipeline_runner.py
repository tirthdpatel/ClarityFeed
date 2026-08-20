"""Tests for the ingestion orchestrator — above all, for the publish barrier.

The barrier is the one property in this system that is worth a test suite of
its own, because the failure it prevents already happened once: an LLM
deprecation stopped the pipeline mid-run and the site emptied out. The
invariant is narrow and absolute —

    once ingest_status is PUBLISHED, nothing below the barrier may change it

— and `test_enrichment_failure_does_not_unpublish` is the test that says so.

These run against a real SQLite database built from the ORM metadata, so the
INSERT and UPDATE paths are exercised rather than mocked. Only the three
collaborators that reach the network are faked.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.database.orm_models import (
    Base, CategoryDef, CleanedArticle, Country, Embedding, IngestionRun,
    RawArticle, Region, Source, SourceHealth, SourcePermission,
)
from backend.pipeline.runner import (
    ENRICH_DONE, ENRICH_FAILED, INGEST_PENDING, INGEST_PUBLISHED,
    RUN_ABORTED, RUN_OK, RUN_PARTIAL, PipelineConfig, PipelineRunner,
)
from backend.retention import StorageStatus

DATA = Path(__file__).resolve().parents[2] / "data"


# ---------------------------------------------------------------------------
# Fakes. Each returns an awaitable without being a coroutine function itself,
# which keeps the doubles readable and the tests synchronous.
# ---------------------------------------------------------------------------


def _done(value):
    """An already-resolved awaitable carrying *value*."""
    return asyncio.sleep(0, result=value)


class FakeRSS:
    """Serves canned entries per feed URL, or raises."""

    def __init__(self, by_feed: dict[str, list[dict]], raises: set[str] = frozenset()):
        self.by_feed = by_feed
        self.raises = raises
        self.calls: list[str] = []

    def fetch_feed(self, source):
        self.calls.append(source.feed_url)
        if source.feed_url in self.raises:
            raise ConnectionError("upstream refused the connection")
        return _done(list(self.by_feed.get(source.feed_url, [])))


class FakeEmbedder:
    model_name = "fake-minilm"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def embed_batch(self, texts):
        if self.fail:
            raise RuntimeError("torch is not installed")
        return [[0.01 * (i + 1)] * 384 for i, _ in enumerate(texts)]


class FakeBodies:
    """Writes cleaned_articles rows, as the real extractor does.

    This matters: enrichment only has candidates where a body exists, so a
    double that skipped the write would make the barrier test pass without
    the summariser ever being called.
    """

    class Result:
        extracted = 0

    def fetch_and_extract_batch(self, db, records):
        for rec in records:
            db.add(CleanedArticle(raw_article_id=rec.id, clean_text="body " * 80,
                                  word_count=80))
        db.commit()
        r = self.Result()
        r.extracted = len(records)
        return _done(r)


class ExplodingSummarizer:
    """Stands in for the outage that emptied the site."""

    def summarize_batch(self, db, records):
        raise RuntimeError("Groq: model has been decommissioned")


class OkSummarizer:
    class Result:
        summarized = 0
        failed = 0

    def summarize_batch(self, db, records):
        r = self.Result()
        r.summarized = len(records)
        return _done(r)


def entry(title: str, url: str, summary: str = "") -> dict:
    return {
        "title": title,
        "url": url,
        "summary": summary,
        "published": datetime(2026, 8, 19, 9, 0, 0),
        "source_name": "Test Wire",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        region = Region(code="south-asia", name="South Asia", sort_order=1)
        s.add(region)
        s.flush()
        for iso2, name, slug in [("IN", "India", "in"), ("US", "United States", "us")]:
            s.add(Country(iso2=iso2, iso3=iso2 + "X", name=name, slug=slug,
                          region_id=region.id, is_enabled=True))
        for c in yaml.safe_load((DATA / "categories.yaml").read_text()):
            s.add(CategoryDef(slug=c["slug"], name=c["name"], sort_order=c["sort_order"]))
        s.add(Source(id=1, name="Test Wire", url="https://wire.example",
                     feed_url="https://wire.example/rss", country="IN", is_active=True))
        s.add(Source(id=2, name="Second Wire", url="https://two.example",
                     feed_url="https://two.example/rss", country="US", is_active=True))
        s.commit()
        yield s


def make_runner(config=None, **kw):
    kw.setdefault("rss_reader", FakeRSS({}))
    kw.setdefault("embedder", FakeEmbedder())
    kw.setdefault("body_reader", FakeBodies())
    kw.setdefault("summarizer", OkSummarizer())
    return PipelineRunner(config or PipelineConfig(), **kw)


# ===========================================================================
# THE BARRIER
# ===========================================================================


def test_enrichment_failure_does_not_unpublish(db):
    """The invariant. An LLM outage costs a summary, never an article.

    This is the regression test for the incident described in V2 §A3: the
    old pipeline published nothing when summarisation failed, so a vendor
    deprecation emptied the site.
    """
    rss = FakeRSS({"https://wire.example/rss": [entry("Delhi floods worsen", "https://a.example/1")]})
    # Full text licensed, so a body is extracted and the article becomes a
    # real enrichment candidate. Without it the summariser is never reached
    # and this test would pass without testing anything.
    db.add(SourcePermission(source_id=1, can_store_full_text=True,
                            reviewed_at=datetime.utcnow()))
    db.commit()
    runner = make_runner(rss_reader=rss, summarizer=ExplodingSummarizer())

    report = runner.run(db)

    assert report.articles_published == 1
    rows = list(db.scalars(select(RawArticle)))
    assert [r.ingest_status for r in rows] == [INGEST_PUBLISHED]
    # Enrichment recorded its own failure without touching ingest_status.
    assert rows[0].enrichment_status == ENRICH_FAILED
    # And the run itself is not a failure: the barrier was reached.
    assert report.ok
    assert any("Groq" in e for e in report.errors)


def test_embedding_failure_does_not_block_publication(db):
    """Embeddings are above the barrier but are not required to cross it.

    Losing them costs deduplication quality on this batch. It must not cost
    visibility — the article is still a headline, an excerpt and a link.
    """
    rss = FakeRSS({"https://wire.example/rss": [entry("Mumbai rail delays", "https://a.example/2")]})
    runner = make_runner(rss_reader=rss, embedder=FakeEmbedder(fail=True))

    report = runner.run(db)

    assert report.articles_published == 1
    assert report.embedded == 0
    assert db.scalar(select(Embedding)) is None
    assert report.ok


def test_successful_run_marks_enrichment_done(db):
    """With a body present and summarisation working, enrichment completes."""
    rss = FakeRSS({"https://wire.example/rss": [entry("Chennai port reopens", "https://a.example/3")]})
    db.add(SourcePermission(source_id=1, can_store_full_text=True, reviewed_at=datetime.utcnow()))
    db.commit()
    runner = make_runner(rss_reader=rss)

    report = runner.run(db)

    art = db.scalar(select(RawArticle))
    assert report.articles_published == 1
    assert art.ingest_status == INGEST_PUBLISHED
    assert art.enrichment_status == ENRICH_DONE
    assert report.enriched == 1


# ===========================================================================
# Storage guard
# ===========================================================================


def test_storage_guard_aborts_before_writing(db, monkeypatch):
    """Above the stop mark the run refuses rather than failing mid-write.

    A half-written article is worse than no article: invisible to the
    reader and invisible to the operator (§A6).
    """
    import backend.pipeline.runner as mod

    monkeypatch.setattr(
        mod, "get_storage_status",
        lambda s, **kw: StorageStatus(used_bytes=480 * 1024 * 1024, limit_bytes=500 * 1024 * 1024),
    )
    rss = FakeRSS({"https://wire.example/rss": [entry("Anything", "https://a.example/4")]})
    runner = make_runner(rss_reader=rss)

    report = runner.run(db)

    assert report.status == RUN_ABORTED
    assert not report.ok
    assert db.scalar(select(RawArticle)) is None
    assert rss.calls == []          # no feed was even read
    run_row = db.scalar(select(IngestionRun))
    assert run_row.status == RUN_ABORTED


# ===========================================================================
# Collection, dedup, health
# ===========================================================================


def test_duplicate_urls_collapse_across_sources(db):
    """The same story from two feeds is one article.

    Canonicalisation means the tracking-parameter variant hashes the same,
    which is the case a raw-string comparison used to miss (§A4).
    """
    rss = FakeRSS({
        "https://wire.example/rss": [entry("Shared story", "https://x.example/story")],
        "https://two.example/rss": [entry("Shared story", "https://x.example/story?utm_source=twitter")],
    })
    report = make_runner(rss_reader=rss).run(db)

    assert report.articles_new == 1
    assert report.articles_duplicate == 1
    assert len(list(db.scalars(select(RawArticle)))) == 1


def test_empty_feed_counts_as_a_failure(db):
    """A 200 with no entries is the failure mode A8 exists to catch."""
    rss = FakeRSS({"https://wire.example/rss": [entry("Only story", "https://a.example/5")]})
    report = make_runner(rss_reader=rss).run(db)

    assert report.sources_ok == 1
    assert report.sources_failed == 1           # the second feed returned nothing
    assert report.status == RUN_PARTIAL
    health = {h.source_id: h for h in db.scalars(select(SourceHealth))}
    assert health[2].ok is False
    assert health[2].error_type == "empty_feed"
    assert db.get(Source, 2).consecutive_failures == 1


def test_one_dead_source_does_not_stop_the_run(db):
    rss = FakeRSS(
        {"https://two.example/rss": [entry("Still fine", "https://a.example/6")]},
        raises={"https://wire.example/rss"},
    )
    report = make_runner(rss_reader=rss).run(db)

    assert report.articles_published == 1
    assert report.sources_failed == 1
    assert report.ok


# ===========================================================================
# Permission gate
# ===========================================================================


def test_gate_runs_before_the_insert(db):
    """A description the publisher does not licence is never stored.

    Not stored-then-cleared: the gate exists so the field never reaches the
    database in the first place.
    """
    db.add(SourcePermission(source_id=1, can_store_description=False,
                            reviewed_at=datetime.utcnow()))
    db.commit()
    rss = FakeRSS({"https://wire.example/rss": [
        entry("Headline only", "https://a.example/7", summary="Body text we may not keep"),
    ]})
    report = make_runner(rss_reader=rss).run(db)

    art = db.scalar(select(RawArticle).where(RawArticle.source_id == 1))
    assert art.title == "Headline only"
    assert art.summary_from_feed is None
    assert report.articles_gated == 1


def test_unreviewed_source_gets_the_restrictive_default(db):
    """No permission row means headline, short excerpt, link — nothing else."""
    long_summary = "word " * 200
    rss = FakeRSS({"https://wire.example/rss": [
        entry("Long excerpt", "https://a.example/8", summary=long_summary),
    ]})
    make_runner(rss_reader=rss).run(db)

    art = db.scalar(select(RawArticle).where(RawArticle.source_id == 1))
    assert len(art.summary_from_feed) <= 301      # 300 + the ellipsis
    assert art.summary_from_feed.endswith("…")


# ===========================================================================
# Dry run
# ===========================================================================


def test_dry_run_writes_nothing_and_records_no_health(db):
    """A rehearsal must not move a real source toward the circuit breaker."""
    rss = FakeRSS({"https://wire.example/rss": [entry("Would be new", "https://a.example/9")]})
    runner = make_runner(PipelineConfig(dry_run=True), rss_reader=rss)

    report = runner.run(db)

    assert report.entries_seen == 1
    assert db.scalar(select(RawArticle)) is None
    assert db.scalar(select(SourceHealth)) is None
    assert db.get(Source, 2).consecutive_failures == 0
    assert report.ok


def test_limit_caps_new_articles(db):
    rss = FakeRSS({"https://wire.example/rss": [
        entry(f"Story {i}", f"https://a.example/l{i}") for i in range(10)
    ]})
    report = make_runner(PipelineConfig(limit=3), rss_reader=rss).run(db)

    assert report.articles_new == 3
    assert report.articles_published == 3


def test_skip_enrichment_still_publishes(db):
    rss = FakeRSS({"https://wire.example/rss": [entry("Fast path", "https://a.example/10")]})
    report = make_runner(
        PipelineConfig(skip_enrichment=True), rss_reader=rss,
        summarizer=ExplodingSummarizer(),
    ).run(db)

    assert report.articles_published == 1
    assert db.scalar(select(RawArticle)).ingest_status == INGEST_PUBLISHED


# ===========================================================================
# Run bookkeeping
# ===========================================================================


def test_ingestion_run_row_is_opened_and_closed(db):
    rss = FakeRSS({"https://wire.example/rss": [entry("Bookkeeping", "https://a.example/11")]})
    report = make_runner(rss_reader=rss).run(db)

    row = db.scalar(select(IngestionRun))
    assert row.id == report.run_id
    assert row.finished_at is not None
    assert row.articles_published == 1
    assert row.status == report.status


def test_no_active_sources_is_reported_not_crashed(db):
    for s in db.scalars(select(Source)):
        s.is_active = False
    db.commit()

    report = make_runner().run(db)

    assert report.sources_attempted == 0
    assert any("No active sources" in e for e in report.errors)
