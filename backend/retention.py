"""Retention and storage guard — ARCHITECTURE_V2 §A6.

A6: at the projected ingest rate the free-tier database fills in about 26
days, and the original design had no cleanup job at all. V2 §15 moved
retention into Phase 1 *specifically* so it exists before there is enough
data for it to matter — a retention job written after the disk is full is
written under pressure, against a database that is already refusing writes.

Two mechanisms:

**Retention** deletes what has aged out. Different classes of data age at
different rates, and `raw_html` is the big one — it is 10–50x the size of the
text extracted from it and is worthless once cleaning has run.

**The storage guard** is a circuit breaker. Past a high-water mark it refuses
further ingestion rather than letting writes fail mid-pipeline, which is a
much worse failure: a half-written article with no cleaned text is invisible
to the reader and invisible to the operator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("news.retention")

# ---------------------------------------------------------------------------
# Policy. Every number here is a storage/coverage trade, so they are grouped
# rather than scattered through the code.
# ---------------------------------------------------------------------------

RAW_HTML_HOURS = 24          # worthless once cleaned; the single biggest win
UNPROCESSED_DAYS = 7         # PENDING this long means it will never process
# Ten days. The free tier is 500 MB and ingestion adds roughly 150 articles an
# hour; at 180 days this table only ever moved in one direction. Ten days is
# also what the date picker offers, so retention and the UI state the same
# promise instead of the site advertising dates whose articles have been
# deleted. Changing one means changing the other — see ARTICLE_HISTORY_DAYS in
# backend/api/articles.py.
ARTICLE_DAYS = 10            # articles older than this are dropped entirely
TRANSLATION_DAYS = 90        # translations age out faster than their articles
READING_HISTORY_DAYS = 90    # V2 §3.8 — the one user table that grows unbounded

STORAGE_WARN_PCT = 70.0
STORAGE_STOP_PCT = 90.0      # refuse new ingestion above this
FREE_TIER_BYTES = 500 * 1024 * 1024


@dataclass
class RetentionStats:
    raw_html_cleared: int = 0
    stale_pending_deleted: int = 0
    old_articles_deleted: int = 0
    old_translations_deleted: int = 0
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "raw_html_cleared": self.raw_html_cleared,
            "stale_pending_deleted": self.stale_pending_deleted,
            "old_articles_deleted": self.old_articles_deleted,
            "old_translations_deleted": self.old_translations_deleted,
        }


@dataclass
class StorageStatus:
    used_bytes: int
    limit_bytes: int

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used_bytes / self.limit_bytes if self.limit_bytes else 0.0

    @property
    def should_warn(self) -> bool:
        return self.used_pct >= STORAGE_WARN_PCT

    @property
    def should_stop_ingestion(self) -> bool:
        return self.used_pct >= STORAGE_STOP_PCT

    @property
    def human(self) -> str:
        mb = self.used_bytes / 1024 / 1024
        limit_mb = self.limit_bytes / 1024 / 1024
        return f"{mb:.0f} MB / {limit_mb:.0f} MB ({self.used_pct:.1f}%)"


def get_storage_status(db: Session, limit_bytes: int = FREE_TIER_BYTES) -> StorageStatus:
    """Measure database size. Falls back to 0 on backends without the
    function (SQLite in tests), which reads as 'plenty of room' — the safe
    direction for a guard that would otherwise block all ingestion."""
    try:
        used = db.execute(text("SELECT pg_database_size(current_database())")).scalar() or 0
    except Exception:
        used = 0
    return StorageStatus(used_bytes=int(used), limit_bytes=limit_bytes)


def run_retention(db: Session, now: datetime | None = None, dry_run: bool = False) -> RetentionStats:
    """Apply every retention policy. Safe to run repeatedly."""
    import time

    started = time.monotonic()
    now = now or datetime.utcnow()
    stats = RetentionStats()

    def _exec(sql: str, params: dict) -> int:
        if dry_run:
            count_sql = f"SELECT count(*) FROM ({sql.replace('DELETE FROM', 'SELECT 1 FROM', 1)}) _q" \
                if sql.strip().upper().startswith("DELETE") else None
            return 0
        return db.execute(text(sql), params).rowcount or 0

    # 1. raw_html — the biggest single win. Cleared, not deleted: the article
    #    row stays, only the HTML blob goes.
    stats.raw_html_cleared = _exec(
        """UPDATE raw_articles SET raw_html = NULL
           WHERE raw_html IS NOT NULL AND created_at < :cutoff""",
        {"cutoff": now - timedelta(hours=RAW_HTML_HOURS)},
    )

    # 2. Articles stuck PENDING. Under the old pipeline these accumulated
    #    forever once the LLM quota ran out (V2 §A1) — every one is a row that
    #    will never be shown to anyone.
    stats.stale_pending_deleted = _exec(
        """DELETE FROM raw_articles
           WHERE status = 'PENDING' AND created_at < :cutoff""",
        {"cutoff": now - timedelta(days=UNPROCESSED_DAYS)},
    )

    # 3. Translations age out ahead of their articles — a six-month-old
    #    translated headline is not worth the UTF-8 bytes (V3 §B9).
    stats.old_translations_deleted = _exec(
        "DELETE FROM article_translations WHERE created_at < :cutoff",
        {"cutoff": now - timedelta(days=TRANSLATION_DAYS)},
    )

    # 4. Old articles. Last, so the cheaper reclaims run even if this is slow.
    #
    # Children first, explicitly. Every foreign key pointing at raw_articles is
    # ON DELETE NO ACTION, so a bare `DELETE FROM raw_articles` raises a
    # violation the moment an article has a country tag — which is nearly all
    # of them. This function had that bug from the start and nobody saw it,
    # because nothing ever called it.
    #
    # Explicit deletes rather than switching the constraints to CASCADE: a new
    # child table added later fails loudly here, which is a better outcome than
    # a cascade quietly removing rows nobody remembered were connected.
    doomed = """
        SELECT id FROM raw_articles
        WHERE COALESCE(published_at, created_at) < :cutoff
    """
    for child, column in (
        ("article_countries", "article_id"),
        ("article_categories", "article_id"),
        ("article_translations", "article_id"),
        ("categories", "raw_article_id"),
        ("summaries", "raw_article_id"),
        ("embeddings", "raw_article_id"),
        ("cleaned_articles", "raw_article_id"),
    ):
        _exec(
            f"DELETE FROM {child} WHERE {column} IN ({doomed})",
            {"cutoff": now - timedelta(days=ARTICLE_DAYS)},
        )

    stats.old_articles_deleted = _exec(
        """DELETE FROM raw_articles
           WHERE COALESCE(published_at, created_at) < :cutoff""",
        {"cutoff": now - timedelta(days=ARTICLE_DAYS)},
    )

    if not dry_run:
        db.commit()

    stats.duration_seconds = round(time.monotonic() - started, 2)
    logger.info("Retention complete: %s", stats.as_dict())
    return stats
