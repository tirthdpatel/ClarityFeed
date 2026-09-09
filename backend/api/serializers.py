"""Article serialization — the SERIALIZE half of the permission boundary.

Every article that leaves this process goes through `serialize_articles()`.
That is the only reason the permission model is worth anything: a gate with a
bypass is decoration. There is deliberately no "raw" serializer to reach for
in a hurry.

Two things here are load-bearing and easy to undo by accident:

1. **Permissions are batch-resolved.** One query for every source in the page,
   not one per article. The N+1 version works fine on a seeded test database
   and falls over on the free tier's connection limit.

2. **A summary is never a bare string.** It serializes as an object carrying
   `aiGenerated: true` and a disclaimer, so a frontend cannot render a
   machine-written sentence as though a person wrote it without deliberately
   throwing the label away. See ROADMAP §6.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Sequence

from sqlalchemy.orm import Session

from backend.database.orm_models import (
    ArticleCategory,
    ArticleCountry,
    CategoryDef,
    Country,
    RawArticle,
    Source,
    SourcePermission,
    Summary,
)
from backend.feedtext import strip_html
from backend.permissions import Permissions, apply_serialize_gate

logger = logging.getLogger("news.api.serializers")

# Shown next to every generated summary. Kept here rather than in the frontend
# so that every client — the website, a future feed, anything — gets it.
AI_DISCLAIMER = "AI-generated summary. May contain errors — read the original."


def resolve_permissions(db: Session, source_ids: Iterable[int]) -> dict[int, Permissions]:
    """source_id -> Permissions, restrictive for anything unreviewed.

    A source with no row is not an error and must not be skipped: it resolves
    to `Permissions.restrictive()`, which is the whole point of the defaults.
    """
    ids = {int(i) for i in source_ids if i is not None}
    if not ids:
        return {}
    rows = db.query(SourcePermission).filter(SourcePermission.source_id.in_(ids)).all()
    by_id = {r.source_id: Permissions.from_row(r) for r in rows}
    return {sid: by_id.get(sid, Permissions.restrictive()) for sid in ids}


def _summary_payload(row: Summary | None, perms: Permissions) -> dict[str, Any] | None:
    if row is None or not perms.can_generate_summary:
        return None

    bullets: list[str] = []
    if row.bullet_points:
        try:
            parsed = json.loads(row.bullet_points)
            if isinstance(parsed, list):
                bullets = [str(b) for b in parsed]
        except (ValueError, TypeError):
            # A malformed bullet blob must not take down a page of articles.
            logger.warning("summary %s has unparseable bullet_points", row.id)

    tldr = (row.tldr or row.summary_text or "").strip()
    if not tldr and not bullets:
        return None

    return {
        "tldr": tldr or None,
        "bulletPoints": bullets,
        # These three travel together on purpose — see the module docstring.
        "aiGenerated": True,
        "disclaimer": AI_DISCLAIMER,
        "model": row.model_name or None,
        "generatedAt": row.created_at.isoformat() if row.created_at else None,
    }


def serialize_articles(
    db: Session,
    articles: Sequence[RawArticle],
    *,
    include_summaries: bool = True,
) -> list[dict[str, Any]]:
    """Serialize a page of articles through the permission gate."""
    if not articles:
        return []

    article_ids = [a.id for a in articles]
    perms_by_source = resolve_permissions(db, (a.source_id for a in articles))

    sources = {
        s.id: s
        for s in db.query(Source).filter(Source.id.in_({a.source_id for a in articles})).all()
    }

    summaries: dict[int, Summary] = {}
    if include_summaries:
        summaries = {
            s.raw_article_id: s
            for s in db.query(Summary).filter(Summary.raw_article_id.in_(article_ids)).all()
        }

    cats: dict[int, list[dict[str, Any]]] = {}
    for art_id, slug, name, is_primary in (
        db.query(ArticleCategory.article_id, CategoryDef.slug, CategoryDef.name, ArticleCategory.is_primary)
        .join(CategoryDef, CategoryDef.id == ArticleCategory.category_id)
        .filter(ArticleCategory.article_id.in_(article_ids))
        .all()
    ):
        cats.setdefault(art_id, []).append(
            {"slug": slug, "name": name, "isPrimary": bool(is_primary)}
        )

    countries: dict[int, list[dict[str, Any]]] = {}
    for art_id, iso2, name, slug, flag, relevance in (
        db.query(
            ArticleCountry.article_id,
            Country.iso2,
            Country.name,
            Country.slug,
            Country.flag_emoji,
            ArticleCountry.relevance,
        )
        .join(Country, Country.id == ArticleCountry.country_id)
        .filter(ArticleCountry.article_id.in_(article_ids))
        .all()
    ):
        countries.setdefault(art_id, []).append(
            {"iso2": iso2, "name": name, "slug": slug, "flag": flag, "relevance": relevance}
        )

    out: list[dict[str, Any]] = []
    for a in articles:
        perms = perms_by_source.get(a.source_id, Permissions.restrictive())
        src = sources.get(a.source_id)

        payload: dict[str, Any] = {
            "id": a.id,
            "title": a.title,
            # Stripped here, before apply_serialize_gate truncates. The gate
            # cuts to max_description_chars, and on a source that ships markup
            # in its feed — The Guardian does, for every item — that budget was
            # being spent on `<a href="...">` rather than on the sentence the
            # reader sees. Order matters: strip, then truncate.
            "description": strip_html(a.summary_from_feed),
            # Full text is never populated here even when permitted. Rendering
            # a publisher's article body on our own domain is reproduction
            # rather than referral, and no source currently licences it.
            "content": None,
            "imageUrl": None,
            "url": a.url,
            "publishedAt": (a.published_at or a.created_at).isoformat()
            if (a.published_at or a.created_at)
            else None,
            "source": {
                "id": src.id if src else a.source_id,
                "name": src.name if src else "",
                "url": src.url if src else None,
                "language": src.language if src else None,
            },
            "categories": cats.get(a.id, []),
            "countries": countries.get(a.id, []),
            "summary": _summary_payload(summaries.get(a.id), perms),
        }

        gated = apply_serialize_gate(payload, perms)

        # Attribution is a hard invariant, not a field. An article with no
        # route back to the publisher is the one thing this API must never
        # emit, so it is dropped rather than served — loudly, in the log.
        attribution = gated.get("attribution") or {}
        if not attribution.get("readOriginalUrl") or not gated["source"]["name"]:
            logger.error(
                "article %s dropped from response: missing attribution (url=%r source=%r)",
                a.id,
                gated.get("url"),
                gated["source"].get("name"),
            )
            continue

        out.append(gated)

    return out
