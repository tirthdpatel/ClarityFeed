"""Reference data — the filter controls the frontend renders.

These are the seeded taxonomy tables (V2 §3, V3 Part C/D). They change when
someone edits a YAML file and runs a migration, not on ingestion, so they are
cached far harder than the article feed.

Countries and languages are filtered to `is_enabled`. A country with a row but
no coverage is a dead filter that returns an empty page, and the fastest way
to make a news site feel broken is a nav full of links to nothing.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from datetime import date as date_cls, timedelta

from sqlalchemy import func, select

from backend.database.orm_models import (
    ArticleCategory,
    ArticleCountry,
    CategoryDef,
    Country,
    Language,
    RawArticle,
    Region,
    Source,
)
from backend.database.session import get_db
from backend.retention import ARTICLE_DAYS

router = APIRouter(tags=["reference"])

# An hour, against the feed's five minutes. Editing the taxonomy is a deploy.
CACHE_CONTROL = "public, s-maxage=3600, stale-while-revalidate=86400"


def _servable_article_ids():
    """Articles a reader can actually reach: published, from a live source.

    Reference counts have to agree with the feed. Counting rows the feed will
    not serve produces a navigation entry advertising articles that are not
    there when you click it.
    """
    return (
        select(RawArticle.id)
        .join(Source, Source.id == RawArticle.source_id)
        .where(RawArticle.ingest_status == "PUBLISHED")
        # Not is_active — see the note in backend/api/articles.py. Counts must
        # agree with what the feed serves, so this predicate has to match it
        # exactly or a publisher appears in the filter with the wrong number.
        .where(Source.takedown_requested_at.is_(None))
    )


@router.get("/countries")
def list_countries(response: Response, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Enabled countries that currently have articles, grouped by region.

    Counted live rather than read from `countries.cached_article_count`, which
    is declared in the schema and never written — it reads 0 for every country,
    so serving it meant every entry advertised nothing.

    Empty countries are omitted entirely. Retention keeps ten days, and a
    low-volume country can genuinely have nothing in that window; a nav link to
    a page that says "nothing here" is worse than no link.
    """
    regions = {r.id: r for r in db.query(Region).all()}

    counts = dict(
        db.execute(
            select(ArticleCountry.country_id, func.count(ArticleCountry.article_id))
            .where(ArticleCountry.article_id.in_(_servable_article_ids()))
            .group_by(ArticleCountry.country_id)
        ).all()
    )

    rows = (
        db.query(Country)
        .filter(Country.is_enabled.is_(True))
        .order_by(Country.sort_order, Country.name)
        .all()
    )
    response.headers["Cache-Control"] = CACHE_CONTROL
    return [
        {
            "iso2": c.iso2,
            "name": c.name,
            "slug": c.slug,
            "flag": c.flag_emoji,
            "regionCode": regions[c.region_id].code if c.region_id in regions else None,
            "regionName": regions[c.region_id].name if c.region_id in regions else None,
            "articleCount": counts.get(c.id, 0),
        }
        for c in rows
        if counts.get(c.id, 0) > 0
    ]


@router.get("/categories")
def list_categories(response: Response, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Enabled categories that have articles. `parentSlug` carries the nesting.

    A root's count includes its descendants, because that is what selecting it
    does in the feed. Counting only articles tagged on the root itself would
    report zero for every one of them — the classifier tags leaves.
    """
    rows = (
        db.query(CategoryDef)
        .filter(CategoryDef.is_enabled.is_(True))
        .order_by(CategoryDef.sort_order, CategoryDef.name)
        .all()
    )
    by_id = {c.id: c for c in rows}

    direct = dict(
        db.execute(
            select(ArticleCategory.category_id, func.count(ArticleCategory.article_id))
            .where(ArticleCategory.article_id.in_(_servable_article_ids()))
            .group_by(ArticleCategory.category_id)
        ).all()
    )

    # Roll each category's own count up into its ancestors.
    total = {c.id: direct.get(c.id, 0) for c in rows}
    for c in rows:
        parent_id = c.parent_id
        seen = set()
        while parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)  # a malformed cycle must not hang the endpoint
            total[parent_id] = total.get(parent_id, 0) + direct.get(c.id, 0)
            parent_id = by_id[parent_id].parent_id

    response.headers["Cache-Control"] = CACHE_CONTROL
    return [
        {
            "slug": c.slug,
            "name": c.name,
            "parentSlug": by_id[c.parent_id].slug if c.parent_id in by_id else None,
            "colorToken": c.color_token,
            "articleCount": total.get(c.id, 0),
        }
        for c in rows
        if total.get(c.id, 0) > 0
    ]


@router.get("/languages")
def list_languages(response: Response, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Enabled languages. `direction` is what an RTL layout switches on."""
    rows = (
        db.query(Language)
        .filter(Language.is_enabled.is_(True))
        .order_by(Language.sort_order, Language.name)
        .all()
    )
    response.headers["Cache-Control"] = CACHE_CONTROL
    return [
        {
            "code": lang.code,
            "name": lang.name,
            "nativeName": lang.native_name,
            "direction": lang.direction,
        }
        for lang in rows
    ]


@router.get("/archive")
def archive_window(response: Response) -> dict[str, Any]:
    """The range of dates that actually have articles behind them.

    The date picker is built from this rather than from a constant in the
    frontend, so it cannot offer a day whose articles retention has already
    deleted. One source of truth, in backend/retention.py.
    """
    today = date_cls.today()
    response.headers["Cache-Control"] = "public, s-maxage=300, stale-while-revalidate=3600"
    return {
        "days": ARTICLE_DAYS,
        "earliest": (today - timedelta(days=ARTICLE_DAYS)).isoformat(),
        "latest": today.isoformat(),
    }


@router.get("/sources")
def list_sources(response: Response, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Publishers that currently have articles, most-covered first.

    This populates the publisher filter, so the question it answers is "whose
    journalism is in this feed?" — not "which feeds do we poll?", which is what
    it used to answer via get_active_sources().

    The difference matters since the GNews adapter landed. A publisher
    discovered through an aggregator has `is_active = False`, because there is
    no feed to poll; it exists to own an attribution and a permission row. The
    old query filtered those out, so Reuters could have articles in the feed
    and be absent from the control that filters by publisher.

    Counted over servable articles only, for the same reason as the other
    reference endpoints: an entry that advertises articles the feed will not
    serve is worse than no entry.
    """
    counts = dict(
        db.execute(
            select(RawArticle.source_id, func.count(RawArticle.id))
            .where(RawArticle.id.in_(_servable_article_ids()))
            .group_by(RawArticle.source_id)
        ).all()
    )
    if not counts:
        response.headers["Cache-Control"] = CACHE_CONTROL
        return []

    rows = db.query(Source).filter(Source.id.in_(counts)).all()
    response.headers["Cache-Control"] = CACHE_CONTROL
    return sorted(
        (
            {
                "id": s.id,
                "name": s.name,
                "url": s.url,
                "language": s.language,
                "kind": s.kind,
                "articleCount": counts.get(s.id, 0),
            }
            for s in rows
        ),
        key=lambda s: (-s["articleCount"], s["name"]),
    )
