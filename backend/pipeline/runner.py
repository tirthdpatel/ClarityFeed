"""The ingestion pipeline orchestrator — ARCHITECTURE_V2 §A3.

This module composes stages that already exist. It contains almost no
domain logic of its own, and that is deliberate: the value here is the
*order*, and one line in the middle of it.

    open ingestion_runs row
      | storage guard          abort above 90% rather than fail mid-write
      | load active sources
      | per source: fetch -> record_outcome() -> circuit breaker
      | permission gate        BEFORE anything is written (see note below)
      | insert new articles, deduplicated on url_hash
      | body fetch + clean     only where can_store_full_text
      | classification         country + category, zero API calls
      | embedding              local MiniLM -> embeddings.vector
      | ============== PUBLISH BARRIER: ingest_status = PUBLISHED ==============
      | enrichment             best-effort, capped, NEVER blocks
    close ingestion_runs row

**Why the barrier is the whole point.** Before it, the site's visible
content depended on an LLM call succeeding. When Groq deprecated both
models the pipeline stopped mid-run and the site emptied out — not because
the articles were missing, but because they never reached a state the API
would serve. Everything above the barrier is deterministic, local, and
free. Everything below it is allowed to fail, and its failure costs a
reader a summary, never an article.

The practical rule this imposes on future work: **if a stage can be broken
by someone else's outage, it goes below the barrier.** Translation (V3 §B2)
belongs there. Classification deliberately does not, which is why it was
rewritten to be rule-based.

NOTE ON GATE ORDER. The handoff sketch placed `apply_ingest_gate()` after
the insert step. That ordering cannot be right: `backend/permissions.py`
documents the ingest gate as running "before anything is stored", and a
gate applied after the INSERT has already stored the field it was meant to
drop. The gate runs here on parsed feed entries, before any row is written.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from backend.database.models import PipelineStatus
from backend.permissions import Permissions, apply_ingest_gate, may_summarize
from backend.retention import get_storage_status
from backend.source_health import FetchOutcome, record_outcome
from backend.urls import url_hash

logger = logging.getLogger("news.pipeline.runner")

# ---------------------------------------------------------------------------
# Ingest states. These mirror the PostgreSQL enum created in migration 004
# (`ingest_status`), which is the authority. Duplicated as plain strings so
# that reading this file tells you the whole state machine.
# ---------------------------------------------------------------------------

INGEST_PENDING = "PENDING"
INGEST_FETCHED = "FETCHED"
INGEST_CLEANED = "CLEANED"
INGEST_PUBLISHED = "PUBLISHED"
INGEST_FAILED = "FAILED"

ENRICH_PENDING = "PENDING"
ENRICH_DONE = "DONE"
ENRICH_FAILED = "FAILED"
ENRICH_SKIPPED = "SKIPPED"

RUN_RUNNING = "running"
RUN_OK = "ok"
RUN_PARTIAL = "partial"
RUN_ABORTED = "aborted"
RUN_FAILED = "failed"

#: Enrichment is capped per run for a reason that is not cost: an unbounded
#: enrichment phase on a backlog of 5,000 articles turns an hourly job into
#: a six-hour job, and the next cron tick starts before this one finishes.
DEFAULT_ENRICHMENT_CAP = 40

#: Chunk size for bulk writes. Latency to Frankfurt is the binding
#: constraint, so the shape that matters is "few round trips", not
#: "small statements" (standing decision: bulk operations only).
BULK_CHUNK = 500


@dataclass
class PipelineConfig:
    """One run's parameters. Everything here maps to a CLI flag."""

    dry_run: bool = False
    limit: int | None = None
    sources: tuple[str, ...] = ()
    skip_enrichment: bool = False
    trigger: str = "cron"
    enrichment_cap: int = DEFAULT_ENRICHMENT_CAP
    #: Body fetching is the slowest stage and is only legal for a minority
    #: of sources. Off by default in dry runs.
    fetch_bodies: bool = True


