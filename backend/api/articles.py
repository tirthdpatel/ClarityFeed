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
from backend.retention import ARTICLE_DAYS

logger = logging.getLogger("news.api.articles")

router = APIRouter(tags=["articles"])

# Every read is CDN-cacheable. Render's free tier sleeps and cold-starts in
# roughly a minute; `stale-while-revalidate` means a reader gets the previous
# page instantly while that happens instead of watching a spinner. Ingestion
# runs hourly, so a 5-minute TTL never shows meaningfully stale news.
CACHE_CONTROL = "public, s-maxage=300, stale-while-revalidate=600"

MAX_LIMIT = 50
DEFAULT_LIMIT = 20

# How far back the archive goes. This is not a display preference — it is the
# retention window in backend/retention.py, restated so the date picker cannot
# offer a day whose articles have already been deleted. The two must move
# together; ARTICLE_DAYS is the source of truth.
ARTICLE_HISTORY_DAYS = ARTICLE_DAYS

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


def _csv_param(value: str, field: str, limit: int = 50) -> list[str]:
    """Split a comma-separated filter value into a clean, bounded list.

    Every list filter goes through here so they behave identically: one value
    and many values are the same code path, and whitespace and stray commas are
    tolerated.

    A value that contains no usable entries returns an empty list, and the
    caller then applies no filter at all. `?country=` and `?country=,,` mean
    the same thing as omitting the parameter — which is what the pickers emit
    when a reader deselects everything, and it would be perverse to answer that
    with an error rather than with the unfiltered feed.

    The cap is not cosmetic: each list becomes an IN clause built from user
    input, and an unbounded one is a cheap way to make the planner do something
    expensive.
    """
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    if len(parts) > limit:
        raise HTTPException(status_code=400, detail=f"too many {field} values.")
    return parts


def _interleave_by_source(rows: list[RawArticle]) -> list[RawArticle]:
    """Reorder one page so a single publisher cannot monopolise it.

    Strict reverse-chronological ordering is correct and reads badly. Feeds
    arrive in bursts, so whichever publisher happened to push last owns the top
    of the page — the front page was nine consecutive Deutsche Welle items out
    of twenty, which makes an aggregator look like a mirror of one outlet.

    This is a round robin over publishers: the newest unshown article from each,
    then the next from each, and so on. Buckets stay in recency order, so recent
    news still floats up; it is the runs that disappear, not the ordering.

    IMPORTANT — this reorders WITHIN a page and never changes its membership.
    The page is still selected chronologically, so the cursor still means
    exactly what it meant before and pagination cannot duplicate or skip. The
    caller must compute the cursor from the chronological order BEFORE calling
    this, which is why this takes an already-sliced page rather than the query.
    """
    if len(rows) < 3:
        return rows

    buckets: dict[int, list[RawArticle]] = {}
    for row in rows:
        buckets.setdefault(row.source_id, []).append(row)

    # One publisher on the page: nothing to interleave, and round-robining a
    # single bucket would just return the same list more slowly.
    if len(buckets) < 2:
        return rows

    order = list(buckets)  # dicts keep insertion order: most recent source first
    out: list[RawArticle] = []
    while len(out) < len(rows):
        for source_id in order:
            bucket = buckets[source_id]
            if bucket:
                out.append(bucket.pop(0))
    return out


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
    country: str | None = Query(
        None, description="Comma-separated country slugs or ISO-2 codes"
    ),
    category: str | None = Query(
        None, description="Comma-separated category slugs; parents match their children"
    ),
    language: str | None = Query(None, description="Source language code"),
    source: str | None = Query(
        None, description="Comma-separated source ids, e.g. 1,4,7"
    ),
    date: str | None = Query(
        None, description="YYYY-MM-DD; a single day of news, in UTC"
    ),
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
        # Several countries widen the feed rather than narrowing it: a reader
        # picking India and the UK wants both, not the empty set of articles
        # filed under both at once. Same for topics and publishers below.
        keys = _csv_param(country, "country")
        if keys:
            query = query.filter(
                select(1)
                .select_from(ArticleCountry)
                .join(Country, Country.id == ArticleCountry.country_id)
                .where(ArticleCountry.article_id == RawArticle.id)
                .where(or_(Country.slug.in_(keys), func.lower(Country.iso2).in_(keys)))
                .exists()
            )

    if category:
        # A category matches itself AND everything beneath it.
        #
        # The classifier only ever tags leaves — `government`, `conflict`,
        # `football` — while the navigation shows the ten roots. Matching the
        # slug exactly therefore returned nothing at all for World, Politics,
        # Society and every other top-level link: not a bug in the tagging, and
        # not an empty database, just a question asked one level too high.
        #
        # Recursive rather than a single parent hop, so a third level added to
        # the taxonomy later does not silently start losing articles.
        slugs = _csv_param(category, "category")
        if slugs:
            roots = (
                select(CategoryDef.id)
                .where(CategoryDef.slug.in_(slugs))
                .cte("category_tree", recursive=True)
            )
            descendants = select(CategoryDef.id).join(
                roots, CategoryDef.parent_id == roots.c.id
            )
            category_tree = roots.union_all(descendants)

            query = query.filter(
                select(1)
                .select_from(ArticleCategory)
                .where(ArticleCategory.article_id == RawArticle.id)
                .where(ArticleCategory.category_id.in_(select(category_tree.c.id)))
                .exists()
            )

    if language:
        query = query.filter(Source.language == language.strip().lower())

    if source:
        # Comma-separated so a reader can pick several publishers at once.
        # Single ids still work — "4" is a one-element list — so existing links
        # do not break.
        try:
            source_ids = [int(p) for p in _csv_param(source, "source")]
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="source must be a comma-separated list of numeric ids.",
            ) from exc

        if source_ids:
            query = query.filter(Source.id.in_(source_ids))

    if date:
        # A whole UTC day, half-open [00:00, next 00:00). Half-open rather than
        # <= 23:59:59 so nothing published in the final second of a day falls
        # between two adjacent date filters.
        try:
            day_start = datetime.strptime(date.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="date must be YYYY-MM-DD."
            ) from exc

        oldest = datetime.utcnow().date() - timedelta(days=ARTICLE_HISTORY_DAYS)
        if day_start.date() < oldest:
            # Say so rather than returning an empty page. An empty page is
            # indistinguishable from a quiet news day, and the reader would
            # have no way to learn the archive simply does not go back that far.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Articles are kept for {ARTICLE_HISTORY_DAYS} days. "
                    f"The archive starts at {oldest.isoformat()}."
                ),
            )

        query = query.filter(
            _SORT_KEY >= day_start, _SORT_KEY < day_start + timedelta(days=1)
        )

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

    # Cursor FIRST, from the chronological order, while rows[-1] is still the
    # oldest article on the page. Interleaving below shuffles display order, and
    # taking the cursor afterwards would point at whatever landed last instead
    # of at the true boundary — which is how pagination starts skipping rows.
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last.published_at or last.created_at, last.id)

    rows = _interleave_by_source(rows)

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
