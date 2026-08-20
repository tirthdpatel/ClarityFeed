#!/usr/bin/env bash
# Render the run report into the workflow summary panel, so that "did the
# site update?" is answerable from the Actions tab without opening logs.
set -uo pipefail

{
  echo "### Ingestion run"
  echo ""
  echo '```json'
  cat run-report.json 2>/dev/null || echo '{"status": "no report produced"}'
  echo '```'
} >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
