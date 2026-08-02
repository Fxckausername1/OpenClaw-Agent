#!/usr/bin/env bash
set -u

# Wrapper to run daily_artists.py with venv and logging
export HOME="/home/heff"
export PATH="/home/heff/.openclaw/workspace/venv/bin:/home/heff/.openclaw/workspace/stockfish/bin:$PATH"
LOGDIR="/home/heff/.openclaw/workspace/logs"
mkdir -p "$LOGDIR"
TS=$(date -u +"%Y%m%d_%H%M%S")
OUT="$LOGDIR/daily_artists_${TS}.log"

echo "Run started: $(date -u)" > "$OUT"

# Activate venv if present
if [ -f "/home/heff/.openclaw/workspace/venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source /home/heff/.openclaw/workspace/venv/bin/activate
fi

CMD=("/home/heff/.openclaw/workspace/venv/bin/python" "/home/heff/.openclaw/workspace/scripts/daily_artists.py")

echo "Command: ${CMD[*]}" >> "$OUT"
"${CMD[@]}" >> "$OUT" 2>&1
EXIT=$?

echo "Exit status: $EXIT" >> "$OUT"
if [ "$EXIT" -ne 0 ]; then
  echo "Run finished with errors at $(date -u)" >> "$OUT"
  exit $EXIT
fi

echo "Run finished successfully at $(date -u)" >> "$OUT"
exit 0
