"""
Database session management for ClarityFeed — Neon PostgreSQL compatible.

Provides ``DatabaseSessionManager`` with Neon-specific engine parameters:
 - ``pool_pre_ping=True``:  issues SELECT 1 before every connection use to
   detect dead connections after Neon auto-resume.
 - ``pool_recycle=300``:  discards connections older than 5 min, forcing
   reconnection after Neon idle suspension.
 - ``connect_args={"sslmode": "require", "connect_timeout": 10}``:  SSL is
   mandatory for Neon; connect_timeout handles resume latency.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database.orm_models import Base
from config.settings import settings

logger = logging.getLogger("news.database.session")


def _build_connect_args() -> dict:
    """Build connect_args based on the database URL.

    For SQLite (used in testing), no special connect args are needed.
    For PostgreSQL (Neon), SSL mode and connect timeout are required.
    """
    if settings.DATABASE_URL.startswith("sqlite"):
        return {}
    return {
        "sslmode": "require",
        "connect_timeout": settings.DB_CONNECT_TIMEOUT,
    }


def _build_engine_kwargs() -> dict:
    """Build engine keyword arguments based on the database URL."""
    kwargs: dict = {}
    if settings.DATABASE_URL.startswith("sqlite"):
        # SQLite doesn't support pool_pre_ping or pool_recycle in the
        # same way as PostgreSQL. Use simple defaults.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = settings.DB_POOL_PRE_PING
        kwargs["pool_recycle"] = settings.DB_POOL_RECYCLE
        kwargs["connect_args"] = _build_connect_args()
    return kwargs


engine = create_engine(settings.DATABASE_URL, **_build_engine_kwargs())

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager that yields a database session and handles cleanup."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session.

    Usage::

        @router.get("/items")
        def list_items(db: Session = Depends(get_db)):
            ...
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Create all tables defined by the ORM models."""
    logger.info("Initialising database tables …")
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialised.")


def drop_all() -> None:
    """Drop all tables. Use in tests only."""
    logger.warning("Dropping all database tables!")
    Base.metadata.drop_all(bind=engine)


def init_db_with_retry(max_attempts: int = 5, delay: float = 5.0) -> None:
    """Retry ``init_db()`` up to *max_attempts* times with *delay* seconds
    between each attempt.

    This handles Neon cold starts during Render's initial startup, where
    the database may take several seconds to resume from suspension.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("init_db attempt %d/%d …", attempt, max_attempts)
            init_db()
            return
        except Exception:
            logger.warning(
                "init_db attempt %d/%d failed",
                attempt,
                max_attempts,
                exc_info=True,
            )
            if attempt == max_attempts:
                logger.error("All init_db attempts exhausted — raising.")
                raise
            time.sleep(delay)
