#!/usr/bin/env bash
# Dependency install for the ingestion workflow.
#
# Lives here rather than inline in ingest.yml so it can be read, linted and
# run by hand. CI steps that exist only as YAML block scalars are the ones
# nobody can reproduce locally when they break.
set -euo pipefail

python -m pip install --upgrade pip

# CPU wheels only. The default torch wheel bundles CUDA and is several GB,
# on a runner that has no GPU. This single flag is most of the difference
# between a two-minute install and a ten-minute one.
python -m pip install -r requirements-ingest.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu
