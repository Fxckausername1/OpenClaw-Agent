"""Shared fill-aware lifecycle and serialization primitives for the MR/ORB
paper evaluators.

Derived from the local draft, with four corrections (see REVIEW notes below).

TWO DEFECTS THIS FIXES
----------------------

1. PHANTOM FILLS. The live executors submit DAY limit orders AT the signal
   boundary. paper_eval.evaluate() and orb_paper_eval.evaluate() ingested
   every trigger straight into open state and tested stop/target/EOD without
   ever comparing a bar to `entry`. They therefore scored outcomes for orders
   Alpaca never filled. `advance_boundary_limit` requires a later bar to
   touch the limit first, and reports never_filled / pending_entry /
   filled_open / filled_closed instead of assuming a position exists.

2. CONCURRENT MUTATION. load_open -> ingest -> evaluate -> save_open is a
   read-modify-write with no mutual exclusion, while three cron paths reach
   it (mean_reversion_wrapper, mr_watch_wrapper, night_report --eod) under
   three DIFFERENT lock names. At 16:05 ET the --eod run closed everything
   and wrote {}, then a concurrent non-eod run saved its stale pre-eod dict
   back, resurrecting closed trades to be closed a second time the next
   session. That produced 34 duplicate rows worth +34.779R.

   `evaluator_lock` is taken INSIDE each evaluator's main(), not in the
   wrappers, so every invocation path is serialized regardless of which
   wrapper (or a human, or a future job) called it. Relying on wrappers is
   what failed: the locks existed, they just guarded the wrong scope.

CORRECTIONS vs the draft
------------------------
* rewrite_csv_unique no longer SILENTLY DROPS rows with a missing trade_id.
  In a financial ledger a vanished row is worse than a crash; it now raises.
* Timezone expectations are asserted, not assumed. The evaluators normalise
  their bar index to naive ET (paper_eval.py:136, orb_paper_eval.py:116) and
  entry_time is naive ET, so the naive 15:45 cutoff is correct on THIS path
  -- but nothing enforced that, and close_time is written tz-AWARE, so the
  mismatch is one refactor away. assert_naive_et makes the contract explicit.
* append_rows_dedup appends rather than rewriting the whole ledger on every
  tick, so a bug in the merge path cannot rewrite history.
* Fill state is explicit, so a never-filled signal is recorded as such rather
  than silently vanishing or being scored.
"""
from __future__ import annotations

import csv
import datetime as dt
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

# The live executor stops accepting new entries at 15:45 ET; a limit not
# touched by then never became a position.
ENTRY_CUTOFF_ET = dt.time(15, 45)

STATE_NEVER_FILLED = "never_filled"
STATE_PENDING_ENTRY = "pending_entry"
STATE_FILLED_OPEN = "filled_open"
STATE_FILLED_CLOSED = "filled_closed"


class LedgerIntegrityError(ValueError):
    """Raised rather than silently dropping or duplicating a ledger row."""


# --------------------------------------------------------------- locking
@contextmanager
def evaluator_lock(path):
    """Serialize one book across cron, EOD and manual invocations.

    BLOCKING (no LOCK_NB) on purpose: at the 16:05 collision we want the
    second process to wait its turn and then see fresh state, not to skip
    silently. Skipping is what a `flock -n` wrapper does, and that is why
    the wrapper locks did not prevent this bug."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write_json(path, value) -> None:
    """tmp + fsync + rename. A torn read of paper_open.json previously fell
    into `except: return {}`, silently dropping every open position."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


# ----------------------------------------------------------------- ledger
def existing_trade_ids(path) -> set:
    path = Path(path)
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {str(r.get("trade_id") or "") for r in csv.DictReader(handle)}


def append_rows_dedup(path, fields, rows) -> int:
    """Append rows whose trade_id is not already present. Returns the count
    written.

    Appends rather than rewriting the whole file: this ledger has already
    been corrupted once, and a full rewrite on every tick means any merge
    bug can rewrite history rather than just the tail.

    Raises on a row with no trade_id instead of discarding it."""
    path = Path(path)
    for row in rows:
        if not str(row.get("trade_id") or "").strip():
            raise LedgerIntegrityError(
                f"refusing to write a ledger row with no trade_id: {row!r}")
    seen = existing_trade_ids(path)
    fresh = []
    for row in rows:
        tid = str(row["trade_id"])
        if tid in seen:
            continue          # already recorded -- the duplicate-race guard
        seen.add(tid)
        fresh.append(row)
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        if new_file:
            writer.writeheader()
        for row in fresh:
            writer.writerow({k: row.get(k, "") for k in fields})
        handle.flush()
        os.fsync(handle.fileno())
    return len(fresh)


