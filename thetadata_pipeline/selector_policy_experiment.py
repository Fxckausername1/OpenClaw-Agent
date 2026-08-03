"""Paper-only selector policy experiment (heff-requested, 2026-08-01):
compares 5 candidate contract-selection policies against the frozen v2.2
replay dataset, with a locked chronological train/holdout split so no
candidate's holdout performance is ever evaluated more than once.

Variants (all pre-specified by heff, none fit/tuned from data here -- so
"lock candidate policies using training data" is satisfied trivially:
nothing in this module is optimized against any result, only reported):

  A  baseline            premium $0.20-$0.30, target_delta 0.35 (SelectorConfig() defaults)
  B  delta_first_debit_cap  target_delta 0.35, no premium ceiling, $100 total-debit cap instead
  C1 premium_0.20_0.50   premium $0.20-$0.50
  C2 premium_0.20_1.00   premium $0.20-$1.00
  C3 premium_0.20_2.50   premium $0.20-$2.50
  D  affordable_delta    same policy as A -- reports A's ACTUAL selected-contract
                         delta distribution (see report_variant_d.py's caller)

D needs no separate simulation: it is a different report on Variant A's own
results, computed downstream of run_all_variants().

Train/holdout split, locked chronologically BEFORE any variant was run or
any result viewed (see selector_policy_report.py):
  TRAIN:   2025-12-03 .. 2026-06-10  (130 sessions, 80%)
  HOLDOUT: 2026-06-11 .. 2026-07-28  (32 sessions, 20%)
Chronological, not random, so no signal ever informs a policy decision using
a later session's data. Point-in-time safety within a session is inherited
unchanged from bt2_selector.build_point_in_time_book's own
`trade_timestamp <= decision_ts` filter -- this module never bypasses it or
constructs its own book.

Never modifies bt2_selector.py, bt2_simulator.py, bt2_fills.py, bt2_exits.py,
or any other frozen baseline file. Two read-only monkeypatches are used, both
restored in a `finally` and both proven inert-to-behavior in
test_selector_policy_experiment.py:
  1. bt2_selector.evaluate_candidate -> a wrapper that calls the real
     function unchanged, then (Variant B only) appends a debit-cap reason if
     applicable. bt2_selector.select_contract calls evaluate_candidate by
     bare name resolved in bt2_selector's OWN module globals, so patching
     the attribute on the bt2_selector module object is the correct target.
  2. bt2_simulator.select_contract -> a wrapper that calls the real
     function unchanged and stashes its return (which already contains the
     winning candidate's delta -- bt2_simulator's ledger row does not keep
     it) into a side-channel. bt2_simulator imported select_contract with
     `from .bt2_selector import select_contract`, a SEPARATE name binding in
     bt2_simulator's own namespace -- patching bt2_selector.select_contract
     would NOT affect bt2_simulator's calls, so the patch target here is
     deliberately the bt2_simulator module attribute, not bt2_selector's.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import bt2_selector
from . import bt2_simulator as bt2_simulator_mod
from .bt2_selector import (
    SelectorConfig, evaluate_candidate as _real_evaluate_candidate,
    select_contract as _real_select_contract,
)
from .bt2_simulator import TradeInputs, simulate_trade
from .backfill import MIN_FREE_RAM_MB, _free_ram_mb
from .bt3_b0_random_control import BACKFILL_RAW_DIR, _load_raw_session_trades
from .bt3_b1_indicator_only import (
    ADMISSION_POLICY, EXIT_CONFIG, FILL_CONFIG, generate_b1_signals, load_triangle_events,
    session_bootstrap_mean_ci, _release_due,
)
from .bt3_b1_v22 import V22_EVENTS_PATH
from .bt_dedup import ADMIT, OpenBook, admit
from .selector_rejection_audit import time_of_day_bucket, wilson_ci

EXPERIMENT_ID = "selector_policy_experiment_v1"

DEBIT_CAP_DOLLARS = 100.0
DEFAULT_QUANTITY = 1
DEFAULT_FEE_PER_CONTRACT = FILL_CONFIG.fee_per_contract  # same $0.05/contract regulatory-fee approximation as every other B-series experiment

# --- Locked train/holdout split (chronological; see module docstring) ------
TRAIN_START = "2025-12-03"
TRAIN_END = "2026-06-10"
HOLDOUT_START = "2026-06-11"
HOLDOUT_END = "2026-07-28"


def segment_of(session: str) -> str:
    if TRAIN_START <= session <= TRAIN_END:
        return "train"
    if HOLDOUT_START <= session <= HOLDOUT_END:
        return "holdout"
    return "unassigned"


# --- Variant configs ---------------------------------------------------

_BASELINE = SelectorConfig()  # A: premium 0.20-0.30, target_delta 0.35, everything else default

VARIANT_CONFIGS = {
    "A_baseline": _BASELINE,
    "B_delta_first_debit_cap": dataclasses.replace(_BASELINE, premium_low=0.0, premium_high=float("inf")),
    "C1_premium_0.20_0.50": dataclasses.replace(_BASELINE, premium_high=0.50),
    "C2_premium_0.20_1.00": dataclasses.replace(_BASELINE, premium_high=1.00),
    "C3_premium_0.20_2.50": dataclasses.replace(_BASELINE, premium_high=2.50),
}
DEBIT_CAP_VARIANTS = {"B_delta_first_debit_cap"}


def total_debit_dollars(ask: float, quantity: int = DEFAULT_QUANTITY,
                         fee_per_contract: float = DEFAULT_FEE_PER_CONTRACT) -> float:
    """The ONLY definition of 'total contract debit' used anywhere in this
    module: actual dollars at risk if filled at this ask, INCLUDING fees --
    never the quoted premium alone. ask*100 converts option-premium-per-
    share to a 100-share contract's dollar cost."""
    return round(float(ask) * 100.0 * quantity + fee_per_contract * quantity, 4)


