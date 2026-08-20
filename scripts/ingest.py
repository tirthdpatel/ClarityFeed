#!/usr/bin/env python3
"""Run one ingestion cycle. This is the entry point GitHub Actions calls.

    python scripts/ingest.py                       # a normal run
    python scripts/ingest.py --dry-run             # read feeds, write nothing
    python scripts/ingest.py --limit 20            # cap new articles
    python scripts/ingest.py --sources bbc,dw      # substring match on name/feed
    python scripts/ingest.py --skip-enrichment     # stop at the publish barrier

EXIT CODES. 0 means the barrier was reached; 1 means it was not. A dead
feed is *not* a failure — the circuit breaker exists to absorb it, and a
non-zero exit on one bad source would turn a nightly cron into a nightly
page. Only an aborted or failed run exits 1.

The heavy imports (sentence-transformers, and torch behind it) happen
inside the runner on first use, so `--help` and `--dry-run` stay fast.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ingest.py",
        description="Run one ClarityFeed ingestion cycle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read feeds and report what would happen. Writes nothing, and "
             "deliberately records no source health: a rehearsal must not "
             "move a real source toward the circuit breaker.",
    )
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="Ingest at most N new articles this run.")
    p.add_argument("--sources", default="", metavar="A,B",
                   help="Comma-separated substrings matched against source "
                        "name or feed URL, case-insensitively.")
    p.add_argument("--skip-enrichment", action="store_true",
                   help="Stop at the publish barrier. Articles are published; "
                        "summaries are left for a later run.")
    p.add_argument("--no-bodies", action="store_true",
                   help="Skip body extraction even for sources that licence "
                        "full text. Useful when a run needs to be quick.")
    p.add_argument("--enrichment-cap", type=int, default=None, metavar="N",
                   help="Maximum articles to enrich in one run.")
    p.add_argument("--trigger", default="cron",
                   choices=("cron", "manual", "backfill"),
                   help="Recorded on the ingestion_runs row.")
    p.add_argument("--json", action="store_true",
                   help="Emit the run report as JSON on stdout.")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party chatter drowns the run log otherwise.
    for noisy in ("httpx", "httpcore", "urllib3", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from backend.database.session import get_session
    from backend.pipeline import PipelineConfig, PipelineRunner

    config = PipelineConfig(
        dry_run=args.dry_run,
        limit=args.limit,
        sources=tuple(s for s in args.sources.split(",") if s.strip()),
        skip_enrichment=args.skip_enrichment,
        trigger=args.trigger,
        fetch_bodies=not args.no_bodies,
    )
    if args.enrichment_cap is not None:
        config.enrichment_cap = args.enrichment_cap

    runner = PipelineRunner(config)

    with get_session() as db:
        report = runner.run(db)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        _print_report(report)

    return 0 if report.ok else 1


def _print_report(report) -> None:
    d = report.as_dict()
    print()
    print(f"  run {d['run_id']}  {d['status'].upper()}" + ("  (dry run)" if d["dry_run"] else ""))
    print(f"  sources        {d['sources']}")
    if d["sources_disabled"]:
        print(f"  DISABLED       {', '.join(d['sources_disabled'])}")
    print(f"  entries seen   {d['entries_seen']}")
    print(f"  new            {d['articles_new']}   duplicate {d['articles_duplicate']}")
    if d["articles_gated"]:
        print(f"  gated          {d['articles_gated']} articles had fields dropped or truncated")
    print(f"  PUBLISHED      {d['articles_published']}")
    print(f"  classified {d['classified']}   embedded {d['embedded']}   enriched {d['enriched']}")
    print(f"  storage        {d['storage_pct']}%")
    slow = sorted(d["timings"].items(), key=lambda kv: -kv[1])[:3]
    print(f"  slowest        {', '.join(f'{k} {v}s' for k, v in slow if v)}")
    if d["errors"]:
        print("  errors:")
        for e in d["errors"]:
            print(f"    - {e}")
    print()


if __name__ == "__main__":
    sys.exit(main())