def append_terminal(path, rec, state, terminal_time) -> None:
    """Persist a non-trade terminal state (never_filled) so a stale trigger
    file cannot re-ingest it tomorrow and score it as a trade."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"trade_id": rec.get("trade_id"), "strategy": rec.get("strategy"),
           "ticker": rec.get("ticker"), "side": rec.get("side"),
           "entry": rec.get("entry"), "signal_time": rec.get("entry_time"),
           "fill_time": rec.get("_paper_fill_time"), "state": state,
           "terminal_time": terminal_time}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def terminal_ids(path) -> set:
    path = Path(path)
    if not path.exists():
        return set()
    out = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("trade_id"):
                out.add(str(row["trade_id"]))
    return out


# ------------------------------------------------------------ fill model
def assert_naive_et(index_or_ts, what: str) -> None:
    """The evaluators normalise bars to naive ET and write entry_time naive
    ET, so the 15:45 cutoff below is a correct wall-clock comparison. That
    was never enforced anywhere; if a future change hands us a UTC-aware
    index the cutoff would silently fire four hours early and quietly shrink
    the trade population. Fail loudly instead."""
    tz = getattr(index_or_ts, "tz", None) or getattr(index_or_ts, "tzinfo", None)
    if tz is not None:
        raise LedgerIntegrityError(
            f"{what} must be naive ET for the {ENTRY_CUTOFF_ET} cutoff to be "
            f"correct, got tz={tz!r}")


def advance_boundary_limit(rec, bars, *, eod, target, cutoff=ENTRY_CUTOFF_ET):
    """Advance a resting boundary-limit order over completed bars.

    The signal is only known after its own bar closes, so only LATER bars can
    fill -- `bars.index > entry_time`. The limit must be touched before the
    live executor's 15:45 ET cutoff. On a bar where both the fill and an exit
    are possible, the STOP wins, matching simulate_boundary_limit()'s
    conservative same-bar rule.

    Idempotent: once a fill is recorded in `_paper_fill_time` it is replayed
    rather than re-derived, so repeated runs converge on the same answer."""
    if bars is None or len(bars) == 0:
        return {"state": STATE_PENDING_ENTRY}

    assert_naive_et(bars.index, "bars index")
    entry_time = pd.Timestamp(rec["entry_time"])
    assert_naive_et(entry_time, "entry_time")

    future = bars[bars.index > entry_time]
    if future.empty:
        # No bar ever followed the signal. Intraday that is still pending;
        # at EOD it is terminal -- returning pending_entry there would leave
        # the record in open state forever.
        return {"state": STATE_NEVER_FILLED if eod else STATE_PENDING_ENTRY}

    side = str(rec["side"]).upper()
    if side not in {"LONG", "SHORT"}:
        raise LedgerIntegrityError(f"unsupported side: {side}")
    entry, stop = float(rec["entry"]), float(rec["stop"])
    risk = abs(entry - stop)
    if risk <= 0:
        raise LedgerIntegrityError("entry and stop must differ")

    known = rec.get("_paper_fill_time")
    known_ts = pd.Timestamp(known) if known else None
    filled = False
    last_close = None

    for index, bar in future.iterrows():
        bar_time = pd.Timestamp(index)
        high, low = float(bar["High"]), float(bar["Low"])

        if not filled:
            if known_ts is not None:
                if bar_time < known_ts:
                    continue
                filled = True
            else:
                if bar_time.time() >= cutoff:
                    continue
                touched = low <= entry if side == "LONG" else high >= entry
                if not touched:
                    continue
                filled = True
                known_ts = bar_time
                rec["_paper_fill_time"] = bar_time.isoformat()

        last_close = float(bar["Close"])
        stop_hit = low <= stop if side == "LONG" else high >= stop
        target_hit = False
        if target is not None:
            target_hit = high >= target if side == "LONG" else low <= target

        if stop_hit:
            return {"state": STATE_FILLED_CLOSED,
                    "fill_time": rec.get("_paper_fill_time"),
                    "exit_price": stop, "exit_reason": "stop", "outcome_r": -1.0}
        if target_hit:
            direction = 1.0 if side == "LONG" else -1.0
            return {"state": STATE_FILLED_CLOSED,
                    "fill_time": rec.get("_paper_fill_time"),
                    "exit_price": target, "exit_reason": "t1",
                    "outcome_r": (target - entry) * direction / risk}

    if not filled:
        return {"state": STATE_NEVER_FILLED if eod else STATE_PENDING_ENTRY}
    if eod and last_close is not None:
        direction = 1.0 if side == "LONG" else -1.0
        return {"state": STATE_FILLED_CLOSED,
                "fill_time": rec.get("_paper_fill_time"),
                "exit_price": last_close, "exit_reason": "eod",
                "outcome_r": (last_close - entry) * direction / risk}
    return {"state": STATE_FILLED_OPEN, "fill_time": rec.get("_paper_fill_time")}
