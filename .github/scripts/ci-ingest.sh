#!/usr/bin/env bash
# Invoke one ingestion cycle from CI.
#
# Reads workflow_dispatch inputs from the environment so that ingest.yml
# stays declarative and this stays runnable by hand:
#
#     DRY_RUN=true .github/scripts/ci-ingest.sh
#
# Exit code is the CLI's: 0 if the publish barrier was reached, 1 if not.
# A dead feed does not fail the run — that is what the circuit breaker is
# for, and paging on one bad source trains everyone to ignore the alert.
set -uo pipefail

TRIGGER="${TRIGGER:-cron}"
ARGS=(--trigger "$TRIGGER" --json)

[ "${DRY_RUN:-false}" = "true" ] && ARGS+=(--dry-run)
[ "${SKIP_ENRICHMENT:-false}" = "true" ] && ARGS+=(--skip-enrichment)
[ -n "${LIMIT:-}" ] && ARGS+=(--limit "$LIMIT")
[ -n "${SOURCES:-}" ] && ARGS+=(--sources "$SOURCES")

echo "running: scripts/ingest.py ${ARGS[*]}"
python scripts/ingest.py "${ARGS[@]}" > run-report.json
STATUS=$?

cat run-report.json || true
exit $STATUS
