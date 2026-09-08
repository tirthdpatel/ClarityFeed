"""
ClarityFeed — FastAPI application entry point.

This is the main application module deployed to Render. It:
 - Serves the public read API (``/articles``) and reference data
 - Seeds default RSS sources on first startup
 - Provides ``GET /health``, ``GET /ready`` and ``GET /sources`` endpoints
 - Configures CORS for the Vercel frontend
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.api.articles import router as articles_router
from backend.api.middleware import NoIndexMiddleware, RateLimitMiddleware
from backend.api.reference import router as reference_router
from backend.collector.feed_sources import FeedSourceRepository
from backend.database.session import get_db, init_db_with_retry
from config.settings import settings

logger = logging.getLogger("news.api.main")

# Configure root logger
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler — startup and shutdown logic."""
    # ---- Startup ----
    logger.info("ClarityFeed starting up on Render free tier …")

    # Fail fast on insecure production config (SQLite URL, wildcard CORS).
    # No-op outside production. This runs before anything binds, so a
    # misconfigured deploy fails visibly instead of coming up exposed.
    settings.validate_or_die()

    init_db_with_retry()

    # Seed default sources if the table is empty
    from backend.database.session import SessionLocal

    db = SessionLocal()
    try:
        repo = FeedSourceRepository(db)
        seeded = repo.seed_default_sources()
        if seeded:
            logger.info("Seeded %d default RSS sources on first startup.", seeded)
    except Exception:
        logger.error("Failed to seed default sources", exc_info=True)
    finally:
        db.close()

    logger.info("ClarityFeed startup complete.")
    yield

    # ---- Shutdown ----
    logger.info("ClarityFeed shutting down.")


app = FastAPI(
    title="ClarityFeed",
    description="AI-powered international news aggregation and distillation",
    version="0.1.0",
    lifespan=lifespan,
)

# -- CORS ------------------------------------------------------------------
#
# `allow_origins=["*"]` together with `allow_credentials=True` was both
# insecure and non-functional: the Fetch spec forbids the wildcard on
# credentialed requests, so browsers reject the response outright, and if it
# had worked it would have exposed the authenticated admin surface to every
# origin on the internet.
#
# Resolution: credentials require an explicit origin allow-list. If a
# wildcard is configured we honour it but force credentials off, so the
# permissive-for-local-dev case still works and the dangerous combination
# is unrepresentable.
_origins = settings.allowed_origins
_wildcard = "*" in _origins

if _wildcard and settings.CORS_ALLOW_CREDENTIALS:
    logger.warning(
        "FRONTEND_URL is '*' — disabling CORS credentials. "
        "Set FRONTEND_URL to an explicit comma-separated origin list in production."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _wildcard else _origins,
    allow_credentials=False if _wildcard else settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

# Starlette runs middleware in reverse registration order, so these two execute
# before CORS: a rate-limited request still needs its CORS headers, or the
# browser reports an opaque network failure instead of the 429 that would tell
# you what actually happened.
app.add_middleware(NoIndexMiddleware)
app.add_middleware(
    RateLimitMiddleware,
    rate=settings.RATE_LIMIT_REQUESTS,
    window=settings.RATE_LIMIT_WINDOW_SECONDS,
)

# -- Routers ----------------------------------------------------------------
app.include_router(articles_router)
app.include_router(reference_router)


# -- Public endpoints -------------------------------------------------------


@app.get("/health")
def health_check() -> dict:
    """Liveness. Is this process alive?

    Deliberately touches nothing external. Kubernetes uses this as its
    livenessProbe, and a liveness probe that checked Postgres would respond to
    a database outage by restarting every API pod — turning one broken
    dependency into a cluster-wide crash loop while fixing nothing. Restarting
    a pod cannot repair someone else's database.

    ``instance`` is the pod name, injected by the downward API. It is how you
    can see the Service load balancing across replicas from the browser.
    """
    return {
        "status": "ok",
        "version": "0.1.0",
        "platform": "render-free-tier",
        "instance": os.getenv("POD_NAME", "local"),
    }


@app.get("/ready")
def readiness_check(db: Session = Depends(get_db)) -> dict:
    """Readiness. Can this pod serve traffic right now?

    This one *does* check the database, which is the difference between it and
    /health. Failing removes the pod from the Service's endpoints without
    restarting it, so requests go to healthy replicas and this pod rejoins by
    itself once Neon resumes or Postgres comes back.

    Returns 503 rather than raising, so the probe sees a definite negative
    instead of a connection reset.
    """
    from sqlalchemy import text

    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the probe must not itself crash
        logger.warning("readiness: database check failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"status": "not ready", "database": False},
        ) from exc

    return {
        "status": "ready",
        "database": True,
        "instance": os.getenv("POD_NAME", "local"),
    }


@app.get("/stats/pipeline")
def pipeline_stats(db: Session = Depends(get_db)) -> dict:
    """Where every article currently sits in the pipeline.

    This is the endpoint the scaling demo watches: `pending_enrichment` is the
    backlog the workers are draining, and it is the same number KEDA's
    PostgreSQL scaler queries to decide how many workers to run
    (k8s/40-keda-scaledobject.yaml).

    Cheap enough to poll: two grouped counts over indexed columns.
    """
    from sqlalchemy import func, select

    from backend.database.orm_models import RawArticle

    def counts(column) -> dict[str, int]:
        rows = db.execute(select(column, func.count(RawArticle.id)).group_by(column)).all()
        return {str(value): int(count) for value, count in rows}

    by_enrichment = counts(RawArticle.enrichment_status)
    by_ingest = counts(RawArticle.ingest_status)

    return {
        "ingest": by_ingest,
        "enrichment": by_enrichment,
        # Named separately because it is the one number that drives autoscaling.
        # PUBLISHED-only: an article that has not cleared the barrier is not
        # work the enrichment workers can claim yet.
        "pending_enrichment": int(
            db.scalar(
                select(func.count(RawArticle.id))
                .where(RawArticle.enrichment_status == "PENDING")
                .where(RawArticle.ingest_status == "PUBLISHED")
            )
            or 0
        ),
        "published": by_ingest.get("PUBLISHED", 0),
        "instance": os.getenv("POD_NAME", "local"),
    }


@app.get("/sources")
def list_sources(db: Session = Depends(get_db)) -> list[dict]:
    """Return all active RSS sources."""
    repo = FeedSourceRepository(db)
    sources = repo.get_active_sources()
    return [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "feed_url": s.feed_url,
            "language": s.language,
            "country": s.country,
            "category": s.category,
            "is_active": s.is_active,
        }
        for s in sources
    ]
