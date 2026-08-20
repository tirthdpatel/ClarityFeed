"""
ClarityFeed — FastAPI application entry point.

This is the main application module deployed to Render. It:
 - Includes the internal trigger router (``/internal/collect``)
 - Seeds default RSS sources on first startup
 - Provides ``GET /health`` and ``GET /sources`` endpoints
 - Configures CORS for the Vercel frontend
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.api.internal import router as internal_router
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

    # Fail fast on insecure production config (default INTERNAL_SECRET,
    # SQLite URL, wildcard CORS). No-op outside production. This runs before
    # anything binds, so a misconfigured deploy fails visibly instead of
    # coming up exposed.
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

# -- Routers ----------------------------------------------------------------
app.include_router(internal_router)


# -- Public endpoints -------------------------------------------------------


@app.get("/health")
def health_check() -> dict:
    """Basic health check endpoint."""
    return {
        "status": "ok",
        "version": "0.1.0",
        "platform": "render-free-tier",
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
