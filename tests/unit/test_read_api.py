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
    ArticleCategory,
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


def test_page_selects_the_newest_articles(client):
    """Selection is chronological even though display order is not.

    _interleave_by_source reorders within a page, so the page no longer reads
    strictly newest-first top to bottom. What must stay true is that the page
    CONTAINS the newest articles — interleaving is a presentation concern and
    must never change which articles were chosen.
    """
    body = client.get("/articles?limit=2").json()
    picked = {a["id"] for a in body["articles"]}

    everything = client.get("/articles?limit=50").json()["articles"]
    newest_two = {
        a["id"]
        for a in sorted(everything, key=lambda x: x["publishedAt"], reverse=True)[:2]
    }
    assert picked == newest_two


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


# -- category hierarchy -----------------------------------------------------


@pytest.fixture
def taxonomy(db_session):
    """A two-level taxonomy tagged the way the classifier actually tags:
    leaves only, never the root."""
    db_session.add_all(
        [
            CategoryDef(id=10, slug="world", name="World", is_enabled=True),
            CategoryDef(id=11, slug="conflict", name="Conflict",
                        parent_id=10, is_enabled=True),
            CategoryDef(id=12, slug="diplomacy", name="Diplomacy",
                        parent_id=10, is_enabled=True),
            CategoryDef(id=20, slug="sports", name="Sports", is_enabled=True),
            CategoryDef(id=21, slug="football", name="Football",
                        parent_id=20, is_enabled=True),
        ]
    )
    db_session.add_all(
        [
            ArticleCategory(article_id=1, category_id=11, is_primary=True),
            ArticleCategory(article_id=2, category_id=12, is_primary=True),
            ArticleCategory(article_id=3, category_id=21, is_primary=True),
        ]
    )
    db_session.commit()
    return db_session


def test_parent_category_returns_its_children(client, taxonomy):
    """The regression that made every top-level nav link render an empty page:
    the classifier tags leaves, the navigation links roots, and matching the
    slug exactly found nothing."""
    ids = {a["id"] for a in client.get("/articles?category=world").json()["articles"]}
    assert ids == {1, 2}, "parent category did not include its children"


def test_leaf_category_still_matches_itself(client, taxonomy):
    ids = {a["id"] for a in client.get("/articles?category=conflict").json()["articles"]}
    assert ids == {1}


def test_sibling_categories_do_not_leak(client, taxonomy):
    ids = {a["id"] for a in client.get("/articles?category=sports").json()["articles"]}
    assert ids == {3}


def test_unknown_category_is_empty_not_an_error(client, taxonomy):
    assert client.get("/articles?category=nonsense").json()["articles"] == []


# -- source diversity -------------------------------------------------------


def test_page_does_not_run_on_one_publisher(client, db_session):
    """Feeds arrive in bursts, so chronological order alone let one publisher
    own the top of the page — production showed nine consecutive Deutsche Welle
    items out of twenty.

    The mix here mirrors that: one dominant publisher, one middling, one light.
    Long runs cannot always be avoided (a source holding most of the articles
    must eventually run consecutively), so what is asserted is the part that
    matters to a reader — the top of the page is mixed.
    """
    now = datetime(2026, 9, 1, 12, 0, 0)
    burst = []
    # 6 from source 1, 4 from source 2, 2 from source 3, all newer than the
    # fixtures so they occupy the head of the feed.
    for i in range(6):
        burst.append((300 + i, 1, i))
    for i in range(4):
        burst.append((320 + i, 2, i))
    for i in range(2):
        burst.append((340 + i, 3, i))

    db_session.add_all(
        [
            RawArticle(
                id=aid, source_id=sid,
                url=f"https://pub.example/mix-{aid}", url_hash=f"mix{aid}",
                title=f"Mixed {aid}", published_at=now + timedelta(minutes=60 + n),
                ingest_status="PUBLISHED",
            )
            for aid, sid, n in burst
        ]
    )
    # source 3 is inactive in the base fixture; the feed would exclude it.
    db_session.get(Source, 3).is_active = True
    db_session.commit()

    names = [a["source"]["name"] for a in client.get("/articles?limit=12").json()["articles"]]

    assert len(set(names[:3])) == 3, f"top of the page is not mixed: {names[:3]}"

    longest = best = 1
    for prev, cur in zip(names, names[1:]):
        longest = longest + 1 if cur == prev else 1
        best = max(best, longest)
    assert best <= 4, f"a publisher ran {best} deep: {names}"