def _make_debit_cap_evaluate_candidate(quantity: int, fee_per_contract: float):
    def wrapped(row, decision_date, config):
        result = _real_evaluate_candidate(row, decision_date, config)
        if "ask" not in result:
            # evaluate_candidate's own quote-validity short-circuit already
            # fired (missing/zero/crossed quote) -- nothing to cap, and no
            # validated ask exists to compute a debit from.
            return result
        debit = total_debit_dollars(result["ask"], quantity, fee_per_contract)
        reasons = list(result["reasons"])
        if debit > DEBIT_CAP_DOLLARS:
            reasons.append(
                f"total debit ${debit:.2f} (ask ${result['ask']:.2f} x 100 x {quantity} "
                f"+ ${fee_per_contract * quantity:.2f} fees) exceeds ${DEBIT_CAP_DOLLARS:.2f} risk cap"
            )
        out = dict(result)
        out["reasons"] = reasons
        out["passed"] = not reasons
        out["total_debit"] = debit
        return out
    return wrapped


_last_selection: dict = {}


def _capturing_select_contract(book, right, decision_ts, config):
    """Observes bt2_selector.select_contract's real return value (unchanged
    -- this is a pure read, the return value is passed through untouched)
    so the winning candidate's delta/ask/dte can be attached to the ledger
    row afterward. bt2_simulator's own ledger schema does not carry delta."""
    result = _real_select_contract(book, right, decision_ts, config)
    _last_selection["result"] = result
    return result


def _simulate_signal_with_config(sig: dict, trades_df: pd.DataFrame,
                                  selector_config: SelectorConfig, admission_check) -> dict:
    context_snapshot_id = f"POLICY-EXP:{sig['symbol']}:{sig['session']}:{sig['bar_index']}:{sig['trigger']}"
    inputs = TradeInputs(
        session=sig["session"], symbol=sig["symbol"],
        signal_ts=sig["decision_ts"], decision_ts=sig["decision_ts"],
        direction=sig["direction"], context_snapshot_id=context_snapshot_id,
        experiment_id=EXPERIMENT_ID, quantity=DEFAULT_QUANTITY, invalidation_level=None,
        underlying_price=sig.get("underlying_price"),
    )
    _last_selection.clear()
    row = simulate_trade(
        inputs, trades_df, pd.DataFrame(), greeks=None,
        selector_config=selector_config, fill_config=FILL_CONFIG, exit_config=EXIT_CONFIG,
        admission_check=admission_check,
    )
    sel = _last_selection.get("result")
    if sel is not None and sel.found:
        row["_selected_delta"] = sel.contract.get("delta")
        row["_selected_ask_at_selection"] = sel.contract.get("ask")
        row["_selected_dte"] = sel.contract.get("dte")
        row["_selected_total_debit"] = sel.contract.get(
            "total_debit", total_debit_dollars(sel.contract.get("ask", 0.0))
        )
    else:
        row["_selected_delta"] = None
        row["_selected_ask_at_selection"] = None
        row["_selected_dte"] = None
        row["_selected_total_debit"] = None
    row["heff_smc_trigger"] = sig["trigger"]
    row["heff_smc_score"] = sig["score"]
    row["heff_smc_in_charter_window"] = sig["in_charter_window"]
    row["time_of_day_bucket"] = time_of_day_bucket(sig["decision_ts"])
    row["segment"] = segment_of(sig["session"])
    return row


