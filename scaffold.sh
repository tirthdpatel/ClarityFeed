#!/bin/bash
# scaffold.sh — Creates the ClarityFeed project directory structure.
# Run: bash scaffold.sh
# This script is idempotent — safe to run multiple times.

set -e

echo "🔧 Creating ClarityFeed project structure..."

# Backend directories
mkdir -p backend/collector
mkdir -p backend/fetcher
mkdir -p backend/cleaner
mkdir -p backend/deduplicator
mkdir -p backend/summarizer
mkdir -p backend/categorizer
mkdir -p backend/api
mkdir -p backend/database
mkdir -p backend/scheduler

# Frontend directory
mkdir -p frontend/src

# GitHub Actions
mkdir -p .github/workflows

# Config, docs, scripts
mkdir -p config
mkdir -p docs
mkdir -p scripts

# Tests
mkdir -p tests/unit
mkdir -p tests/integration

# Create __init__.py files for all Python packages
PACKAGES=(
    backend
    backend/collector
    backend/fetcher
    backend/cleaner
    backend/deduplicator
    backend/summarizer
    backend/categorizer
    backend/api
    backend/database
    backend/scheduler
    config
    tests
    tests/unit
    tests/integration
)

for pkg in "${PACKAGES[@]}"; do
    touch "$pkg/__init__.py"
done

echo "✅ Project structure created successfully!"
echo ""
echo "Directory tree:"
find . -type f -name "*.py" -o -name "*.yml" -o -name "*.yaml" -o -name "*.sh" -o -name "*.txt" -o -name "*.md" | grep -v ".git/" | grep -v "node_modules" | sort
