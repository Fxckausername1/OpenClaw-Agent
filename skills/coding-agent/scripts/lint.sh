#!/usr/bin/env bash
# Run common linters if they exist. Non-fatal: prints which checks ran.
set -euo pipefail
echo "coding-agent: running lint checks..."
if [ -f package.json ] && grep -q 'eslint' package.json 2>/dev/null; then
  if command -v npm >/dev/null 2>&1; then
    echo "-> running npm run lint"
    npm run lint --silent || echo "lint failed"
  fi
fi
if command -v shellcheck >/dev/null 2>&1; then
  echo "-> running shellcheck on scripts/*.sh"
  shellcheck scripts/*.sh || true
fi
echo "coding-agent: lint checks complete"