def _simulate_variant_session(variant_name: str, config: SelectorConfig, signals: list,
                               trades_df: pd.DataFrame, policy=ADMISSION_POLICY) -> list:
    """Own OpenBook/admission state per variant per session -- each variant
    is an independent chronological replay of the SAME signals, exactly
    mirroring bt3_b1_indicator_only.simulate_b1_session's control flow.

    Self-contained: installs and restores BOTH monkeypatches itself
    (evaluate_candidate for the debit-cap variant, select_contract's delta
    capture for every variant) rather than relying on a caller to have set
    them up -- callable and independently testable on its own, not just as
    part of run_all_variants()'s whole-run patch scope."""
    book = OpenBook()
    releases: dict = {}
    rows = []
    debit_cap = variant_name in DEBIT_CAP_VARIANTS
    if debit_cap:
        bt2_selector.evaluate_candidate = _make_debit_cap_evaluate_candidate(
            DEFAULT_QUANTITY, DEFAULT_FEE_PER_CONTRACT
        )
    bt2_simulator_mod.select_contract = _capturing_select_contract
    try:
        for sig in sorted(signals, key=lambda item: pd.Timestamp(item["decision_ts"])):
            decision_ts = pd.Timestamp(sig["decision_ts"])
            _release_due(book, releases, decision_ts)
            row = _simulate_signal_with_config(
                sig, trades_df, config,
                admission_check=lambda occ, qty: admit(book, occ, qty, policy),
            )
            cid = row.get("contract_id")
            if cid and row.get("exit_reason") != "ADMISSION_REJECT":
                row["admission_decision"] = ADMIT
                row["admission_reason"] = "ok"
                book.add(cid, 1)
                if row.get("entry_fill"):
                    if row.get("exit_fill") and row.get("exit_quote_ts"):
                        releases[cid] = pd.Timestamp(row["exit_quote_ts"])
                else:
                    releases[cid] = (
                        decision_ts
                        + pd.Timedelta(seconds=FILL_CONFIG.reaction_latency_seconds)
                        + pd.Timedelta(seconds=FILL_CONFIG.entry_ttl_seconds)
                    )
            rows.append(row)
    finally:
        if debit_cap:
            bt2_selector.evaluate_candidate = _real_evaluate_candidate
        bt2_simulator_mod.select_contract = _real_select_contract
    return rows


def run_all_variants(events_path: Path = V22_EVENTS_PATH,
                      raw_dir: Path = BACKFILL_RAW_DIR,
                      variants: dict = None) -> dict:
    """Single I/O pass: each session's raw trades are loaded exactly once
    and shared across every variant's own independent chronological
    replay -- avoids N separate full-dataset loads for N variants on a
    single-core, RAM-constrained box."""
    variants = variants or VARIANT_CONFIGS
    events = load_triangle_events(events_path)
    signals = generate_b1_signals(events)
    by_session: dict = {}
    for sig in signals:
        by_session.setdefault(sig["session"], []).append(sig)

    all_rows = {name: [] for name in variants}
    for session in sorted(by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            raise RuntimeError(
                f"aborting before session {session}: only {free_mb:.0f}MB RAM "
                f"free (< {MIN_FREE_RAM_MB}MB floor)"
            )
        session_signals = by_session[session]
        symbol = session_signals[0]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, raw_dir)
        for name, config in variants.items():
            # _simulate_variant_session installs/restores its own patches
            # (evaluate_candidate for debit-cap variants, select_contract's
            # delta capture for every variant) -- self-contained per call.
            all_rows[name].extend(
                _simulate_variant_session(name, config, session_signals, trades_df)
            )
        del trades_df
    return all_rows
