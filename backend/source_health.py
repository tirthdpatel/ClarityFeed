"""Source health tracking and the circuit breaker — ARCHITECTURE_V2 §A8.

A8 found that two of ten seed sources were dead or proxied, and nothing in
the system noticed. Feeds die silently: the URL keeps resolving, the server
keeps answering 200, and the response contains zero entries forever. Coverage
for that country quietly goes to nothing and the only symptom is an empty
country page.

**The key decision: zero articles counts as a failure.** Keying off HTTP
status alone would never catch it — the failure mode is a healthy-looking
200 with an empty body. `articles_returned` is the column that matters.

The breaker deliberately does not delete or permanently disable anything. It
sets `is_active = false` and records why, so a source that broke because of a
transient upstream problem is one click away in the admin panel rather than
gone from the seed list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

logger = logging.getLogger("news.source_health")

# Six consecutive empty or failing runs. At hourly cadence that is six hours
# of silence — long enough to ride out a publisher's maintenance window,
# short enough that a genuinely dead feed surfaces the same day.
BREAKER_THRESHOLD = 6

# Warn in the dashboard well before disabling, so a human can look first.
WARN_THRESHOLD = 3


@dataclass
class FetchOutcome:
    """What happened when one source was fetched."""

    source_id: int
    ok: bool
    http_status: int | None = None
    latency_ms: int | None = None
    articles_returned: int = 0
    error_type: str | None = None
    error_detail: str | None = None

    @property
    def is_effective_failure(self) -> bool:
        """A 200 with no entries is a failure, whatever the transport said."""
        return (not self.ok) or self.articles_returned == 0


def record_outcome(db: Session, outcome: FetchOutcome, now: datetime | None = None) -> bool:
    """Record a fetch result and apply the breaker.

    Returns True if this call disabled the source.
    """
    from backend.database.orm_models import Source, SourceHealth

    now = now or datetime.utcnow()

    db.add(SourceHealth(
        source_id=outcome.source_id,
        checked_at=now,
        ok=not outcome.is_effective_failure,
        http_status=outcome.http_status,
        latency_ms=outcome.latency_ms,
        articles_returned=outcome.articles_returned,
        error_type=outcome.error_type or ("empty_feed" if outcome.ok and outcome.articles_returned == 0 else None),
        error_detail=outcome.error_detail,
    ))

    source = db.get(Source, outcome.source_id)
    if source is None:
        return False

    disabled = False
    if outcome.is_effective_failure:
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        source.last_error_at = now
        source.last_error = outcome.error_detail or (
            "Feed returned 0 articles" if outcome.ok else outcome.error_type or "unknown error"
        )
        if source.consecutive_failures >= BREAKER_THRESHOLD and source.is_active:
            source.is_active = False
            disabled = True
            logger.error(
                "CIRCUIT BREAKER: disabling source %s (%s) after %d consecutive failures — %s",
                source.id, source.name, source.consecutive_failures, source.last_error,
            )
        elif source.consecutive_failures >= WARN_THRESHOLD:
            logger.warning(
                "Source %s (%s) has failed %d consecutive runs — %s",
                source.id, source.name, source.consecutive_failures, source.last_error,
            )
    else:
        # Recovery resets the counter. A feed that works again is healthy;
        # carrying old failures forward would eventually trip the breaker on
        # a source that has been fine for weeks.
        if source.consecutive_failures:
            logger.info(
                "Source %s (%s) recovered after %d failures",
                source.id, source.name, source.consecutive_failures,
            )
        source.consecutive_failures = 0
        source.last_success_at = now

    return disabled


def failing_sources(db: Session, threshold: int = WARN_THRESHOLD) -> list:
    """Sources at or above the warning threshold, worst first.

    This is the single most useful view in the admin dashboard — it is how a
    coverage hole gets noticed before a reader finds it.
    """
    from backend.database.orm_models import Source

    return list(
        db.query(Source)
        .filter(Source.consecutive_failures >= threshold)
        .order_by(Source.consecutive_failures.desc())
        .all()
    )
