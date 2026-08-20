#!/usr/bin/env bash
#
# One-shot database bootstrap: create a virtualenv, install the packages the
# migrations need, verify the connection, migrate, verify again.
#
#     ./scripts/setup_db.sh
#
# Safe to re-run. Alembic only applies revisions that have not run yet, so a
# second invocation on an up-to-date database is a no-op.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
step() { echo; echo "${BOLD}==> $1${RESET}"; }

if [ ! -f .env ]; then
    echo "${RED}No .env file.${RESET}  Run: cp .env.example .env"
    exit 1
fi

# -- 1. virtualenv -----------------------------------------------------------
# A venv rather than a bare `pip3 install` because recent macOS/Homebrew
# Pythons are PEP-668 "externally managed" and refuse to install into the
# system interpreter. This sidesteps that entirely.
step "Python environment"
if [ ! -d .venv ]; then
    python3 -m venv .venv
    echo "  created .venv"
else
    echo "  .venv already exists"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -V | sed 's/^/  /'

# -- 2. dependencies ---------------------------------------------------------
# Deliberately NOT `pip install -r requirements.txt`. newspaper3k (2020) has
# build dependencies — jieba3k, tinysegmenter, feedfinder2 — that fail against
# modern setuptools. None of it is needed to run migrations, and letting it
# block the database setup would be silly. Install the full requirements
# separately when you next work on the fetcher.
step "Installing migration dependencies"
pip install --quiet --upgrade pip
pip install --quiet \
    "alembic==1.13.2" \
    "sqlalchemy==2.0.30" \
    "psycopg2-binary==2.9.9" \
    "pydantic==2.7.1" \
    "pydantic-settings==2.2.1" \
    "python-dotenv==1.0.1"
echo "  done"

# -- 3. pre-flight -----------------------------------------------------------
step "Checking the database connection"
if ! python scripts/check_db.py; then
    echo "${RED}Connection check failed — not migrating.${RESET}"
    exit 1
fi

# -- 4. migrate --------------------------------------------------------------
step "Applying migrations"
echo "  current revision:"
alembic current 2>&1 | sed 's/^/    /'
echo
alembic upgrade head

# -- 5. verify ---------------------------------------------------------------
step "Verifying result"
python scripts/check_db.py

echo
echo "${GREEN}${BOLD}Database is ready.${RESET}"
echo
echo "The venv stays activated only inside this script. For later commands:"
echo "  ${BOLD}source .venv/bin/activate${RESET}"
