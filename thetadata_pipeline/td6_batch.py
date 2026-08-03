"""TD-6 rolling-batch symbol rotation (2026-07-30) -- expands ThetaData
collection beyond SPY/QQQ to the 19-name liquid/affordable candidate list
(TD6_SCOPING_NOTES.md addendum), using the SAME rolling-batch pattern
already proven for GEX refresh (live_gex_rolling.py: ~20 tickers/batch,
full-universe cycle ~24-30min) rather than a naive full-list-every-cycle
swap that the earlier capacity scoping found would overrun the 5-min
cadence (real timing baseline: SPY+QQQ alone comfortably fit 5min with
margin; 19 symbols every cycle would not).

SPY/QQQ stay on their own always-on path (proven, unchanged) -- this module
only owns the 17 NEW names, rotated in small batches so each cycle adds a
modest, bounded amount of work on top of the existing SPY/QQQ load.
Conservative batch size (3) chosen deliberately: no real per-symbol timing
data exists yet for names beyond SPY/QQQ, so start small and let real
observed cycle durations (logged each run) inform whether the batch size
can safely grow later -- same "measure, don't assume" discipline as the
rest of TD-STD.

Cursor persisted at data/thetadata/cursors/td6_batch_rotation.json, same
convention collector.py already uses for its own per-symbol cursors.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURSOR_PATH = ROOT / "data" / "thetadata" / "cursors" / "td6_batch_rotation.json"

# 17 new names -- the 19-name TD6_SCOPING_NOTES.md list minus SPY/QQQ,
# which already have their own always-on collection path.
NEW_SYMBOLS = [
    "NVDA", "TSLA", "PLTR", "SOFI", "RIVN", "NOW", "INTC", "NFLX",
    "HOOD", "MSTR", "SLV", "SMCI", "RKLB", "CRWD", "IWM", "XLE", "TLT",
]

BATCH_SIZE = 3  # conservative starting point -- see module docstring


def _batches(symbols: list, size: int) -> list:
    return [symbols[i:i + size] for i in range(0, len(symbols), size)]


BATCHES = _batches(NEW_SYMBOLS, BATCH_SIZE)


def _load_cursor() -> int:
    if not CURSOR_PATH.exists():
        return 0
    try:
        return json.loads(CURSOR_PATH.read_text()).get("next_index", 0)
    except Exception:
        return 0


def _save_cursor(idx: int) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"next_index": idx, "n_batches": len(BATCHES)}))
    tmp.replace(CURSOR_PATH)


def get_current_batch_symbols() -> tuple:
    """Returns this cycle's batch of new symbols and advances the cursor
    for next time. Never raises -- an unreadable/corrupt cursor just
    restarts rotation from batch 0, same fail-safe convention as every
    other cursor read in this codebase."""
    idx = _load_cursor() % len(BATCHES)
    batch = BATCHES[idx]
    _save_cursor((idx + 1) % len(BATCHES))
    return tuple(batch)
