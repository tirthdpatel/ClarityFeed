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

from backend.database.orm_models import CategoryDef, Country, Language, Region
from backend.database.session import get_db
from backend.retention import ARTICLE_DAYS

router = APIRouter(tags=["reference"])

# An hour, against the feed's five minutes. Editing the taxonomy is a deploy.
CACHE_CONTROL = "public, s-maxage=3600, stale-while-revalidate=86400"


@router.get("/countries")
def list_countries(response: Response, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Enabled countries, grouped by region via `regionCode`."""
    regions = {r.id: r for r in db.query(Region).all()}
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
            "articleCount": c.cached_article_count,
        }
        for c in rows
    ]


@router.get("/categories")
def list_categories(response: Response, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Enabled categories. `parentSlug` carries the Technology → AI nesting."""
    rows = (
        db.query(CategoryDef)
        .filter(CategoryDef.is_enabled.is_(True))
        .order_by(CategoryDef.sort_order, CategoryDef.name)
        .all()
    )
    by_id = {c.id: c for c in rows}
    response.headers["Cache-Control"] = CACHE_CONTROL
    return [
        {
            "slug": c.slug,
            "name": c.name,
            "parentSlug": by_id[c.parent_id].slug if c.parent_id in by_id else None,
            "colorToken": c.color_token,
        }
        for c in rows
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