@dataclass
class StageTimings:
    """Wall-clock per stage. The first thing to look at when an hourly job
    stops fitting in an hour."""

    collect: float = 0.0
    insert: float = 0.0
    bodies: float = 0.0
    classify: float = 0.0
    embed: float = 0.0
    publish: float = 0.0
    enrich: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "collect": round(self.collect, 2),
            "insert": round(self.insert, 2),
            "bodies": round(self.bodies, 2),
            "classify": round(self.classify, 2),
            "embed": round(self.embed, 2),
            "publish": round(self.publish, 2),
            "enrich": round(self.enrich, 2),
        }


@dataclass
class RunReport:
    """What happened. Mirrors the `ingestion_runs` row and is what the CLI
    prints and what the exit code is derived from."""

    run_id: int | None = None
    status: str = RUN_RUNNING
    trigger: str = "cron"
    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None

    sources_attempted: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    sources_disabled: list[str] = field(default_factory=list)

    entries_seen: int = 0
    articles_new: int = 0
    articles_duplicate: int = 0
    articles_gated: int = 0
    articles_published: int = 0

    bodies_extracted: int = 0
    embedded: int = 0
    classified: int = 0
    enriched: int = 0
    llm_calls_used: int = 0

    storage_pct: float = 0.0
    errors: list[str] = field(default_factory=list)
    timings: StageTimings = field(default_factory=StageTimings)

    @property
    def ok(self) -> bool:
        """A run is ok if the barrier was reached without an unhandled error.

        Deliberately not "nothing went wrong": a dead source is expected
        and is what the circuit breaker exists to absorb. Exit code 1 on a
        single failing feed would page someone every night.
        """
        return self.status in (RUN_OK, RUN_PARTIAL)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "sources": f"{self.sources_ok}/{self.sources_attempted} ok",
            "sources_disabled": self.sources_disabled,
            "entries_seen": self.entries_seen,
            "articles_new": self.articles_new,
            "articles_duplicate": self.articles_duplicate,
            "articles_gated": self.articles_gated,
            "articles_published": self.articles_published,
            "bodies_extracted": self.bodies_extracted,
            "classified": self.classified,
            "embedded": self.embedded,
            "enriched": self.enriched,
            "llm_calls_used": self.llm_calls_used,
            "storage_pct": round(self.storage_pct, 1),
            "errors": self.errors[:10],
            "timings": self.timings.as_dict(),
        }


# ===========================================================================
# The runner
# ===========================================================================


