#!/usr/bin/env python3
"""Run one enrichment worker. This is the container entrypoint in Kubernetes.

    python scripts/enrich_worker.py                 # poll until SIGTERM
    python scripts/enrich_worker.py --once          # drain one batch and exit
    python scripts/enrich_worker.py --batch-size 10

Scale it with replicas, not flags:

    kubectl -n clarity scale deployment/clarity-worker --replicas=8

Each worker claims its own rows with SELECT ... FOR UPDATE SKIP LOCKED, so
replicas never contend and never double-summarise. See
backend/enrichment/worker.py for why this is Postgres rather than Redis.

EXIT CODES. 0 on a clean shutdown or a completed --once run. 1 only if the
worker could not start at all — a bad DATABASE_URL, say. A failing LLM is not
a startup failure: it is the exact condition the publish barrier exists to
absorb, and a worker that exits on it would crash-loop instead of retrying.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClarityFeed enrichment worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process a single batch and exit. Used by tests and by the CronJob "
        "fallback when no long-running worker is deployed.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Articles claimed per transaction (default: ENRICH_BATCH_SIZE).",
    )
    parser.add_argument(
        "--no-probe-server",
        action="store_true",
        help="Do not serve the health endpoint. Useful when running locally.",
    )
    return parser


def _make_probe_handler(worker):
    class ProbeHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path in ("/health", "/healthz"):
                # Liveness only: is this process still looping? It deliberately
                # does not check Postgres or the LLM. A worker restarted because
                # someone else's API is down is a worse outcome than a worker
                # sitting idle and retrying, and restarting cannot fix either.
                body = {"status": "ok", "stopping": worker.stopping}
            elif self.path == "/metrics":
                body = worker.stats.as_dict()
            else:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass  # probes would otherwise dominate the log

    return ProbeHandler


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from backend.database.session import get_session
    from backend.enrichment import EnrichmentWorker
    from config.settings import settings

    logging.basicConfig(
        level=settings.LOG_LEVEL,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s [worker] %(name)s: %(message)s",
    )
    log = logging.getLogger("news.enrichment.entrypoint")

    worker = EnrichmentWorker(batch_size=args.batch_size)

    if args.once:
        with get_session() as db:
            claimed = worker.process_batch(db)
        log.info("drained one batch: %d claimed, stats=%s", claimed, worker.stats.as_dict())
        return 0

    worker.install_signal_handlers()

    if not args.no_probe_server:
        handler = _make_probe_handler(worker)
        server = HTTPServer(("0.0.0.0", settings.WORKER_PROBE_PORT), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("probe server listening on :%d", settings.WORKER_PROBE_PORT)

    worker.run_forever(get_session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
