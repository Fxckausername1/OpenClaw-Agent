#!/usr/bin/env python3
"""cross_book_registry.py — same-ticker cross-book overlap check (2026-07-05, heff).

Three books share one Alpaca paper account with zero mutual visibility:
  - mean-reversion equity book  (data/paper_open.json,     keyed by trade_id, each rec has "ticker")
  - ORB equity book             (data/orb_paper_open.json, keyed by trade_id, each rec has "ticker")
  - options tournament book     (data/options_eval.db trades_ledger, legs_metadata JSON has "legs"
                                 -> each leg has an "occ" OCC symbol)

This is a DEFENSE-IN-DEPTH correlation cap, not a primary risk control: if a ticker is already
open in ANY one book, the others should not open a NEW position in it. Both functions FAIL OPEN
(return an empty set + log a warning) on any read error -- a broken read must never itself block
trading. Cheap by design: small JSON file reads / one SQLite query, safe to call every ~2min tick.
"""
import json
import sqlite3
from pathlib import Path

from log_setup import get_logger
log = get_logger("executor")  # shared sink; this module is called from alpaca_executor.py /
                               # options_orchestrator.py, both already log to this stream

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PAPER_OPEN_PATH = DATA / "paper_open.json"
ORB_PAPER_OPEN_PATH = DATA / "orb_paper_open.json"
OPTIONS_DB_PATH = DATA / "options_eval.db"


def _tickers_from_open_file(path):
    """Load a {trade_id: {..., "ticker": ...}} JSON file -> set of tickers. Fail-open (empty
    set) if missing/malformed -- caller logs why."""
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text())
    except Exception as e:
        log.warning(f"cross_book_registry: could not parse {path.name}: {e}; fail-open (empty set)")
        return set()
    if not isinstance(raw, dict):
        log.warning(f"cross_book_registry: {path.name} is not a dict (got {type(raw).__name__}); "
                    f"fail-open (empty set)")
        return set()
    out = set()
    for tid, rec in raw.items():
        try:
            t = rec.get("ticker")
        except AttributeError:
            continue
        if t:
            out.add(str(t).upper())
    return out


def equity_open_tickers():
    """Union of tickers with an open paper position in the mean-reversion book OR the ORB book.
    Fail-open per-file: a broken/missing file just contributes nothing, it never raises."""
    return _tickers_from_open_file(PAPER_OPEN_PATH) | _tickers_from_open_file(ORB_PAPER_OPEN_PATH)


def options_open_tickers():
    """Underlying tickers with an OPEN/PARTIAL_CLOSE/PENDING spread in the options tournament
    ledger, derived from each leg's OCC symbol via options_eval._occ_root (reused, not
    reimplemented). Fail-open (empty set) on any DB/parse error."""
    try:
        from options_eval import _occ_root
    except Exception as e:
        log.warning(f"cross_book_registry: could not import _occ_root from options_eval: {e}; "
                    f"fail-open (empty set)")
        return set()

    try:
        conn = sqlite3.connect(str(OPTIONS_DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT legs_metadata FROM trades_ledger WHERE status IN "
            "('OPEN','PARTIAL_CLOSE','PENDING')").fetchall()
        conn.close()
    except Exception as e:
        log.warning(f"cross_book_registry: options_eval.db query failed: {e}; fail-open (empty set)")
        return set()

    out = set()
    for row in rows:
        try:
            meta = json.loads(row["legs_metadata"])
        except Exception as e:
            log.warning(f"cross_book_registry: bad legs_metadata JSON, skipping row: {e}")
            continue
        for leg in (meta.get("legs") or []):
            occ = leg.get("occ")
            root = _occ_root(occ)
            if root:
                out.add(root.upper())
    return out
