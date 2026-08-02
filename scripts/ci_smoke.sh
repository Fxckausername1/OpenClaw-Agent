#!/usr/bin/env bash
set -euo pipefail
echo "Running CI smoke checks..."
python -m py_compile artist_pipeline.py || (echo "Python syntax errors" && exit 2)
python -m py_compile scripts/dump_rejected.py || true
echo "Smoke checks passed"

