"""Classification stage — run the classifiers over articles and persist results.

Sits between cleaning and the publish barrier. Costs zero API calls, so unlike
the old LLM categoriser it cannot stall the pipeline when a vendor changes
their free tier (ARCHITECTURE_V3 §A1).

Writes:
    article_countries    primary + secondary + mentioned, with confidence
    article_categories   most specific matching category, with confidence

Two properties worth stating:

**Idempotent.** Re-running replaces an article's assignments rather than
adding to them. Re-classification after a gazetteer fix is a normal operation,
not a repair job.

**Abstention is a real outcome.** An article with no country evidence gets
`is_international` and no row, not a guess. Same for categories. The review
queue in §14 is fed from `needs_review`, and it is the mechanism by which
classification actually improves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from backend.classifier.category import CategoryClassifier
from backend.classifier.country import CountryClassifier

logger = logging.getLogger("news.classifier.assigner")


@dataclass
class AssignmentStats:
    processed: int = 0
    countries_assigned: int = 0
    categories_assigned: int = 0
    international: int = 0
    needs_review: int = 0
    unclassified_country: int = 0
    unclassified_category: int = 0
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "countries_assigned": self.countries_assigned,
            "categories_assigned": self.categories_assigned,
            "international": self.international,
            "needs_review": self.needs_review,
            "unclassified_country": self.unclassified_country,
            "unclassified_category": self.unclassified_category,
        }


class ClassificationStage:
    """Applies both classifiers to a batch of articles and persists the result."""

    def __init__(
        self,
        country_classifier: CountryClassifier,
        category_classifier: CategoryClassifier,
    ) -> None:
        self._country = country_classifier
        self._category = category_classifier

    # -- lookups ---------------------------------------------------------
    def _country_ids(self, db: Session) -> dict[str, int]:
        from backend.database.orm_models import Country
        return {c.iso2: c.id for c in db.scalars(select(Country))}

    def _category_ids(self, db: Session) -> dict[str, int]:
        from backend.database.orm_models import CategoryDef
        return {c.slug: c.id for c in db.scalars(select(CategoryDef))}

    # -- main ------------------------------------------------------------
    def classify_batch(self, db: Session, articles: Sequence) -> AssignmentStats:
        """Classify `articles` (RawArticle rows) and write their associations."""
        import time

        from backend.database.orm_models import (
            ArticleCategory, ArticleCountry, Source,
        )

        start = time.monotonic()
        stats = AssignmentStats()
        if not articles:
            return stats

        country_ids = self._country_ids(db)
        category_ids = self._category_ids(db)

        # Source metadata drives the Tier 0 and Tier 1 priors.
        source_ids = {a.source_id for a in articles}
        sources = {
            s.id: s for s in db.scalars(select(Source).where(Source.id.in_(source_ids)))
        }

        article_ids = [a.id for a in articles]
        # Idempotence: clear prior assignments for exactly this batch.
        db.execute(delete(ArticleCountry).where(ArticleCountry.article_id.in_(article_ids)))
        db.execute(delete(ArticleCategory).where(ArticleCategory.article_id.in_(article_ids)))

        country_rows: list[dict] = []
        category_rows: list[dict] = []

        for art in articles:
            stats.processed += 1
            src = sources.get(art.source_id)
            description = getattr(art, "summary_from_feed", "") or ""

            # ---- country ----
            cr = self._country.classify(
                title=art.title or "",
                description=description,
                source_country=(src.country if src else None),
                feed_url=(src.feed_url if src else ""),
            )
            if cr.needs_review:
                stats.needs_review += 1
            if cr.is_international:
                stats.international += 1

            if cr.primary is None:
                stats.unclassified_country += 1
            else:
                for iso2, relevance in (
                    [(cr.primary, "primary")]
                    + [(c, "secondary") for c in cr.secondary]
                    + [(c, "mentioned") for c in cr.mentioned]
                ):
                    cid = country_ids.get(iso2)
                    if cid is None:
                        continue
                    country_rows.append({
                        "article_id": art.id,
                        "country_id": cid,
                        "relevance": relevance,
                        "confidence": cr.confidence if relevance == "primary" else 0.0,
                        "method": cr.method,
                        # Denormalised so the country feed is an index-only
                        # scan — V2 §3.4, the most important index decision.
                        "published_at": art.published_at,
                    })
                stats.countries_assigned += 1

            # ---- category ----
            gr = self._category.classify(art.title or "", description)
            if gr.primary is None:
                stats.unclassified_category += 1
            else:
                for slug, primary in (
                    [(gr.primary, True)] + [(s, False) for s in gr.secondary]
                ):
                    gid = category_ids.get(slug)
                    if gid is None:
                        continue
                    category_rows.append({
                        "article_id": art.id,
                        "category_id": gid,
                        "confidence": gr.confidence if primary else 0.0,
                        "is_primary": primary,
                        "method": "rule",
                    })
                stats.categories_assigned += 1

        # Bulk write — one round trip each, not one per row. Latency to a
        # remote database is the binding constraint here.
        if country_rows:
            db.execute(insert(ArticleCountry), country_rows)
        if category_rows:
            db.execute(insert(ArticleCategory), category_rows)
        db.commit()

        stats.duration_seconds = round(time.monotonic() - start, 2)
        logger.info("Classified %d articles: %s", stats.processed, stats.as_dict())
        return stats
