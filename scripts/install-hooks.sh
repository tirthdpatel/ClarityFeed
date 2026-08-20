#!/usr/bin/env bash
#
# Point git at the version-controlled hooks in .githooks/.
# Run once per clone:
#
#     ./scripts/install-hooks.sh
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

chmod +x .githooks/* 2>/dev/null || true
chmod +x scripts/*.sh 2>/dev/null || true

git config core.hooksPath .githooks

echo "Git hooks installed (core.hooksPath -> .githooks)"
echo ""
echo "Active hooks:"
for hook in .githooks/*; do
    [ -f "$hook" ] && echo "  - $(basename "$hook")"
done
echo ""
echo "Verify with:  git config core.hooksPath"
echo ""
echo "Note: hooks are a guardrail, not a guarantee — 'git commit --no-verify'"
echo "skips them. CI runs the same scan so a bypass cannot reach main."
