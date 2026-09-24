#!/usr/bin/env python3
"""Apply the retention policy. Run after each ingestion cycle.

    python scripts/retention.py [--dry-run]

backend/retention.py has existed since Phase 1 and was never called from
anywhere, so the article table only ever grew. It is invoked from
.github/workflows/ingest.yml, after ingestion rather than before, so a
retention failure can never cost a cycle of news.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Run as `python scripts/retention.py`, sys.path[0] is scripts/, not the repo
# root, so `backend` is not importable. Every other script in here that imports
# backend does this; this one did not, and the workflow step died on the import
# for weeks with the ingestion above it perfectly healthy.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report, change nothing")
    args = parser.parse_args()

    # Inside the guard, not above it. These imports pull in the database layer
    # and every model behind it; leaving them outside meant an import error was
    # the one failure mode the "never fail the workflow" promise below did not
    # actually cover.
    db = None
    try:
        from backend.database.session import SessionLocal
        from backend.retention import ARTICLE_DAYS, get_storage_status, run_retention

        db = SessionLocal()

        before = get_storage_status(db)
        print(f"storage before: {before.human}", file=sys.stderr)

        stats = run_retention(db, dry_run=args.dry_run)

        after = get_storage_status(db)
        print(f"storage after:  {after.human}", file=sys.stderr)
        print(json.dumps({"window_days": ARTICLE_DAYS, **stats.as_dict()}, indent=2))
        return 0
    except Exception:
        # Retention is housekeeping. It must never fail the workflow and take
        # an otherwise good ingestion run down with it.
        logging.exception("retention failed")
        return 0
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
