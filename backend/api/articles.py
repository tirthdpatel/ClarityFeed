"""Public read API — the only endpoints that serve article data.

Cursor pagination, not offset. A news feed gains rows at the head between a
reader's first and second page; with OFFSET that shows them duplicates and
silently skips articles. The cursor encodes the sort key of the last row seen,
so a page boundary means the same thing regardless of what arrived since.

Ordering is `COALESCE(published_at, created_at) DESC, id DESC`. `published_at`
comes from the feed and is nullable and occasionally a lie; `id` breaks ties so
the order is total — without it, articles sharing a timestamp can swap places
between requests and paginate incorrectly.
"""
from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.api.serializers import serialize_articles
from backend.database.orm_models import (
    ArticleCategory,
    ArticleCountry,
    CategoryDef,
    Country,
    RawArticle,
    Source,
)
from backend.database.session import get_db

logger = logging.getLogger("news.api.articles")

router = APIRouter(tags=["articles"])

# Every read is CDN-cacheable. Render's free tier sleeps and cold-starts in
# roughly a minute; `stale-while-revalidate` means a reader gets the previous
# page instantly while that happens instead of watching a spinner. Ingestion
# runs hourly, so a 5-minute TTL never shows meaningfully stale news.
CACHE_CONTROL = "public, s-maxage=300, stale-while-revalidate=600"

MAX_LIMIT = 50
DEFAULT_LIMIT = 20

# The sort key: published_at where the feed gave us one, else ingestion time.
_SORT_KEY = func.coalesce(RawArticle.published_at, RawArticle.created_at)


def _encode_cursor(sort_value: datetime, article_id: int) -> str:
    raw = f"{sort_value.isoformat()}|{article_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, _, ident = base64.urlsafe_b64decode(padded.encode()).decode().rpartition("|")
        return datetime.fromisoformat(stamp), int(ident)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        # A bad cursor is a client error, not a server one. Returning page one
        # instead would silently restart a reader's scroll from the top.
        raise HTTPException(status_code=400, detail="Malformed cursor.") from exc


def _published_query(db: Session):
    """Base query: published articles only.

    The publish barrier (V2 §A3) means nothing if the read path goes around
    it. `PUBLISHED` is the only state a reader may observe; everything else is
    mid-pipeline and may be incomplete.
    """
    return (
        db.query(RawArticle)
        .join(Source, Source.id == RawArticle.source_id)
        .filter(RawArticle.ingest_status == "PUBLISHED")
        .filter(Source.is_active.is_(True))
        # A takedown must take effect on the next request, not on the next
        # purge. Setting the column is the whole remedy; deleting the rows is
        # cleanup that can follow at leisure.
        .filter(Source.takedown_requested_at.is_(None))
    )


@router.get("/articles")
def list_articles(
    response: Response,
    db: Session = Depends(get_db),
    country: str | None = Query(None, description="Country slug or ISO-2 code"),
    category: str | None = Query(None, description="Category slug"),
    language: str | None = Query(None, description="Source language code"),
    source: str | None = Query(None, description="Source id"),
    since: str | None = Query(None, description="ISO-8601; articles published after this"),
    days: int | None = Query(None, ge=1, le=90, description="Articles from the last N days"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None, description="From a previous response's nextCursor"),
) -> dict[str, Any]:
    """A page of published articles, newest first."""
    query = _published_query(db)

    # Country and category are many-to-many, so joining to filter on them
    # multiplies the article row once per matching tag. EXISTS asks the same
    # question without producing those duplicates in the first place.
    #
    # The obvious alternative — join, then SELECT DISTINCT — is what this used
    # to do, and it is wrong on PostgreSQL: DISTINCT requires every ORDER BY
    # expression to appear in the select list, and the sort key here is a
    # COALESCE over two columns rather than a plain column. SQLite accepts it,
    # so the whole test suite passed while /articles returned 500 against the
    # real database. EXISTS avoids the duplicates and therefore the DISTINCT,
    # and lets the ordering index still be used.
    if country:
        key = country.strip().lower()
        query = query.filter(
            select(1)
            .select_from(ArticleCountry)
            .join(Country, Country.id == ArticleCountry.country_id)
            .where(ArticleCountry.article_id == RawArticle.id)
            .where(or_(Country.slug == key, func.lower(Country.iso2) == key))
            .exists()
        )

    if category:
        query = query.filter(
            select(1)
            .select_from(ArticleCategory)
            .join(CategoryDef, CategoryDef.id == ArticleCategory.category_id)
            .where(ArticleCategory.article_id == RawArticle.id)
            .where(CategoryDef.slug == category.strip().lower())
            .exists()
        )

    if language:
        query = query.filter(Source.language == language.strip().lower())

    if source:
        try:
            query = query.filter(Source.id == int(source))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="source must be a numeric id.") from exc

    if since:
        try:
            query = query.filter(_SORT_KEY > datetime.fromisoformat(since))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be ISO-8601.") from exc

    if days:
        query = query.filter(_SORT_KEY > datetime.utcnow() - timedelta(days=days))

    if cursor:
        cur_stamp, cur_id = _decode_cursor(cursor)
        # Strict "older than the last row seen", with id as the tiebreak. The
        # row-value form is what makes this a single index range scan.
        query = query.filter(
            or_(_SORT_KEY < cur_stamp, (_SORT_KEY == cur_stamp) & (RawArticle.id < cur_id))
        )

    # Fetch one extra to learn whether another page exists without COUNT(*)
    # over the whole table on every request.
    rows = query.order_by(_SORT_KEY.desc(), RawArticle.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.published_at or last.created_at, last.id)

    response.headers["Cache-Control"] = CACHE_CONTROL
    return {
        "articles": serialize_articles(db, rows),
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@router.get("/articles/{article_id}")
def get_article(
    article_id: int,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A single published article."""
    row = _published_query(db).filter(RawArticle.id == article_id).first()
    if row is None:
        # Unpublished and nonexistent are deliberately the same response. The
        # difference is not the reader's business and leaks the pipeline state.
        raise HTTPException(status_code=404, detail="Article not found.")

    serialized = serialize_articles(db, [row])
    if not serialized:
        # The gate dropped it — an article with no usable attribution. It is
        # not servable, and the log line in the serializer says why.
        raise HTTPException(status_code=404, detail="Article not found.")

    response.headers["Cache-Control"] = CACHE_CONTROL
    return serialized[0]