class PipelineRunner:
    """Runs one ingestion cycle.

    Every collaborator is injectable. That is not ceremony — the barrier
    invariant ("enrichment failure must not unpublish anything") is only
    testable if enrichment can be made to fail on demand.
    """

    def __init__(
        self,
        config: PipelineConfig | None = None,
        *,
        rss_reader: Any = None,
        body_reader: Any = None,
        embedder: Any = None,
        summarizer: Any = None,
        clock: Any = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self._rss = rss_reader
        self._bodies = body_reader
        self._embedder = embedder
        self._summarizer = summarizer
        self._clock = clock or datetime.utcnow
        self._vector_type: str | None = None
        self._loop = None

    # -- coroutine bridge --------------------------------------------------

    def _await(self, coro: Any) -> Any:
        """Drive one coroutine to completion on this run's event loop.

        The runner is deliberately synchronous. It is a batch job: it reads
        a few feeds, writes some rows and exits, and every stage depends on
        the one before it, so there is nothing for concurrency to overlap at
        this level. Three of its collaborators happen to be coroutines
        (RSS reading, body extraction, summarisation) and each is driven
        here on a single loop owned by the run.

        One loop rather than `asyncio.run()` per call, because the rate
        limiter and connection pools inside those collaborators are keyed to
        a loop and recreating it per source would quietly discard them.
        """
        import asyncio

        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _close_loop(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:
                logger.debug("Event loop close failed", exc_info=True)
        self._loop = None

    # -- lazy collaborators ------------------------------------------------
    #
    # Imported on first use, never at module import. sentence-transformers
    # pulls in torch and costs seconds; the API imports this module's
    # siblings and must not pay for a stage it never runs.

    @property
    def rss(self) -> Any:
        if self._rss is None:
            from backend.collector.rss_fetcher import RSSFetcher

            self._rss = RSSFetcher()
        return self._rss

    @property
    def embedder(self) -> Any:
        if self._embedder is None:
            from backend.deduplicator.local_embedder import LocalEmbedder

            self._embedder = LocalEmbedder()
        return self._embedder

    @property
    def bodies(self) -> Any:
        if self._bodies is None:
            from backend.fetcher.article_fetcher import ArticleFetcher

            self._bodies = ArticleFetcher()
        return self._bodies

    @property
    def summarizer(self) -> Any:
        if self._summarizer is None:
            from backend.llm.factory import get_provider
            from backend.summarizer.summarizer import Summarizer

            self._summarizer = Summarizer(get_provider())
        return self._summarizer

    # =====================================================================
    # Entry point
    # =====================================================================

    def run(self, db: Session) -> RunReport:
        """Execute one full cycle.

        Never raises for an expected failure — the report and the
        `ingestion_runs` row are the interface. A caller that wants an exit
        code reads `report.ok`.
        """
        cfg = self.config
        report = RunReport(
            trigger=cfg.trigger, dry_run=cfg.dry_run, started_at=self._clock()
        )
        run_row = self._open_run(db, report)

        try:
            # ---- storage guard ------------------------------------------
            status = get_storage_status(db)
            report.storage_pct = status.used_pct
            if status.should_stop_ingestion:
                # Refusing is the *safe* failure. Writing until the disk
                # rejects a statement leaves half-written articles that are
                # invisible to readers and to the operator alike (§A6).
                msg = f"Storage guard tripped at {status.human} — ingestion refused"
                logger.error(msg)
                report.errors.append(msg)
                report.status = RUN_ABORTED
                return self._close_run(db, run_row, report)
            if status.should_warn:
                logger.warning("Storage at %s — above the warning mark", status.human)

            # ---- collect -------------------------------------------------
            t = time.monotonic()
            sources = self._load_sources(db)
            report.sources_attempted = len(sources)
            if not sources:
                report.errors.append(
                    "No active sources matched — nothing to ingest."
                )
                report.status = RUN_PARTIAL
                return self._close_run(db, run_row, report)

            harvested = self._collect(db, sources, report)
            report.timings.collect = time.monotonic() - t

            # ---- permission gate, then insert ----------------------------
            t = time.monotonic()
            new_ids = self._gate_and_insert(db, harvested, report)
            report.timings.insert = time.monotonic() - t

            if cfg.dry_run:
                logger.info("Dry run — rolling back before anything is published")
                db.rollback()
                report.status = RUN_OK
                return self._close_run(db, run_row, report)

            if not new_ids:
                logger.info("No new articles this run")
                report.status = RUN_OK
                return self._close_run(db, run_row, report)

            articles = self._load_articles(db, new_ids)

            # ---- bodies, only where the licence allows -------------------
            if cfg.fetch_bodies:
                t = time.monotonic()
                self._extract_bodies(db, articles, report)
                report.timings.bodies = time.monotonic() - t

            # ---- classification (zero API calls) -------------------------
            t = time.monotonic()
            self._classify(db, articles, report)
            report.timings.classify = time.monotonic() - t

            # ---- embeddings ----------------------------------------------
            t = time.monotonic()
            self._embed(db, articles, report)
            report.timings.embed = time.monotonic() - t

            # ==============================================================
            #                      PUBLISH BARRIER
            # Everything above had to succeed. Everything below may fail.
            # ==============================================================
            t = time.monotonic()
            report.articles_published = self._publish(db, new_ids)
            report.timings.publish = time.monotonic() - t
            logger.info(
                "PUBLISH BARRIER crossed: %d articles are now visible",
                report.articles_published,
            )
            report.status = RUN_PARTIAL if report.sources_failed else RUN_OK

            # ---- enrichment: best-effort, capped, never blocking ---------
            if cfg.skip_enrichment:
                self._set_enrichment(db, new_ids, ENRICH_SKIPPED)
            else:
                t = time.monotonic()
                self._enrich(db, new_ids, report)
                report.timings.enrich = time.monotonic() - t

        except Exception as exc:
            # Reaching here means a stage *above* the barrier failed, or
            # something unforeseen did. Articles already published in an
            # earlier run are untouched either way.
            logger.error("Ingestion run failed", exc_info=True)
            report.errors.append(f"{type(exc).__name__}: {exc}")
            report.status = RUN_FAILED
            try:
                db.rollback()
            except Exception:
                pass

        return self._close_run(db, run_row, report)

    # =====================================================================
    # Run bookkeeping
    # =====================================================================

    def _open_run(self, db: Session, report: RunReport) -> Any:
        """Open the `ingestion_runs` row and commit it immediately.

        Committed up front on purpose: a run that dies hard still leaves a
        row with status='running' and no finished_at, which is exactly the
        signal the admin dashboard needs. A row written only at the end
        would make a crashed run indistinguishable from one that never
        started.
        """
        from backend.database.orm_models import IngestionRun

        row = IngestionRun(
            started_at=report.started_at or self._clock(),
            trigger=report.trigger,
            status=RUN_RUNNING,
            git_sha=(os.environ.get("GITHUB_SHA") or "")[:40] or None,
        )
        db.add(row)
        db.commit()
        report.run_id = row.id
        logger.info("Ingestion run %s opened (trigger=%s)", row.id, report.trigger)
        return row

    def _close_run(self, db: Session, row: Any, report: RunReport) -> RunReport:
        self._close_loop()
        report.finished_at = self._clock()
        try:
            row.finished_at = report.finished_at
            row.status = report.status
            row.sources_attempted = report.sources_attempted
            row.sources_ok = report.sources_ok
            row.sources_failed = report.sources_failed
            row.articles_new = report.articles_new
            row.articles_duplicate = report.articles_duplicate
            row.articles_published = report.articles_published
            row.llm_calls_used = report.llm_calls_used
            row.error_summary = "\n".join(report.errors[:20]) or None
            db.commit()
        except Exception:
            logger.error("Could not close ingestion_runs row", exc_info=True)
            db.rollback()
        logger.info("Ingestion run %s finished: %s", report.run_id, report.as_dict())
        return report

    # =====================================================================
    # Stage 1 — collect
    # =====================================================================

    def _load_sources(self, db: Session) -> list:
        """Active sources, optionally filtered by --sources.

        The filter matches on name or feed URL substring, case-insensitively,
        because typing an integer id from memory is not a thing anyone does.
        """
        from backend.database.orm_models import Source

        rows = list(db.scalars(select(Source).where(Source.is_active.is_(True))))
        wanted = tuple(s.strip().lower() for s in self.config.sources if s.strip())
        if wanted:
            rows = [
                r
                for r in rows
                if any(
                    w in (r.name or "").lower() or w in (r.feed_url or "").lower()
                    for w in wanted
                )
            ]
        return rows

    def _read_feed(self, source: Any) -> tuple[list[dict], dict]:
        """One feed read, returning entries plus transport metadata.

        Prefers `fetch_feed_detailed()` when the reader exposes it, because
        `SourceHealth.http_status` is otherwise always NULL — and A8 exists
        precisely because the interesting failure is a *healthy-looking*
        200 with an empty body. Falls back to the plain call so injected
        test doubles stay simple.
        """
        reader = self.rss
        detailed = getattr(reader, "fetch_feed_detailed", None)
        if detailed is not None:
            return self._await(detailed(source))
        entries = self._await(reader.fetch_feed(source))
        # ok=True means the *transport* succeeded. Emptiness is judged
        # separately by FetchOutcome, and the distinction is the whole
        # point of A8: "connection refused" and "200 with nothing in it"
        # are both failures, but only the second one is invisible.
        return list(entries or []), {"ok": True, "http_status": None}

    def _collect(
        self, db: Session, sources: Sequence[Any], report: RunReport
    ) -> list[tuple[Any, list[dict]]]:
        """Read every source. One dead feed must not end the run."""
        harvest: list[tuple[Any, list[dict]]] = []

        for source in sources:
            started = time.monotonic()
            entries: list[dict] = []
            meta: dict = {}
            try:
                entries, meta = self._read_feed(source)
            except Exception as exc:
                logger.error("Source %s (%s) raised", source.id, source.name, exc_info=True)
                meta = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc)[:500],
                }

            latency_ms = int((time.monotonic() - started) * 1000)
            outcome = FetchOutcome(
                source_id=source.id,
                ok=bool(meta.get("ok", bool(entries))),
                http_status=meta.get("http_status"),
                latency_ms=latency_ms,
                articles_returned=len(entries),
                error_type=meta.get("error_type"),
                error_detail=meta.get("error_detail"),
            )

            if outcome.is_effective_failure:
                report.sources_failed += 1
                report.errors.append(
                    f"{source.name}: {outcome.error_type or 'empty feed'}"
                )
            else:
                report.sources_ok += 1

            # A dry run must not move any source toward the circuit breaker.
            # Health is an observation about a real read; recording one for a
            # rehearsal would disable feeds on the strength of runs that
            # never happened.
            if not self.config.dry_run:
                if record_outcome(db, outcome):
                    report.sources_disabled.append(source.name)

            report.entries_seen += len(entries)
            if entries:
                harvest.append((source, entries))

        if not self.config.dry_run:
            db.commit()
        return harvest

    # =====================================================================
    # Stage 2 — permission gate, then insert
    # =====================================================================

    def _permissions_for(self, db: Session, source_ids: Iterable[int]) -> dict[int, Permissions]:
        """Resolve permissions for every source in one query.

        A source with no row gets `Permissions.restrictive()` — headline,
        short excerpt, link, nothing else. That is the whole posture of §11:
        an unreviewed publisher is not a permissive one.
        """
        from backend.database.orm_models import SourcePermission

        ids = list(source_ids)
        if not ids:
            return {}
        rows = db.scalars(
            select(SourcePermission).where(SourcePermission.source_id.in_(ids))
        )
        by_id = {r.source_id: Permissions.from_row(r) for r in rows}
        return {sid: by_id.get(sid, Permissions.restrictive()) for sid in ids}

    def _gate_and_insert(
        self,
        db: Session,
        harvest: Sequence[tuple[Any, list[dict]]],
        report: RunReport,
    ) -> list[int]:
        """Apply the ingest gate, then bulk-insert what survived.

        The gate runs *first*. `backend/permissions.py` specifies it as
        running before anything is stored, and it means it literally: a
        field dropped after the INSERT has already been stored.
        """
        from backend.compliance import validate_attribution
        from backend.database.orm_models import RawArticle

        if not harvest:
            return []

        perms_by_source = self._permissions_for(db, {s.id for s, _ in harvest})

        # hash -> row. Deduplicates within the batch as well as against the
        # database: two feeds carrying the same syndicated story in one run
        # would otherwise race to insert the same url_hash.
        candidates: dict[str, dict] = {}
        collisions = 0

        for source, entries in harvest:
            perms = perms_by_source.get(source.id, Permissions.restrictive())
            for entry in entries:
                url = (entry.get("url") or "").strip()
                if not url:
                    continue
                if not validate_attribution({**entry, "source_name": entry.get("source_name") or source.name}):
                    logger.warning("Dropping unattributed article: %s", url)
                    continue

                gated = apply_ingest_gate(
                    {
                        "url": url,
                        "title": entry.get("title") or "",
                        "description": entry.get("summary") or "",
                        "full_text": None,
                        "image_url": entry.get("image_url"),
                    },
                    perms,
                )
                if gated.changed:
                    report.articles_gated += 1

                title = (gated.fields.get("title") or "").strip()
                if not title:
                    # A source that does not licence its headlines cannot be
                    # represented at all — an untitled row is not an article.
                    continue

                h = url_hash(url)
                if h in candidates:
                    # Two feeds carrying the same syndicated story in one
                    # run. Counted as a duplicate even though it never
                    # reached the database, because that is what it is.
                    collisions += 1
                candidates.setdefault(
                    h,
                    {
                        "source_id": source.id,
                        "url": url,
                        "url_hash": h,
                        "title": title[:1024],
                        "published_at": entry.get("published"),
                        "summary_from_feed": gated.fields.get("description"),
                        "status": PipelineStatus.PENDING,
                        "ingest_status": INGEST_PENDING,
                        "enrichment_status": ENRICH_PENDING,
                    },
                )

        if not candidates:
            return []

        # One SELECT for the whole batch, not one per article. The old
        # inserter did a query per row; against Frankfurt from a US runner
        # that is the difference between one second and several minutes.
        existing: set[str] = set()
        hashes = list(candidates)
        for i in range(0, len(hashes), BULK_CHUNK):
            chunk = hashes[i : i + BULK_CHUNK]
            existing.update(
                db.scalars(
                    select(RawArticle.url_hash).where(RawArticle.url_hash.in_(chunk))
                )
            )

        fresh = [row for h, row in candidates.items() if h not in existing]
        report.articles_duplicate = collisions + (len(candidates) - len(fresh))
        report.articles_new = len(fresh)

        if self.config.limit is not None and len(fresh) > self.config.limit:
            fresh = fresh[: self.config.limit]
            report.articles_new = len(fresh)
            logger.info("--limit applied: keeping %d articles", len(fresh))

        if not fresh:
            return []

        if self.config.dry_run:
            logger.info("Dry run — %d articles would be inserted", len(fresh))
            return []

        for i in range(0, len(fresh), BULK_CHUNK):
            db.execute(RawArticle.__table__.insert(), fresh[i : i + BULK_CHUNK])
        db.commit()

        inserted_hashes = [r["url_hash"] for r in fresh]
        ids: list[int] = []
        for i in range(0, len(inserted_hashes), BULK_CHUNK):
            chunk = inserted_hashes[i : i + BULK_CHUNK]
            ids.extend(
                db.scalars(
                    select(RawArticle.id).where(RawArticle.url_hash.in_(chunk))
                )
            )
        logger.info("Inserted %d new articles", len(ids))
        return ids

    def _load_articles(self, db: Session, ids: Sequence[int]) -> list:
        from backend.database.orm_models import RawArticle

        out: list = []
        for i in range(0, len(ids), BULK_CHUNK):
            out.extend(
                db.scalars(
                    select(RawArticle).where(RawArticle.id.in_(ids[i : i + BULK_CHUNK]))
                )
            )
        return out

    # =====================================================================
    # Stage 3 — bodies (only where licensed)
    # =====================================================================

    def _extract_bodies(
        self, db: Session, articles: Sequence[Any], report: RunReport
    ) -> None:
        """Retrieve and extract article bodies.

        Restricted to sources with `can_store_full_text`. For everyone else
        the feed excerpt is all we are allowed to hold, so there is nothing
        to retrieve — and not making the request is also the polite thing to
        do to a publisher who said no.
        """
        from backend.database.models import RawArticleRecord

        perms = self._permissions_for(db, {a.source_id for a in articles})
        allowed = [a for a in articles if perms[a.source_id].can_store_full_text]
        if not allowed:
            logger.info("No sources licence full text — skipping body extraction")
            return

        records = [RawArticleRecord(id=a.id, source_id=a.source_id, url=a.url, title=a.title) for a in allowed]
        try:
            result = self._await(self.bodies.fetch_and_extract_batch(db, records))
            report.bodies_extracted = getattr(result, "extracted", 0)
            logger.info("Body extraction: %s", result)
        except Exception as exc:
            # Extraction is above the barrier but is not required for an
            # article to be publishable: a headline, excerpt and link is a
            # complete unit. Degrade, do not abort.
            logger.error("Body extraction failed wholesale", exc_info=True)
            report.errors.append(f"bodies: {type(exc).__name__}: {exc}")

    # =====================================================================
    # Stage 4 — classification
    # =====================================================================

    def _build_classifiers(self, db: Session) -> Any:
        from backend.classifier.assigner import ClassificationStage
        from backend.classifier.category import CategoryClassifier, CategoryRule
        from backend.classifier.country import CountryClassifier
        from backend.classifier.gazetteer import load_from_db
        from backend.database.orm_models import CategoryDef

        gaz = load_from_db(db)
        defs = list(db.scalars(select(CategoryDef)))
        slug_by_id = {c.id: c.slug for c in defs}
        rules = [
            CategoryRule.from_row(c, slug_by_id.get(c.parent_id))
            for c in defs
            if c.is_enabled
        ]
        logger.info("Classifiers built: %d aliases, %d category rules", gaz.size, len(rules))
        return ClassificationStage(CountryClassifier(gaz), CategoryClassifier(rules))

    def _classify(self, db: Session, articles: Sequence[Any], report: RunReport) -> None:
        try:
            stage = self._build_classifiers(db)
            stats = stage.classify_batch(db, articles)
            report.classified = stats.processed
        except Exception as exc:
            # Classification is rule-based and local, so a failure here is a
            # bug in our own code rather than someone else's outage. It is
            # still not worth losing the articles over — they fall back to
            # International, which is what abstention means (§A10).
            logger.error("Classification failed", exc_info=True)
            report.errors.append(f"classify: {type(exc).__name__}: {exc}")

    # =====================================================================
    # Stage 5 — embeddings
    # =====================================================================

    def _vector_column_type(self, db: Session) -> str | None:
        """The udt name of `embeddings.vector`, or None if there is no such
        column (SQLite in tests, or a database where pgvector was absent and
        migration 004 skipped it)."""
        if self._vector_type is not None:
            return self._vector_type or None
        try:
            if db.bind.dialect.name != "postgresql":
                self._vector_type = ""
                return None
            udt = db.execute(
                text(
                    "SELECT udt_name FROM information_schema.columns "
                    "WHERE table_name = 'embeddings' AND column_name = 'vector'"
                )
            ).scalar()
        except Exception:
            udt = None
        self._vector_type = udt or ""
        return udt

    def _embed(self, db: Session, articles: Sequence[Any], report: RunReport) -> None:
        """Embed title + excerpt locally and store the vector.

        Text, not body: the body is often absent by licence, and a headline
        plus excerpt is what every article is guaranteed to have. Comparing
        like with like matters more for near-duplicate detection than having
        more words on one side.
        """
        from backend.database.orm_models import Embedding
        from backend.deduplicator.local_embedder import format_for_pgvector

        if not articles:
            return
        try:
            texts = [
                f"{a.title or ''} {a.summary_from_feed or ''}".strip() for a in articles
            ]
            vectors = self.embedder.embed_batch(texts)
        except Exception as exc:
            # Losing embeddings costs deduplication quality on this batch,
            # not visibility. Never fatal.
            logger.error("Embedding failed", exc_info=True)
            report.errors.append(f"embed: {type(exc).__name__}: {exc}")
            return

        rows = []
        vec_params = []
        for art, vec in zip(articles, vectors):
            if vec is None:
                continue
            rows.append(
                {
                    "raw_article_id": art.id,
                    "vector_json": None,
                    "model_name": getattr(self.embedder, "model_name", ""),
                }
            )
            vec_params.append({"aid": art.id, "vec": format_for_pgvector(vec)})

        if not rows:
            return

        for i in range(0, len(rows), BULK_CHUNK):
            db.execute(Embedding.__table__.insert(), rows[i : i + BULK_CHUNK])
        db.commit()

        # `vector` is a pgvector column added by raw DDL in migration 004 and
        # is deliberately absent from the ORM, so it is written here in SQL.
        # psycopg2 has no halfvec adapter — the value goes over as text and
        # is cast in the statement.
        coltype = self._vector_column_type(db)
        if coltype and vec_params:
            stmt = text(
                f"UPDATE embeddings SET vector = CAST(:vec AS {coltype}) "
                "WHERE raw_article_id = :aid"
            )
            try:
                for i in range(0, len(vec_params), BULK_CHUNK):
                    db.execute(stmt, vec_params[i : i + BULK_CHUNK])
                db.commit()
            except Exception as exc:
                logger.error("Writing pgvector column failed", exc_info=True)
                report.errors.append(f"vector: {type(exc).__name__}: {exc}")
                db.rollback()

        report.embedded = len(rows)
        logger.info("Embedded %d articles", len(rows))

    # =====================================================================
    # THE PUBLISH BARRIER
    # =====================================================================

    def _publish(self, db: Session, ids: Sequence[int]) -> int:
        """Flip `ingest_status` to PUBLISHED. This is the barrier.

        One statement per chunk, committed here and not again afterwards.
        After this returns, the articles are visible to readers and no
        later stage in this run is permitted to take that away.

        Articles already marked FAILED upstream are excluded rather than
        force-published — a body extraction that gave up on an article is a
        reason to leave it invisible, not to publish it empty.
        """
        from backend.database.orm_models import RawArticle

        published = 0
        for i in range(0, len(ids), BULK_CHUNK):
            chunk = ids[i : i + BULK_CHUNK]
            result = db.execute(
                update(RawArticle)
                .where(
                    RawArticle.id.in_(chunk),
                    RawArticle.ingest_status != INGEST_FAILED,
                )
                .values(
                    ingest_status=INGEST_PUBLISHED,
                    status=PipelineStatus.PROCESSED,
                    updated_at=self._clock(),
                )
                .execution_options(synchronize_session=False)
            )
            published += result.rowcount or 0
        db.commit()
        return published

    # =====================================================================
    # Below the barrier — enrichment
    # =====================================================================

    def _set_enrichment(self, db: Session, ids: Sequence[int], value: str) -> None:
        from backend.database.orm_models import RawArticle

        try:
            for i in range(0, len(ids), BULK_CHUNK):
                db.execute(
                    update(RawArticle)
                    .where(RawArticle.id.in_(ids[i : i + BULK_CHUNK]))
                    .values(enrichment_status=value)
                    .execution_options(synchronize_session=False)
                )
            db.commit()
        except Exception:
            logger.error("Could not set enrichment_status=%s", value, exc_info=True)
            db.rollback()

    def _enrich(self, db: Session, ids: Sequence[int], report: RunReport) -> None:
        """Summarise what we are allowed to summarise.

        Three properties, and all three are the point of this method:

        1. **Capped.** `enrichment_cap` articles per run. An hourly job that
           tries to summarise a 5,000-article backlog does not finish before
           the next tick fires.
        2. **Permission-checked.** `can_generate_summary` is per source.
        3. **Incapable of failing the run.** Every path returns normally.
           This is the guarantee the barrier exists to provide, and the
           broad `except` below is load-bearing rather than lazy.
        """
        from backend.database.orm_models import CleanedArticle, RawArticle

        try:
            candidates = list(
                db.execute(
                    select(RawArticle.id, RawArticle.source_id, CleanedArticle.id)
                    .join(CleanedArticle, CleanedArticle.raw_article_id == RawArticle.id)
                    .where(RawArticle.id.in_(list(ids)[:BULK_CHUNK]))
                    .limit(self.config.enrichment_cap)
                ).all()
            )
            if not candidates:
                # Nothing summarisable is a normal outcome, not a failure:
                # most sources do not licence the full text needed to
                # summarise anything.
                self._set_enrichment(db, ids, ENRICH_SKIPPED)
                return

            perms = self._permissions_for(db, {c[1] for c in candidates})
            eligible = [c for c in candidates if may_summarize(perms[c[1]])]
            skipped = [c[0] for c in candidates if not may_summarize(perms[c[1]])]
            if skipped:
                self._set_enrichment(db, skipped, ENRICH_SKIPPED)
            if not eligible:
                self._set_enrichment(db, [i for i in ids if i not in {c[0] for c in candidates}], ENRICH_SKIPPED)
                return

            from backend.database.models import CleanedArticleRecord

            records = [
                CleanedArticleRecord(id=c[2], raw_article_id=c[0]) for c in eligible
            ]
            result = self._await(self.summarizer.summarize_batch(db, records))
            done = getattr(result, "summarized", 0)
            report.enriched = done
            report.llm_calls_used += done + getattr(result, "failed", 0)
            self._set_enrichment(db, [c[0] for c in eligible], ENRICH_DONE)
            logger.info("Enrichment: %s", result)

        except Exception as exc:
            # An LLM outage previously emptied the site. It now costs a
            # summary. Anything already published stays published — this
            # handler must never touch ingest_status.
            logger.error("Enrichment failed — articles remain published", exc_info=True)
            report.errors.append(f"enrich: {type(exc).__name__}: {exc}")
            try:
                self._set_enrichment(db, ids, ENRICH_FAILED)
            except Exception:
                pass

        # Anything not otherwise resolved stays PENDING and is picked up by
        # a later run. PENDING is a queue, not an error.
