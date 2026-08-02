#!/usr/bin/env bash
# Run tests if a test runner is configured. Non-fatal.
set -euo pipefail
echo "coding-agent: running tests (if present)"
if [ -f package.json ] && grep -q 'jest' package.json 2>/dev/null; then
  if command -v npm >/dev/null 2>&1; then
    echo "-> running npm test"
    npm test --silent || echo "tests failed or returned non-zero"
  fi
else
  echo "No recognized JS test runner found; skipping."
fi
echo "coding-agent: tests step complete"

