"""GNews collection.

The interesting behaviour is not the HTTP call — it is that an aggregator
returns many publishers under one source row, and this schema hangs both
attribution and permissions off that row. Most of these assert that an article
ends up owned by the publisher who wrote it rather than by GNews.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.collector.gnews_fetcher import (
    DISCOVERED_KIND,
    GNewsFetcher,
    publisher_identity,
    publisher_slug,
)
from backend.database.orm_models import Base, Source


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class _Response:
    def __init__(self, status_code: int, payload=None, raises: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class _Client:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _source(**kw):
    defaults = dict(id=1, name="GNews", category="general", language="en", country="")
    defaults.update(kw)
    return type("S", (), defaults)()


# -- normalisation ----------------------------------------------------------


def test_articles_are_normalised_to_the_pipeline_shape() -> None:
    entries = GNewsFetcher._normalise(
        [
            {
                "title": "  Something happened  ",
                "url": " https://bbc.co.uk/a ",
                "description": " a summary ",
                "publishedAt": "2026-09-09T10:30:00Z",
                "source": {"name": "BBC News", "url": "https://bbc.co.uk"},
            }
        ]
    )
    assert entries == [
        {
            "title": "Something happened",
            "url": "https://bbc.co.uk/a",
            "published": datetime(2026, 9, 9, 10, 30, 0),
            "summary": "a summary",
            "source_name": "BBC News",
            "publisher_name": "BBC News",
            "publisher_url": "https://bbc.co.uk",
        }
    ]


def test_an_article_with_no_publisher_is_dropped() -> None:
    """This API is the one place an article could be filed under whoever
    relayed it. No publisher means no attribution, so it does not enter."""
    entries = GNewsFetcher._normalise(
        [
            {"title": "A", "url": "https://x/1", "source": {}},
            {"title": "B", "url": "https://x/2", "source": {"name": "Real Paper"}},
        ]
    )
    assert [e["publisher_name"] for e in entries] == ["Real Paper"]


def test_timestamps_are_stored_naive_utc() -> None:
    """The rest of the schema is naive UTC; a tz-aware value here would
    compare wrongly against every other published_at."""
    (entry,) = GNewsFetcher._normalise(
        [{"title": "A", "url": "https://x/1", "publishedAt": "2026-09-09T10:00:00Z",
          "source": {"name": "P"}}]
    )
    assert entry["published"].tzinfo is None
    assert entry["published"] == datetime(2026, 9, 9, 10, 0, 0)


def test_a_malformed_timestamp_does_not_lose_the_article() -> None:
    (entry,) = GNewsFetcher._normalise(
        [{"title": "A", "url": "https://x/1", "publishedAt": "not a date",
          "source": {"name": "P"}}]
    )
    assert entry["published"] is None


# -- publisher resolution ---------------------------------------------------


def test_a_new_publisher_gets_its_own_source_row(db) -> None:
    resolved = GNewsFetcher().resolve_publisher(db, "Le Monde", "https://lemonde.fr")
    assert resolved is not None
    assert resolved.name == "Le Monde"
    assert resolved.kind == DISCOVERED_KIND
    assert resolved.feed_url == "gnews://le-monde"
    # Never polled: there is no feed behind it.
    assert resolved.is_active is False


def test_the_same_publisher_resolves_to_one_row(db) -> None:
    a = GNewsFetcher().resolve_publisher(db, "Le Monde", "https://lemonde.fr")
    b = GNewsFetcher().resolve_publisher(db, "Le Monde", "https://lemonde.fr")
    assert a.id == b.id
    assert db.query(Source).filter(Source.name == "Le Monde").count() == 1


@pytest.mark.parametrize(
    "rss_name,aggregator_name",
    [
        # An RSS feed is usually one desk; the aggregator names the masthead.
        ("The Guardian World", "The Guardian"),
        ("BBC World News", "BBC"),
        ("Al Jazeera English", "Al Jazeera"),
        ("NPR News", "NPR"),
        ("The Times of India", "Times of India"),
        ("France 24 English", "France 24"),
    ],
)
def test_an_aggregated_publisher_reuses_its_existing_rss_row(
    db, rss_name, aggregator_name
) -> None:
    """One publisher, one row — therefore one permission set and one takedown
    switch. Reviewing the Guardian's terms once must not leave half its
    articles governed by an unreviewed duplicate."""
    existing = Source(
        name=rss_name, url="https://example.com",
        feed_url="https://example.com/rss.xml", is_active=True, kind="rss",
    )
    db.add(existing)
    db.commit()

    resolved = GNewsFetcher().resolve_publisher(db, aggregator_name, "https://example.com")
    assert resolved.id == existing.id, f"{aggregator_name!r} did not match {rss_name!r}"
    assert resolved.kind == "rss", "the RSS row was converted into a discovered one"
    assert db.query(Source).count() == 1


@pytest.mark.parametrize(
    "a,b",
    [
        ("The Guardian", "Reuters"),
        ("BBC", "Sky News"),
        ("The Times of India", "The Times"),
        ("Le Monde", "Le Figaro"),
    ],
)
def test_different_publishers_are_never_merged(db, a, b) -> None:
    """Merging two publishers who are not the same one would apply a
    permission decision to journalism it was never made about — a worse error
    than carrying a duplicate row, so the matching stays conservative."""
    db.add(
        Source(name=a, url="https://a.example", feed_url="https://a.example/rss",
               is_active=True, kind="rss")
    )
    db.commit()

    resolved = GNewsFetcher().resolve_publisher(db, b, "https://b.example")
    assert resolved.name == b
    assert db.query(Source).count() == 2


def test_identity_survives_reduction_to_nothing() -> None:
    """A name made only of edition words must not collapse every such source
    into one row."""
    assert publisher_identity("World News") == "world-news"
    assert publisher_identity("The News") == "news"
    assert publisher_identity("") == "unknown"


def test_a_publisher_with_no_name_is_unresolvable(db) -> None:
    assert GNewsFetcher().resolve_publisher(db, "", "https://x") is None
    assert GNewsFetcher().resolve_publisher(db, "   ", None) is None


def test_slugs_are_stable_and_url_safe() -> None:
    assert publisher_slug("The New York Times") == "the-new-york-times"
    assert publisher_slug("Al Jazeera  English!") == "al-jazeera-english"
    assert publisher_slug("") == "unknown"


# -- transport --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_fetch_returns_entries_and_metadata(monkeypatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "GNEWS_API_KEY", "test-key")
    client = _Client(
        _Response(200, {"articles": [
            {"title": "A", "url": "https://x/1", "source": {"name": "P"}}
        ]})
    )
    entries, meta = await GNewsFetcher(client=client).fetch_feed_detailed(_source())

    assert len(entries) == 1
    assert meta == {"ok": True, "http_status": 200}
    assert client.calls[0]["params"]["apikey"] == "test-key"


@pytest.mark.asyncio
async def test_a_missing_key_is_reported_not_raised(monkeypatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "GNEWS_API_KEY", "")
    entries, meta = await GNewsFetcher(client=_Client(_Response(200, {}))).fetch_feed_detailed(
        _source()
    )
    assert entries == []
    assert meta["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_an_error_response_ends_the_source_not_the_run(monkeypatch, status) -> None:
    """A dead aggregator must not end a run the RSS feeds could finish."""
    from config.settings import settings

    monkeypatch.setattr(settings, "GNEWS_API_KEY", "k")
    entries, meta = await GNewsFetcher(client=_Client(_Response(status))).fetch_feed_detailed(
        _source()
    )
    assert entries == []
    assert meta == {"ok": False, "http_status": status}


@pytest.mark.asyncio
async def test_a_network_failure_is_caught(monkeypatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "GNEWS_API_KEY", "k")
    entries, meta = await GNewsFetcher(
        client=_Client(RuntimeError("connection reset"))
    ).fetch_feed_detailed(_source())
    assert entries == []
    assert meta["ok"] is False
    assert "connection reset" in meta["error"]


@pytest.mark.asyncio
async def test_unparseable_json_is_caught(monkeypatch) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "GNEWS_API_KEY", "k")
    entries, meta = await GNewsFetcher(
        client=_Client(_Response(200, raises=True))
    ).fetch_feed_detailed(_source())
    assert entries == []
    assert meta["ok"] is False
