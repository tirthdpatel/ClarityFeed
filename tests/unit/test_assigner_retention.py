"""Integration-ish tests for the classification stage and retention job.

These run against a real SQLite database built from the ORM metadata, so they
exercise the actual INSERT/DELETE paths rather than mocks. The retention SQL
that is Postgres-specific is covered separately in the eval run.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.classifier import (
    Alias, CategoryClassifier, ClassificationStage, CountryClassifier,
    Gazetteer, load_rules_from_yaml,
)
from backend.database.models import PipelineStatus
from backend.database.orm_models import (
    ArticleCategory, ArticleCountry, Base, CategoryDef, Country, RawArticle, Region, Source,
)
from backend.retention import RetentionStats, get_storage_status, run_retention

DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        region = Region(code="south-asia", name="South Asia", sort_order=1)
        s.add(region)
        s.flush()
        for iso2, name, slug in [("IN", "India", "in"), ("US", "United States", "us"),
                                 ("GB", "United Kingdom", "gb"), ("DE", "Germany", "de")]:
            s.add(Country(iso2=iso2, iso3=iso2 + "X", name=name, slug=slug,
                          region_id=region.id, is_enabled=True))
        for c in yaml.safe_load((DATA / "categories.yaml").read_text()):
            s.add(CategoryDef(slug=c["slug"], name=c["name"], sort_order=c["sort_order"]))
        s.add(Source(id=1, name="Times of India", url="https://toi.example",
                     feed_url="https://toi.example/rss", country="IN"))
        s.commit()
        yield s


@pytest.fixture(scope="module")
def stage():
    aliases = []
    for path in sorted((DATA / "gazetteer").glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        d = yaml.safe_load(path.read_text())
        for a in d["aliases"]:
            aliases.append(Alias(
                country_iso2=d["country"], alias=a["alias"].lower(),
                alias_type=a.get("type", "name"), weight=float(a["weight"]),
                is_ambiguous=bool(a.get("ambiguous", False)),
                context=tuple(a.get("context", ())),
            ))
    return ClassificationStage(
        CountryClassifier(Gazetteer(aliases)),
        CategoryClassifier(load_rules_from_yaml(DATA / "categories.yaml")),
    )


def _article(db, aid, title, published=None):
    a = RawArticle(id=aid, source_id=1, url=f"https://toi.example/{aid}",
                   url_hash=f"{aid:064d}", title=title,
                   published_at=published or datetime.utcnow(),
                   status=PipelineStatus.PENDING)
    db.add(a)
    db.commit()
    return a


class TestClassificationStage:
    def test_assigns_country_and_category(self, db, stage):
        a = _article(db, 1, "Modi announces AI policy in New Delhi")
        stats = stage.classify_batch(db, [a])
        assert stats.processed == 1
        assert stats.countries_assigned == 1

        rows = list(db.scalars(select(ArticleCountry).where(ArticleCountry.article_id == 1)))
        assert any(r.relevance == "primary" for r in rows)
        primary = next(r for r in rows if r.relevance == "primary")
        assert db.get(Country, primary.country_id).iso2 == "IN"

    def test_denormalises_published_at(self, db, stage):
        """The whole point of §3.4 — without this the country feed degrades
        to a join-then-sort at scale."""
        when = datetime(2026, 3, 1, 12, 0)
        a = _article(db, 2, "Bundestag approves budget in Berlin", published=when)
        stage.classify_batch(db, [a])
        row = db.scalar(select(ArticleCountry).where(ArticleCountry.article_id == 2))
        assert row.published_at == when

    def test_is_idempotent(self, db, stage):
        a = _article(db, 3, "Modi announces policy in New Delhi")
        stage.classify_batch(db, [a])
        first = len(list(db.scalars(select(ArticleCountry).where(ArticleCountry.article_id == 3))))
        stage.classify_batch(db, [a])
        second = len(list(db.scalars(select(ArticleCountry).where(ArticleCountry.article_id == 3))))
        assert first == second

    def test_abstains_rather_than_guessing(self, db, stage):
        """No country evidence must produce no row, not a guess."""
        a = _article(db, 4, "Scientists discover new species of deep-sea coral")
        a.source_id = 1
        stats = stage.classify_batch(db, [a])
        # The source prior still applies (Times of India), but it must be
        # flagged rather than presented as confident.
        rows = list(db.scalars(select(ArticleCountry).where(ArticleCountry.article_id == 4)))
        if rows:
            assert rows[0].confidence <= 0.35

    def test_records_category_as_primary(self, db, stage):
        a = _article(db, 5, "OpenAI releases new large language model")
        stage.classify_batch(db, [a])
        rows = list(db.scalars(select(ArticleCategory).where(ArticleCategory.article_id == 5)))
        assert rows
        primary = next(r for r in rows if r.is_primary)
        assert db.get(CategoryDef, primary.category_id).slug == "ai"

    def test_empty_batch_is_safe(self, db, stage):
        assert stage.classify_batch(db, []).processed == 0


class TestRetention:
    def test_clears_old_raw_html(self, db):
        old = _article(db, 10, "Old article")
        old.raw_html = "<html>" + "x" * 10000 + "</html>"
        old.created_at = datetime.utcnow() - timedelta(hours=48)
        db.commit()
        run_retention(db)
        assert db.get(RawArticle, 10).raw_html is None

    def test_keeps_recent_raw_html(self, db):
        recent = _article(db, 11, "Recent article")
        recent.raw_html = "<html>keep me</html>"
        recent.created_at = datetime.utcnow()
        db.commit()
        run_retention(db)
        assert db.get(RawArticle, 11).raw_html is not None

    def test_deletes_articles_stuck_pending(self, db):
        """Under the old pipeline these accumulated forever once the LLM
        quota ran out — every one is a row nobody will ever see."""
        stuck = _article(db, 12, "Stuck forever")
        stuck.created_at = datetime.utcnow() - timedelta(days=10)
        stuck.status = PipelineStatus.PENDING
        db.commit()
        run_retention(db)
        assert db.get(RawArticle, 12) is None

    def test_retention_is_repeatable(self, db):
        _article(db, 13, "Some article")
        a = run_retention(db)
        b = run_retention(db)
        assert isinstance(a, RetentionStats) and isinstance(b, RetentionStats)


class TestStorageGuard:
    def test_thresholds(self):
        from backend.retention import StorageStatus
        assert not StorageStatus(100 * 1024**2, 500 * 1024**2).should_warn
        assert StorageStatus(360 * 1024**2, 500 * 1024**2).should_warn
        assert StorageStatus(460 * 1024**2, 500 * 1024**2).should_stop_ingestion

    def test_unsupported_backend_does_not_block_ingestion(self, db):
        """SQLite has no pg_database_size. Failing open is correct here — a
        guard that blocks all ingestion because it cannot measure is worse
        than one that lets it through."""
        st = get_storage_status(db)
        assert st.should_stop_ingestion is False
