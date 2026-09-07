#!/usr/bin/env python3
"""Seed a synthetic enrichment backlog, for the Kubernetes scaling demo.

    python scripts/seed_demo_backlog.py --count 200
    python scripts/seed_demo_backlog.py --clean       # remove what it created

WHY THIS EXISTS

The scaling demo in docs/kubernetes.md needs rows with
`enrichment_status = 'PENDING'` for the workers to claim. A real database may
have none, and on 2026-09-07 the live one had 9,069 articles of which every
single one was SKIPPED — because no source has a `source_permissions` row, so
`Permissions.restrictive()` denies `can_store_full_text`, so no body is ever
fetched, so nothing is summarisable. That is the compliance posture working as
designed, not a bug.

WHY THE CONTENT IS SYNTHETIC

The alternative — granting real publishers `can_store_full_text` so their
articles become summarisable — is a licensing decision, and it is yours to
make per source after review, which is exactly what backend/permissions.py
exists to enforce. A demo must not quietly change that posture. So this creates
its own fixture source with lorem-ipsum bodies: real load, no real content, no
compliance implications.

SAFETY

Refuses to run when APP_ENV is production. This writes rows to whatever
DATABASE_URL points at, and DATABASE_URL in a developer's .env is very often
the live database — as it was here.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEMO_SOURCE_NAME = "Demo Fixture (synthetic)"
DEMO_URL_PREFIX = "https://demo.invalid/clarity/"

_BODY = (
    "This is synthetic fixture text generated for a load demonstration. "
    "It exists so the enrichment workers have something to claim and "
    "summarise. It is not a real news article and contains no reporting. "
    "The sentence structure is varied only enough to give an extractive "
    "summariser something to rank. Ingestion, classification and embedding "
    "have already completed for this row; only enrichment remains."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Seed a synthetic enrichment backlog")
    p.add_argument("--count", type=int, default=100, help="Articles to create (default 100)")
    p.add_argument("--clean", action="store_true", help="Delete the fixture source and its rows")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from sqlalchemy import delete, select

    from backend.database.orm_models import (
        CleanedArticle,
        RawArticle,
        Source,
        SourcePermission,
    )
    from backend.database.session import get_session
    from backend.urls import url_hash
    from config.settings import settings

    if settings.is_production:
        print(
            "REFUSING: APP_ENV is production.\n"
            "This writes fixture rows and must never touch a production database.",
            file=sys.stderr,
        )
        return 1

    print(f"database: {settings.DATABASE_URL.split('@')[-1]}")

    with get_session() as db:
        source = db.scalar(select(Source).where(Source.name == DEMO_SOURCE_NAME))

        if args.clean:
            if source is None:
                print("nothing to clean")
                return 0
            ids = list(db.scalars(select(RawArticle.id).where(RawArticle.source_id == source.id)))
            if ids:
                db.execute(delete(CleanedArticle).where(CleanedArticle.raw_article_id.in_(ids)))
                db.execute(delete(RawArticle).where(RawArticle.id.in_(ids)))
            db.execute(delete(SourcePermission).where(SourcePermission.source_id == source.id))
            db.execute(delete(Source).where(Source.id == source.id))
            print(f"removed fixture source and {len(ids)} articles")
            return 0

        if source is None:
            source = Source(
                name=DEMO_SOURCE_NAME,
                url="https://demo.invalid",
                feed_url="https://demo.invalid/rss.xml",
                is_active=False,  # never collected from; it has no real feed
            )
            db.add(source)
            db.flush()
            # Granted on the fixture only. No real publisher's posture changes.
            db.add(
                SourcePermission(
                    source_id=source.id,
                    can_store_full_text=True,
                    can_generate_summary=True,
                )
            )
            db.flush()
            print(f"created fixture source id={source.id}")

        existing = db.scalar(
            select(RawArticle.id)
            .where(RawArticle.source_id == source.id)
            .order_by(RawArticle.id.desc())
            .limit(1)
        )
        offset = int(existing or 0)

        now = datetime.now(timezone.utc)
        created = 0
        for i in range(args.count):
            url = f"{DEMO_URL_PREFIX}{offset}-{i}"
            raw = RawArticle(
                source_id=source.id,
                title=f"Synthetic fixture article {offset}-{i}",
                url=url,
                url_hash=url_hash(url),
                published_at=now - timedelta(minutes=i),
                # Already past the publish barrier, awaiting enrichment —
                # exactly the state the workers claim.
                ingest_status="PUBLISHED",
                enrichment_status="PENDING",
            )
            db.add(raw)
            db.flush()
            db.add(
                CleanedArticle(
                    raw_article_id=raw.id,
                    clean_text=_BODY,
                    word_count=len(_BODY.split()),
                    language="en",
                )
            )
            created += 1

        print(f"created {created} PENDING articles")

    with get_session() as db:
        from sqlalchemy import func

        pending = db.scalar(
            select(func.count(RawArticle.id))
            .where(RawArticle.enrichment_status == "PENDING")
            .where(RawArticle.ingest_status == "PUBLISHED")
        )
        print(f"backlog now: {pending} PENDING")
        print("\nwatch it drain:  kubectl -n clarity get pods -l app=clarity-worker -w")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
