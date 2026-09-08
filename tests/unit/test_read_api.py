"""Tests for the public read API.

The point of most of these is not that the endpoints work — it is that the
permission gate and the publish barrier have no bypass. A gate that holds for
the paths someone remembered to check is not a gate.

RUNS ON TWO DATABASES. By default this uses in-memory SQLite, which is fast
and needs nothing installed. Set TEST_DATABASE_URL and the identical tests run
against PostgreSQL instead; CI does exactly that (.github/workflows/tests.yml).

That is not belt-and-braces. SQLite accepts SQL that PostgreSQL rejects, and
this suite passed in full while GET /articles returned 500 against a real
database — `SELECT DISTINCT` with an ORDER BY over a COALESCE, which Postgres
refuses and SQLite waves through. The bug reached a deployed cluster before
anything caught it. Running the same assertions on the dialect that actually
serves production is the cheapest way to not repeat that.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.main import app
from backend.database.orm_models import (
    Base,
    CategoryDef,
    Country,
    RawArticle,
    Source,
    SourcePermission,
    Summary,
)
from backend.database.session import get_db


@pytest.fixture
def db_session():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if test_url:
        # Real PostgreSQL. Dropping first makes the fixture idempotent when a
        # previous run died before its teardown.
        engine = create_engine(test_url)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    else:
        # StaticPool: without it each checkout opens a fresh :memory: database
        # and the schema created above is simply not there.
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    session.add_all(
        [
            Source(id=1, name="Test Wire", url="https://pub.example",
                   feed_url="https://pub.example/rss", language="en", is_active=True),
            Source(id=2, name="Other Wire", url="https://two.example",
                   feed_url="https://two.example/rss", language="fr", is_active=True),
            Source(id=3, name="Dead Wire", url="https://three.example",
                   feed_url="https://three.example/rss", language="en", is_active=False),
        ]
    )

    now = datetime(2026, 9, 1, 12, 0, 0)
    session.add_all(
        [
            RawArticle(id=1, source_id=1, url="https://pub.example/a", url_hash="h1",
                       title="Published one", summary_from_feed="A description.",
                       published_at=now, ingest_status="PUBLISHED"),
            RawArticle(id=2, source_id=1, url="https://pub.example/b", url_hash="h2",
                       title="Published two", summary_from_feed="Another.",
                       published_at=now - timedelta(hours=1), ingest_status="PUBLISHED"),
            RawArticle(id=3, source_id=2, url="https://two.example/c", url_hash="h3",
                       title="French one", summary_from_feed="Trois.",
                       published_at=now - timedelta(hours=2), ingest_status="PUBLISHED"),
            # Mid-pipeline: must never be visible.
            RawArticle(id=4, source_id=1, url="https://pub.example/d", url_hash="h4",
                       title="Not yet published", published_at=now,
                       ingest_status="CLEANED"),
            # Published, but its source was deactivated (e.g. a takedown).
            RawArticle(id=5, source_id=3, url="https://three.example/e", url_hash="h5",
                       title="From a disabled source", published_at=now,
                       ingest_status="PUBLISHED"),
        ]
    )
    session.commit()

    app.dependency_overrides[get_db] = lambda: session
    yield session
    app.dependency_overrides.clear()
    session.close()
    if os.environ.get("TEST_DATABASE_URL"):
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(db_session):
    return TestClient(app)


# -- the publish barrier ----------------------------------------------------


def test_only_published_articles_are_listed(client):
    ids = {a["id"] for a in client.get("/articles").json()["articles"]}
    assert ids == {1, 2, 3}, "the barrier or the is_active filter leaked a row"


def test_unpublished_article_is_404_not_403(client):
    # Same response as a nonexistent id: the pipeline state is not the
    # reader's business.
    assert client.get("/articles/4").status_code == 404


def test_inactive_source_article_is_hidden(client):
    assert client.get("/articles/5").status_code == 404


def test_takedown_hides_articles_immediately(client, db_session):
    """Setting the column is the whole remedy — purging rows can follow later."""
    assert client.get("/articles/1").status_code == 200

    source = db_session.get(Source, 1)
    source.takedown_requested_at = datetime(2026, 9, 8, 9, 0, 0)
    source.takedown_requested_by = "legal@pub.example"
    db_session.commit()

    assert client.get("/articles/1").status_code == 404
    ids = {a["id"] for a in client.get("/articles").json()["articles"]}
    assert ids == {3}, "a taken-down publisher was still being served"


def test_takedown_stops_us_fetching_the_feed(db_session):
    """The publisher asked us to stop, which means stop requesting — not
    merely stop displaying."""
    from backend.collector.feed_sources import FeedSourceRepository

    repo = FeedSourceRepository(db_session)
    assert 1 in {s.id for s in repo.get_active_sources()}

    db_session.get(Source, 1).takedown_requested_at = datetime(2026, 9, 8)
    db_session.commit()

    assert 1 not in {s.id for s in repo.get_active_sources()}


# -- the permission gate ----------------------------------------------------


def test_unreviewed_source_never_serves_content(client):
    """No source_permissions row means restrictive, which means no body."""
    for article in client.get("/articles").json()["articles"]:
        assert article["content"] is None
        assert article["imageUrl"] is None


def test_every_article_carries_attribution(client):
    for article in client.get("/articles").json()["articles"]:
        assert article["attribution"]["readOriginalUrl"] == article["url"]
        assert article["source"]["name"]


def test_revoking_summary_permission_takes_effect_immediately(client, db_session):
    """The serialize gate is re-applied per request, not baked in at ingest."""
    db_session.add(
        Summary(raw_article_id=1, summary_text="A summary.", tldr="A summary.",
                model_name="test-model")
    )
    db_session.commit()

    def summary_for(article_id):
        return client.get(f"/articles/{article_id}").json()["summary"]

    assert summary_for(1) is not None

    db_session.add(SourcePermission(source_id=1, can_generate_summary=False))
    db_session.commit()

    assert summary_for(1) is None, "a revoked permission survived until re-crawl"


def test_lowering_description_limit_retrims_stored_text(client, db_session):
    """Tightening max_description_chars must shorten what is served."""
    db_session.add(SourcePermission(source_id=1, max_description_chars=6))
    db_session.commit()

    body = client.get("/articles/1").json()
    assert body["description"] == "A…", body["description"]


def test_summary_is_labelled_as_ai_generated(client, db_session):
    """A summary can only be rendered unlabelled by deliberately discarding
    the label — it is not a separate field the frontend might forget."""
    db_session.add(
        Summary(raw_article_id=1, summary_text="Machine text.", tldr="Machine text.",
                model_name="llama-x")
    )
    db_session.commit()

    summary = client.get("/articles/1").json()["summary"]
    assert summary["aiGenerated"] is True
    assert "may contain errors" in summary["disclaimer"].lower()
    assert summary["model"] == "llama-x"


# -- pagination -------------------------------------------------------------


def test_cursor_pagination_walks_every_article_once(client):
    seen, cursor = [], None
    for _ in range(5):
        url = f"/articles?limit=1{f'&cursor={cursor}' if cursor else ''}"
        body = client.get(url).json()
        seen.extend(a["id"] for a in body["articles"])
        cursor = body["nextCursor"]
        if not cursor:
            break

    assert seen == [1, 2, 3], "pagination skipped or repeated a row"


def test_last_page_reports_no_more(client):
    body = client.get("/articles?limit=50").json()
    assert body["hasMore"] is False
    assert body["nextCursor"] is None


def test_malformed_cursor_is_rejected_not_silently_reset(client):
    # Returning page one would restart a reader's scroll from the top with no
    # indication anything went wrong.
    assert client.get("/articles?cursor=not-base64!!").status_code == 400


def test_articles_are_newest_first(client):
    stamps = [a["publishedAt"] for a in client.get("/articles").json()["articles"]]
    assert stamps == sorted(stamps, reverse=True)


# -- filters ----------------------------------------------------------------


def test_language_filter(client):
    ids = {a["id"] for a in client.get("/articles?language=fr").json()["articles"]}
    assert ids == {3}


def test_source_filter_rejects_non_numeric(client):
    assert client.get("/articles?source=bbc").status_code == 400


def test_unknown_country_returns_empty_not_error(client):
    body = client.get("/articles?country=zz").json()
    assert body["articles"] == []


# -- headers ----------------------------------------------------------------


def test_reads_are_cacheable(client):
    assert "s-maxage" in client.get("/articles").headers["cache-control"]


def test_every_response_asserts_noindex(client):
    """ROADMAP §1 — the JSON feed is headline data and would otherwise be
    indexable on its own, which a frontend meta tag cannot prevent."""
    for path in ("/articles", "/articles/1", "/countries", "/health"):
        assert "noindex" in client.get(path).headers["x-robots-tag"]


# -- reference data ---------------------------------------------------------


def test_reference_endpoints_hide_disabled_rows(client, db_session):
    db_session.add_all(
        [
            Country(id=1, iso2="GB", iso3="GBR", name="United Kingdom",
                    slug="united-kingdom", is_enabled=True),
            Country(id=2, iso2="XX", iso3="XXX", name="Nowhere",
                    slug="nowhere", is_enabled=False),
            CategoryDef(id=1, slug="world", name="World", is_enabled=True),
            CategoryDef(id=2, slug="hidden", name="Hidden", is_enabled=False),
        ]
    )
    db_session.commit()

    assert [c["iso2"] for c in client.get("/countries").json()] == ["GB"]
    assert [c["slug"] for c in client.get("/categories").json()] == ["world"]