def test_interleaving_does_not_break_pagination(client, db_session):
    """Reordering happens within a page, never across one. Walking every page
    must still visit each article exactly once."""
    now = datetime(2026, 9, 1, 12, 0, 0)
    db_session.add_all(
        [
            RawArticle(
                id=200 + i, source_id=(1 if i % 2 else 2),
                url=f"https://pub.example/pag-{i}", url_hash=f"pag{i}",
                title=f"Paged {i}", published_at=now + timedelta(minutes=i),
                ingest_status="PUBLISHED",
            )
            for i in range(10)
        ]
    )
    db_session.commit()

    seen, cursor, pages = [], None, 0
    while pages < 20:
        url = f"/articles?limit=3{f'&cursor={cursor}' if cursor else ''}"
        body = client.get(url).json()
        seen.extend(a["id"] for a in body["articles"])
        cursor = body["nextCursor"]
        pages += 1
        if not cursor:
            break

    assert len(seen) == len(set(seen)), "pagination repeated an article"
    total = len(client.get("/articles?limit=50").json()["articles"])
    assert len(seen) == total, f"walked {len(seen)} of {total} articles"


# -- date selection ---------------------------------------------------------


def test_date_filter_returns_only_that_day(client, db_session):
    """A whole UTC day, half-open, so nothing falls between adjacent dates."""
    from datetime import timezone

    today = datetime.utcnow().date()
    day = today - timedelta(days=2)
    db_session.add_all(
        [
            # 00:00:00 and 23:59:59 on the target day, plus one second either
            # side of it. The boundary rows are the point of the test.
            RawArticle(id=400, source_id=1, url="https://p/d0", url_hash="d0",
                       title="Midnight exactly",
                       published_at=datetime(day.year, day.month, day.day, 0, 0, 0),
                       ingest_status="PUBLISHED"),
            RawArticle(id=401, source_id=1, url="https://p/d1", url_hash="d1",
                       title="Last second",
                       published_at=datetime(day.year, day.month, day.day, 23, 59, 59),
                       ingest_status="PUBLISHED"),
            RawArticle(id=402, source_id=1, url="https://p/d2", url_hash="d2",
                       title="One second before",
                       published_at=datetime(day.year, day.month, day.day) - timedelta(seconds=1),
                       ingest_status="PUBLISHED"),
            RawArticle(id=403, source_id=1, url="https://p/d3", url_hash="d3",
                       title="Next day",
                       published_at=datetime(day.year, day.month, day.day) + timedelta(days=1),
                       ingest_status="PUBLISHED"),
        ]
    )
    db_session.commit()

    ids = {
        a["id"]
        for a in client.get(f"/articles?date={day.isoformat()}&limit=50").json()["articles"]
    }
    assert 400 in ids and 401 in ids, "day boundaries excluded"
    assert 402 not in ids and 403 not in ids, "adjacent days leaked in"


def test_date_outside_retention_explains_itself(client):
    """An empty page reads as a quiet news day. Say the archive is shorter."""
    old = (datetime.utcnow().date() - timedelta(days=60)).isoformat()
    r = client.get(f"/articles?date={old}")
    assert r.status_code == 400
    assert "kept for" in r.json()["detail"]


def test_malformed_date_is_rejected(client):
    assert client.get("/articles?date=09-09-2026").status_code == 400
    assert client.get("/articles?date=yesterday").status_code == 400


def test_archive_window_matches_retention(client):
    """The picker's range is derived from the retention policy, not restated."""
    from backend.retention import ARTICLE_DAYS

    body = client.get("/archive").json()
    assert body["days"] == ARTICLE_DAYS
    assert body["earliest"] < body["latest"]


# -- feed HTML --------------------------------------------------------------


def test_markup_in_a_feed_excerpt_is_stripped(client, db_session):
    """The Guardian ships HTML in every description; it rendered literally."""
    db_session.add(
        RawArticle(
            id=500, source_id=1, url="https://p/html", url_hash="html500",
            title="Markup test",
            summary_from_feed=(
                '<p>Follow the day&apos;s news live</p><ul><li><p>Get our '
                '<a href="https://x/y?CMP=cvau_sfl">new political email</a></p></li></ul>'
            ),
            published_at=datetime(2026, 9, 1, 12, 0, 0), ingest_status="PUBLISHED",
        )
    )
    db_session.commit()

    desc = client.get("/articles/500").json()["description"]
    assert "<" not in desc and "href" not in desc, desc
    assert "Follow the day's news live" in desc
    assert "new political email" in desc


def test_excerpt_limit_counts_text_not_tags(client, db_session):
    """Truncating before stripping spent the whole budget on markup."""
    db_session.add(
        RawArticle(
            id=501, source_id=1, url="https://p/html2", url_hash="html501",
            title="Budget test",
            summary_from_feed=(
                '<a href="https://example.com/a-very-long-tracking-url-'
                'that-eats-the-entire-character-budget">Real sentence here</a>'
            ),
            published_at=datetime(2026, 9, 1, 12, 0, 0), ingest_status="PUBLISHED",
        )
    )
    db_session.add(SourcePermission(source_id=1, max_description_chars=40))
    db_session.commit()

    desc = client.get("/articles/501").json()["description"]
    assert "Real sentence here" in desc, f"markup consumed the budget: {desc!r}"


