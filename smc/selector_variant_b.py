"""Live Variant B selector: delta-first contract selection with a $100
total-debit cap, replacing live_heff_smc_selector.py's unvalidated
moderate_combo config (delta 0.10, premium $0.15-$0.40) entirely (heff's
explicit instruction, 2026-08-01: "Remove the unvalidated delta 0.10 /
premium $0.15-$0.40 selector").

Reuses the EXACT debit-cap logic already tested in
thetadata_pipeline/selector_policy_experiment.py (23 tests, including the
fee-inclusion boundary case: ask=$1.00 passes without fee, fails at $100.05
with the $0.05/contract fee) -- not a reimplementation.

Quote-source agnostic: takes a "book" DataFrame in the same shape
bt2_selector.build_point_in_time_book produces (strike, right, expiration,
bid, ask, bid_size, ask_size, delta, quote_age_seconds), so Phase 2's
ThetaData WebSocket cache and this module have one clean, already-tested
seam -- nothing here cares whether the book came from a live stream, a REST
snapshot, or a backtest.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from thetadata_pipeline import bt2_selector
from thetadata_pipeline.bt2_selector import (
    SelectorConfig, SelectionResult, evaluate_candidate as _real_evaluate_candidate,
    select_contract as _real_select_contract,
)
from thetadata_pipeline.selector_policy_experiment import (
    DEBIT_CAP_DOLLARS, DEFAULT_FEE_PER_CONTRACT, DEFAULT_QUANTITY, total_debit_dollars,
)

# SelectorConfig with the premium band effectively disabled -- delta and the
# debit cap (applied via the evaluate_candidate wrapper below) govern
# selection instead. Every OTHER field (allowed_dte, max_spread_dollars,
# max_spread_pct_mid, min_abs_delta, max_quote_age_seconds, min_ask_size,
# target_delta=0.35) is untouched from bt2_selector.SelectorConfig's own
# already-reviewed defaults -- same as Variant B in the policy experiment.
VARIANT_B_CONFIG = SelectorConfig(premium_low=0.0, premium_high=float("inf"))

# heff's explicit instruction: exclude SWEEP_RECLAIM BEFORE selection, and
# preserve MSS/BOS/MA_FADE/PULLBACK weights untouched (nothing in this
# module or heff_smc_engine.py's scoring changes for those four).
EXCLUDED_TRIGGERS = frozenset({"SWEEP_RECLAIM"})


def is_trigger_eligible(trigger: str) -> bool:
    """Callers MUST check this before ever building a book or calling
    select_variant_b_contract for a signal -- exclusion happens at the
    signal-filtering stage, not by relying on the selector to reject it."""
    return trigger not in EXCLUDED_TRIGGERS


def _debit_cap_evaluate_candidate(row, decision_date, config):
    """Calls the REAL evaluate_candidate unchanged, then (only when a valid
    ask exists -- i.e. the quote-validity short-circuit did not already
    fire) appends a debit-cap reason if the total cost exceeds $100
    including fees. Identical wrapper shape to
    selector_policy_experiment._make_debit_cap_evaluate_candidate, kept as
    its own copy here (not imported) so this live module has zero import-
    time dependency on backtest-only machinery beyond the three pure
    functions/constants imported above."""
    result = _real_evaluate_candidate(row, decision_date, config)
    if "ask" not in result:
        return result
    debit = total_debit_dollars(result["ask"], DEFAULT_QUANTITY, DEFAULT_FEE_PER_CONTRACT)
    reasons = list(result["reasons"])
    if debit > DEBIT_CAP_DOLLARS:
        reasons.append(
            f"total debit ${debit:.2f} (ask ${result['ask']:.2f} x 100 x {DEFAULT_QUANTITY} "
            f"+ ${DEFAULT_FEE_PER_CONTRACT * DEFAULT_QUANTITY:.2f} fees) exceeds "
            f"${DEBIT_CAP_DOLLARS:.2f} risk cap"
        )
    out = dict(result)
    out["reasons"] = reasons
    out["passed"] = not reasons
    out["total_debit"] = debit
    return out


def select_variant_b_contract(book: pd.DataFrame, right: str,
                               decision_ts: dt.datetime) -> SelectionResult:
    """The ONLY live entry point for contract selection under the frozen
    candidate. Patches bt2_selector.evaluate_candidate for the duration of
    this call only (same monkeypatch-and-restore pattern already tested in
    selector_policy_experiment.py's _simulate_variant_session -- restored in
    a finally even on exception), so select_contract's own quality-ranking
    logic (lowest spread_pct_mid, then closest to target_delta=0.35) runs
    completely unmodified against whatever book the caller supplies."""
    bt2_selector.evaluate_candidate = _debit_cap_evaluate_candidate
    try:
        return _real_select_contract(book, right, decision_ts, VARIANT_B_CONFIG)
    finally:
        bt2_selector.evaluate_candidate = _real_evaluate_candidate
