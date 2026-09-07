"""Enrichment worker — the below-the-barrier stage, run as a scalable pool.

WHY THIS EXISTS SEPARATELY FROM THE RUNNER

`backend/pipeline/runner.py` already ends with `_enrich()`, capped at
`enrichment_cap` articles per run and wrapped in a load-bearing `except` so an
LLM outage can never fail a run. That design is right for a single batch job in
GitHub Actions, and it has one consequence the runner's own docstring names:

    "Anything not otherwise resolved stays PENDING and is picked up by a
     later run. PENDING is a queue, not an error."

It is already a queue. This module is its consumer. Ingestion (deterministic,
local, free) stays a scheduled batch job; enrichment (slow, rate-limited,
dependent on someone else's uptime) becomes a pool of workers that scales
independently of it. That split is the reason this project runs on Kubernetes
at all — see docs/kubernetes.md.

WHY POSTGRES AND NOT REDIS

The obvious move is to put Redis in the middle and push article IDs onto a
list. It was not done, for three reasons:

1. `raw_articles.enrichment_status` is *already* the queue and already the
   source of truth. Adding Redis would mean two places that disagree about what
   still needs enriching, and the disagreement would surface as either
   double-summarised articles (wasted LLM budget, which is capped at
   LLM_DAILY_CALL_BUDGET) or silently dropped ones.
2. `SELECT ... FOR UPDATE SKIP LOCKED` is exactly the primitive a work queue
   needs, and Postgres has had it since 9.5. Each worker claims rows no other
   worker can see, in one round trip.
3. Crash recovery is free. A worker killed mid-batch never commits, the row
   locks die with its connection, and the articles are PENDING again — which is
   precisely the recovery path the runner already documents. A Redis list would
   need acknowledgements and a reaper to match that.

The honest limit: every worker polls the same table, so this stops scaling when
one Postgres can no longer serve the claim query — thousands of workers, not
the eight this project will ever run. Redis (or a real broker) is the answer
*then*, not now.

WHAT THIS MODULE MUST NEVER DO

Touch `ingest_status`. Everything here runs below the publish barrier. An
article that is PUBLISHED stays PUBLISHED whether or not it is ever summarised;
that is the guarantee the barrier exists to provide, and the reason a Groq
outage costs a summary rather than emptying the site.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.permissions import may_summarize
from config.settings import settings

logger = logging.getLogger("news.enrichment.worker")

# Mirrors the enrichment_status enum in alembic/versions/004. Duplicated as
# plain strings so reading this file tells you the whole state machine, which
# is the convention backend/pipeline/runner.py already follows.
ENRICH_PENDING = "PENDING"
ENRICH_DONE = "DONE"
ENRICH_FAILED = "FAILED"
ENRICH_SKIPPED = "SKIPPED"

INGEST_PUBLISHED = "PUBLISHED"


@dataclass
class WorkerStats:
    """Counters served on /metrics. Reset only by a restart."""

    claimed: int = 0
    summarized: int = 0
    failed: int = 0
    skipped: int = 0
    empty_polls: int = 0
    errors: int = 0
    instance: str = field(default_factory=lambda: os.getenv("POD_NAME", "local"))

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def _supports_skip_locked(db: Session) -> bool:
    """SKIP LOCKED is PostgreSQL-only.

    The test suite runs on SQLite, where the whole database is locked per write
    and the clause is a syntax error. Degrading to a plain SELECT there is safe
    because SQLite is never running two workers.
    """
    return db.bind.dialect.name == "postgresql"


def claim_pending(db: Session, limit: int) -> list[tuple[int, int, int]]:
    """Claim up to *limit* articles awaiting enrichment.

    Returns (raw_article_id, source_id, cleaned_article_id) triples, locked for
    the life of the caller's transaction. Another worker running this same query
    concurrently skips these rows entirely rather than blocking on them — that
    is what makes replicas additive instead of contended.

    Only PUBLISHED articles are considered. An article that has not cleared the
    barrier is not ours to enrich yet, and only articles with a cleaned body are
    summarisable at all.
    """
    from backend.database.orm_models import CleanedArticle, RawArticle

    stmt = (
        select(RawArticle.id, RawArticle.source_id, CleanedArticle.id)
        .join(CleanedArticle, CleanedArticle.raw_article_id == RawArticle.id)
        .where(RawArticle.enrichment_status == ENRICH_PENDING)
        .where(RawArticle.ingest_status == INGEST_PUBLISHED)
        # Oldest first: a backlog should drain in the order it accumulated,
        # otherwise a burst of new articles starves whatever is already waiting.
        .order_by(RawArticle.id)
        .limit(limit)
    )
    if _supports_skip_locked(db):
        # of=RawArticle: lock only the rows we are going to update. Without it
        # Postgres also locks the joined cleaned_articles rows, which nothing
        # here writes and another worker may legitimately need.
        stmt = stmt.with_for_update(skip_locked=True, of=RawArticle)

    return [(r[0], r[1], r[2]) for r in db.execute(stmt).all()]


def set_status(db: Session, ids: Sequence[int], value: str) -> None:
    """Set enrichment_status on the given articles. Never touches ingest_status."""
    from sqlalchemy import update

    from backend.database.orm_models import RawArticle

    if not ids:
        return
    db.execute(
        update(RawArticle)
        .where(RawArticle.id.in_(list(ids)))
        .values(enrichment_status=value)
        .execution_options(synchronize_session=False)
    )


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class EnrichmentWorker:
    """One process. Run as many as the backlog justifies."""

    def __init__(self, summarizer=None, batch_size: int | None = None) -> None:
        self.batch_size = batch_size or settings.ENRICH_BATCH_SIZE
        self.stats = WorkerStats()
        self._shutdown = threading.Event()
        self._summarizer = summarizer

    # -- lifecycle ---------------------------------------------------------

    @property
    def summarizer(self):
        """Built lazily so the process can start, and pass its readiness probe,
        even while the LLM provider is misconfigured."""
        if self._summarizer is None:
            from backend.llm.factory import get_llm_provider
            from backend.summarizer.summarizer import Summarizer

            self._summarizer = Summarizer(get_llm_provider())
        return self._summarizer

    def request_shutdown(self) -> None:
        self._shutdown.set()

    @property
    def stopping(self) -> bool:
        return self._shutdown.is_set()

    # -- one unit of work --------------------------------------------------

    def process_batch(self, db: Session) -> int:
        """Claim and enrich one batch. Returns how many articles were claimed.

        The caller owns the transaction. Everything here happens inside it, so a
        worker that dies before commit releases its claim rather than stranding
        articles in a state no one will retry.
        """
        claimed = claim_pending(db, self.batch_size)
        if not claimed:
            return 0
        self.stats.claimed += len(claimed)

        # Permission is per source and is checked here, not only at ingest: a
        # publisher's terms can be tightened between collection and enrichment,
        # and the stricter answer must win.
        from backend.database.orm_models import SourcePermission
        from backend.permissions import Permissions

        source_ids = {c[1] for c in claimed}
        rows = db.scalars(
            select(SourcePermission).where(SourcePermission.source_id.in_(source_ids))
        )
        by_id = {r.source_id: Permissions.from_row(r) for r in rows}
        perms = {sid: by_id.get(sid, Permissions.restrictive()) for sid in source_ids}

        eligible = [c for c in claimed if may_summarize(perms[c[1]])]
        not_allowed = [c[0] for c in claimed if not may_summarize(perms[c[1]])]

        if not_allowed:
            set_status(db, not_allowed, ENRICH_SKIPPED)
            self.stats.skipped += len(not_allowed)

        if not eligible:
            return len(claimed)

        from backend.database.models import CleanedArticleRecord

        records = [CleanedArticleRecord(id=c[2], raw_article_id=c[0]) for c in eligible]

        try:
            result = asyncio.run(self.summarizer.summarize_batch(db, records))
        except Exception as exc:
            # Same posture as the runner: enrichment failing is a summary lost,
            # never an article lost. Mark FAILED and keep serving.
            logger.error("Enrichment batch failed — articles remain published", exc_info=True)
            self.stats.errors += 1
            set_status(db, [c[0] for c in eligible], ENRICH_FAILED)
            return len(claimed)

        set_status(db, [c[0] for c in eligible], ENRICH_DONE)
        self.stats.summarized += getattr(result, "summarized", 0)
        self.stats.failed += getattr(result, "failed", 0)
        logger.info(
            "batch done: claimed=%d summarized=%s failed=%s",
            len(claimed),
            getattr(result, "summarized", 0),
            getattr(result, "failed", 0),
        )
        return len(claimed)

    # -- the loop ----------------------------------------------------------

    def run_forever(self, session_factory) -> None:
        """Poll for work until asked to stop.

        `session_factory` is a context manager yielding a Session and committing
        on clean exit — backend.database.session.get_session.
        """
        logger.info("enrichment worker ready (batch=%d)", self.batch_size)
        while not self.stopping:
            try:
                with session_factory() as db:
                    claimed = self.process_batch(db)
            except Exception:
                self.stats.errors += 1
                logger.error("worker iteration failed", exc_info=True)
                claimed = 0

            if claimed == 0:
                self.stats.empty_polls += 1
                # Interruptible sleep: a SIGTERM during the idle wait should
                # exit now, not up to ENRICH_POLL_SECONDS later. The pod's
                # termination grace period is finite.
                self._shutdown.wait(settings.ENRICH_POLL_SECONDS)

        logger.info("enrichment worker stopped: %s", self.stats.as_dict())

    def install_signal_handlers(self) -> None:
        """Finish the batch in hand, then exit.

        Kubernetes sends SIGTERM and waits terminationGracePeriodSeconds before
        SIGKILL. Because a claim only lasts as long as its transaction, a hard
        kill is survivable here — but draining cleanly still avoids re-doing
        LLM calls that were already paid for out of the daily budget.
        """

        def _handle(signum, _frame):
            logger.info("signal %s received — finishing current batch", signum)
            self.request_shutdown()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