# -- multi-select publishers ------------------------------------------------


def test_multiple_sources_can_be_selected(client):
    ids = {a["source"]["id"] for a in client.get("/articles?source=1,2").json()["articles"]}
    assert ids == {1, 2}


def test_single_source_still_works(client):
    """Existing one-id links must not break when the parameter learns commas."""
    ids = {a["source"]["id"] for a in client.get("/articles?source=2").json()["articles"]}
    assert ids == {2}


def test_source_list_tolerates_spacing_and_trailing_commas(client):
    ids = {a["source"]["id"] for a in client.get("/articles?source=1, 2,").json()["articles"]}
    assert ids == {1, 2}


def test_source_list_is_bounded(client):
    """The value goes straight into an IN clause; unbounded input is a cheap
    way to make the planner do something expensive."""
    huge = ",".join(str(i) for i in range(200))
    assert client.get(f"/articles?source={huge}").status_code == 400


def test_source_and_category_compose(client, taxonomy):
    body = client.get("/articles?source=1&category=world").json()
    assert all(a["source"]["id"] == 1 for a in body["articles"])


# -- combining filters ------------------------------------------------------


def test_multiple_countries_widen_the_feed(client, db_session):
    """Two countries means either, not both — picking India and the UK asks
    for news from each, not for articles filed under both at once."""
    db_session.add_all(
        [
            Country(id=10, iso2="IN", iso3="IND", name="India", slug="in", is_enabled=True),
            Country(id=11, iso2="GB", iso3="GBR", name="United Kingdom", slug="gb", is_enabled=True),
            Country(id=12, iso2="FR", iso3="FRA", name="France", slug="fr", is_enabled=True),
        ]
    )
    from backend.database.orm_models import ArticleCountry

    db_session.add_all(
        [
            ArticleCountry(article_id=1, country_id=10, relevance="primary"),
            ArticleCountry(article_id=2, country_id=11, relevance="primary"),
            ArticleCountry(article_id=3, country_id=12, relevance="primary"),
        ]
    )
    db_session.commit()

    ids = {a["id"] for a in client.get("/articles?country=in,gb").json()["articles"]}
    assert ids == {1, 2}


def test_country_accepts_slug_or_iso2_in_a_list(client, db_session):
    db_session.add(
        Country(id=13, iso2="JP", iso3="JPN", name="Japan", slug="japan", is_enabled=True)
    )
    from backend.database.orm_models import ArticleCountry

    db_session.add(ArticleCountry(article_id=1, country_id=13, relevance="primary"))
    db_session.commit()

    by_slug = {a["id"] for a in client.get("/articles?country=japan").json()["articles"]}
    by_iso = {a["id"] for a in client.get("/articles?country=jp").json()["articles"]}
    assert by_slug == by_iso == {1}


def test_multiple_categories_include_all_their_children(client, taxonomy):
    """world covers conflict+diplomacy, sports covers football."""
    ids = {a["id"] for a in client.get("/articles?category=world,sports").json()["articles"]}
    assert ids == {1, 2, 3}


def test_country_category_and_source_compose(client, db_session, taxonomy):
    """All three narrow together: within the chosen countries AND the chosen
    topics AND the chosen publishers."""
    db_session.add(
        Country(id=14, iso2="GB", iso3="GBR", name="United Kingdom", slug="gb", is_enabled=True)
    )
    from backend.database.orm_models import ArticleCountry

    db_session.add_all(
        [
            ArticleCountry(article_id=1, country_id=14, relevance="primary"),
            ArticleCountry(article_id=3, country_id=14, relevance="primary"),
        ]
    )
    db_session.commit()

    # article 1: source 1, country gb, category conflict (under world)
    # article 3: source 2, country gb, category football (under sports)
    body = client.get("/articles?country=gb&category=world&source=1").json()
    assert {a["id"] for a in body["articles"]} == {1}


def test_empty_filter_value_means_no_filter(client):
    """`?country=` is what a picker emits when everything is deselected. It
    means "no filter", not "an error" — and `,,` has to agree, or the two
    spellings of the same intent behave differently."""
    unfiltered = len(client.get("/articles?limit=50").json()["articles"])
    for q in ("country=", "category=,,", "source=,", "country=&category="):
        body = client.get(f"/articles?{q}&limit=50").json()
        assert len(body["articles"]) == unfiltered, f"{q} changed the result"


def test_country_and_category_lists_are_bounded(client):
    many = ",".join(f"x{i}" for i in range(200))
    assert client.get(f"/articles?country={many}").status_code == 400
    assert client.get(f"/articles?category={many}").status_code == 400
