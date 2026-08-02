"""Variant B differential parity harness (heff-requested blocking gate,
2026-08-01): proves that smc/selector_variant_b.py reproduces
selector_policy_experiment_v1's Variant B decision-for-decision, BEFORE the
selector is ever connected to Alpaca PAPER.

Method: for every historical B1 signal, build ONE point-in-time book exactly
the way bt2_simulator.simulate_trade builds it (same call, same args,
greeks=None, same intraday underlying_price), then hand that SAME book object
to both implementations and compare every observable field of the decision.
Feeding one shared book to both paths is what makes this a clean differential
test -- any difference in outcome must come from selector logic, never from
differing inputs. Neither path mutates the book (select_contract only reads),
so sharing it is safe.

This harness NEVER modifies a frozen baseline file. It uses the same
monkeypatch-and-restore-in-finally pattern the experiment itself uses, and
restores in a finally even on exception.

Read-only: places no orders, touches no broker, writes only its own report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import traceback
from pathlib import Path

import pandas as pd

from thetadata_pipeline import bt2_selector
from thetadata_pipeline.bt2_selector import (
    _quality_key,
    build_point_in_time_book,
    evaluate_candidate as _real_evaluate_candidate,
    select_contract as _real_select_contract,
)
from thetadata_pipeline.bt2_schemas import DIRECTION_TO_RIGHT
from thetadata_pipeline.schemas import normalize_right
from thetadata_pipeline.selector_policy_experiment import (
    DEFAULT_FEE_PER_CONTRACT,
    DEFAULT_QUANTITY,
    VARIANT_CONFIGS,
    _make_debit_cap_evaluate_candidate,
    segment_of,
)
from thetadata_pipeline.backfill import MIN_FREE_RAM_MB, _free_ram_mb
from thetadata_pipeline.bt3_b0_random_control import BACKFILL_RAW_DIR, _load_raw_session_trades
from thetadata_pipeline.bt3_b1_indicator_only import generate_b1_signals, load_triangle_events
from thetadata_pipeline.bt3_b1_v22 import V22_EVENTS_PATH

from smc.selector_variant_b import (
    EXCLUDED_TRIGGERS,
    VARIANT_B_CONFIG,
    is_trigger_eligible,
    select_variant_b_contract,
)

ORIGINAL_B_CONFIG = VARIANT_CONFIGS["B_delta_first_debit_cap"]

# Every observable field of a selected contract that parity must hold on.
CONTRACT_FIELDS = [
    "strike", "right", "expiration", "delta", "bid", "ask", "mid", "spread",
    "spread_pct_mid", "total_debit", "dte", "quote_age_seconds", "ask_size",
]


def occ_symbol(symbol, expiration, right, strike):
    """Standard 21-char OCC: root padded to 6, YYMMDD, C/P, strike*1000 in 8."""
    if expiration is None or strike is None:
        return None
    exp = expiration if isinstance(expiration, dt.date) else dt.date.fromisoformat(str(expiration)[:10])
    return (f"{str(symbol).upper():<6}{exp:%y%m%d}"
            f"{normalize_right(right)}{int(round(float(strike) * 1000)):08d}")


def _eq(a, b) -> bool:
    """None/NaN/inf-safe equality. Exact for everything except finite floats,
    which get a 1e-12 tolerance so pure float-repr noise is not reported as a
    behavioral mismatch."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        if math.isinf(a) or math.isinf(b):
            return a == b
        return abs(a - b) <= 1e-12
    return a == b


def _introspect(book, right, decision_ts, config, eval_fn) -> dict:
    """Recomputes select_contract's own evaluation so the ranking tuple and
    tie structure are observable -- select_contract itself pops
    `_delta_distance` off the winner and never exposes the ranked set.
    Mirrors select_contract's control flow exactly (same subset filter, same
    decision_date derivation, same _quality_key)."""
    out = {"n_passing": 0, "winning_key": None, "n_tied_full_key": 0,
           "n_tied_min_spread": 0, "delta_tiebreak_exercised": False}
    if book is None or book.empty:
        return out
    subset = book[book["right"].map(normalize_right) == normalize_right(right)]
    if subset.empty:
        return out
    decision_date = pd.Timestamp(decision_ts).date()
    passing = [c for c in (eval_fn(row, decision_date, config) for _, row in subset.iterrows())
               if c["passed"]]
    if not passing:
        return out
    keys = [_quality_key(c) for c in passing]
    best = min(keys)
    out["n_passing"] = len(passing)
    out["winning_key"] = [
        None if (isinstance(v, float) and math.isinf(v)) else v for v in best
    ]
    out["n_tied_full_key"] = sum(1 for k in keys if k == best)
    out["n_tied_min_spread"] = sum(1 for k in keys if _eq(k[0], best[0]))
    # The delta tie-break only actually decides anything when >1 candidate
    # shares the minimum spread_pct_mid.
    out["delta_tiebreak_exercised"] = out["n_tied_min_spread"] > 1
    return out


def compare_one(book, right, decision_ts, symbol) -> dict:
    """Runs BOTH implementations against one shared book and returns a full
    field-by-field comparison record."""
    patched = _make_debit_cap_evaluate_candidate(DEFAULT_QUANTITY, DEFAULT_FEE_PER_CONTRACT)

    # --- ORIGINAL experiment Variant B path -----------------------------
    bt2_selector.evaluate_candidate = patched
    try:
        orig = _real_select_contract(book, right, decision_ts, ORIGINAL_B_CONFIG)
        intro = _introspect(book, right, decision_ts, ORIGINAL_B_CONFIG, patched)
    finally:
        bt2_selector.evaluate_candidate = _real_evaluate_candidate

    # --- NEW live selector path (installs/restores its own patch) --------
    new = select_variant_b_contract(book, right, decision_ts)

    # Guard: the new module must leave the global exactly as it found it.
    patch_leaked = bt2_selector.evaluate_candidate is not _real_evaluate_candidate

    rec = {
        "orig_found": orig.found, "new_found": new.found,
        "orig_reason": orig.reason, "new_reason": new.reason,
        "orig_candidates_checked": orig.candidates_checked,
        "new_candidates_checked": new.candidates_checked,
        "patch_leaked": patch_leaked,
        "ranking": intro,
        "field_mismatches": [],
        "orig_occ": None, "new_occ": None,
    }

    if orig.found and new.found:
        oc, nc = orig.contract, new.contract
        rec["orig_occ"] = occ_symbol(symbol, oc.get("expiration"), oc.get("right"), oc.get("strike"))
        rec["new_occ"] = occ_symbol(symbol, nc.get("expiration"), nc.get("right"), nc.get("strike"))
        for f in CONTRACT_FIELDS:
            a, b = oc.get(f), nc.get(f)
            if not _eq(a, b):
                rec["field_mismatches"].append({"field": f, "orig": a, "new": b})
        rec["orig_contract"] = {f: oc.get(f) for f in CONTRACT_FIELDS}
        rec["new_contract"] = {f: nc.get(f) for f in CONTRACT_FIELDS}

    # Mismatch classification
    cats = []
    if orig.found != new.found:
        cats.append("selection_vs_rejection")
    if rec["orig_occ"] != rec["new_occ"]:
        cats.append("occ")
    if (not orig.found) and (not new.found) and orig.reason != new.reason:
        cats.append("rejection_reason")
    if rec["field_mismatches"]:
        cats.append("contract_field")
    if orig.candidates_checked != new.candidates_checked:
        cats.append("candidates_checked")
    if patch_leaked:
        cats.append("patch_leak")
    rec["mismatch_categories"] = cats
    rec["match"] = not cats
    return rec


def run(events_path: Path, raw_dir: Path, limit_sessions: int | None) -> dict:
    # Config equivalence is itself part of parity: the new module builds
    # SelectorConfig(...) directly while the experiment used
    # dataclasses.replace(SelectorConfig(), ...). Assert, never assume.
    config_identical = (VARIANT_B_CONFIG == ORIGINAL_B_CONFIG)

    events = load_triangle_events(events_path)
    signals = generate_b1_signals(events)
    by_session: dict = {}
    for sig in signals:
        by_session.setdefault(sig["session"], []).append(sig)

    sessions = sorted(by_session)
    if limit_sessions:
        sessions = sessions[:limit_sessions]

    records = []
    skipped_sessions = []
    for session in sessions:
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            raise RuntimeError(f"aborting before {session}: only {free_mb:.0f}MB free "
                               f"(< {MIN_FREE_RAM_MB}MB floor)")
        sess_signals = by_session[session]
        symbol = sess_signals[0]["symbol"]
        try:
            trades_df = _load_raw_session_trades(symbol, session, raw_dir)
        except Exception as exc:
            skipped_sessions.append({"session": session, "error": f"{type(exc).__name__}: {exc}"})
            continue

        for sig in sorted(sess_signals, key=lambda s: pd.Timestamp(s["decision_ts"])):
            right = DIRECTION_TO_RIGHT[sig["direction"]]
            book = build_point_in_time_book(
                trades_df, sig["decision_ts"], None,
                underlying_price=sig.get("underlying_price"),
            )
            rec = compare_one(book, right, sig["decision_ts"], sig["symbol"])
            rec.update({
                "session": session, "symbol": sig["symbol"],
                "decision_ts": pd.Timestamp(sig["decision_ts"]).isoformat(),
                "trigger": sig["trigger"], "right": right,
                "segment": segment_of(session),
                "eligible": is_trigger_eligible(sig["trigger"]),
            })
            records.append(rec)
        del trades_df

    return summarize(records, config_identical, skipped_sessions)


def summarize(records: list, config_identical: bool, skipped_sessions: list) -> dict:
    eligible = [r for r in records if r["eligible"]]
    excluded = [r for r in records if not r["eligible"]]

    def first_example(rows, cat):
        for r in rows:
            if cat in r["mismatch_categories"]:
                return r
        return None

    cats = ["selection_vs_rejection", "occ", "rejection_reason",
            "contract_field", "candidates_checked", "patch_leak"]

    # Parity is judged on the ELIGIBLE population (the signals the live
    # selector will actually act on). The full population is reported too so
    # the SWEEP_RECLAIM scope change is visible rather than hidden.
    report = {
        "config_identical": config_identical,
        "variant_b_config": str(VARIANT_B_CONFIG),
        "original_b_config": str(ORIGINAL_B_CONFIG),
        "excluded_triggers": sorted(EXCLUDED_TRIGGERS),
        "total_decisions_all_triggers": len(records),
        "total_decisions_eligible": len(eligible),
        "total_decisions_excluded_by_new_selector": len(excluded),
        "skipped_sessions": skipped_sessions,
        "eligible": {
            "exact_matches": sum(1 for r in eligible if r["match"]),
            "mismatches": sum(1 for r in eligible if not r["match"]),
            "both_selected": sum(1 for r in eligible if r["orig_found"] and r["new_found"]),
            "both_rejected": sum(1 for r in eligible if not r["orig_found"] and not r["new_found"]),
            "by_category": {c: sum(1 for r in eligible if c in r["mismatch_categories"]) for c in cats},
            "first_examples": {c: first_example(eligible, c) for c in cats},
        },
        "all_triggers": {
            "exact_matches": sum(1 for r in records if r["match"]),
            "mismatches": sum(1 for r in records if not r["match"]),
            "by_category": {c: sum(1 for r in records if c in r["mismatch_categories"]) for c in cats},
            "first_examples": {c: first_example(records, c) for c in cats},
        },
        # Ranking-order evidence: how often the delta tie-break actually
        # decided anything, and whether any FULL-tuple ties exist (which
        # would make selection depend on book row order).
        "ranking_evidence": {
            "decisions_with_passing_candidates": sum(1 for r in records if r["ranking"]["n_passing"] > 0),
            "delta_tiebreak_exercised": sum(1 for r in records if r["ranking"]["delta_tiebreak_exercised"]),
            "full_key_ties_order_dependent": sum(1 for r in records if r["ranking"]["n_tied_full_key"] > 1),
        },
    }
    report["PARITY_PASS"] = bool(
        config_identical
        and report["eligible"]["mismatches"] == 0
        and report["eligible"]["by_category"]["patch_leak"] == 0
        and len(eligible) > 0
    )
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(V22_EVENTS_PATH))
    ap.add_argument("--raw-dir", default=str(BACKFILL_RAW_DIR))
    ap.add_argument("--limit-sessions", type=int, default=None)
    ap.add_argument("--out", default="VARIANT_B_DIFFERENTIAL_PARITY.json")
    args = ap.parse_args()

    try:
        report = run(Path(args.events), Path(args.raw_dir), args.limit_sessions)
    except Exception:
        traceback.print_exc()
        return 2

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))

    e = report["eligible"]
    print("=" * 62)
    print("VARIANT B DIFFERENTIAL PARITY")
    print("=" * 62)
    print(f"config identical (new vs experiment) : {report['config_identical']}")
    print(f"total decisions (all triggers)       : {report['total_decisions_all_triggers']}")
    print(f"total decisions (eligible)           : {report['total_decisions_eligible']}")
    print(f"excluded by new selector (SWEEP_*)   : {report['total_decisions_excluded_by_new_selector']}")
    print(f"exact matches (eligible)             : {e['exact_matches']}")
    print(f"mismatches (eligible)                : {e['mismatches']}")
    print(f"  both selected                      : {e['both_selected']}")
    print(f"  both rejected                      : {e['both_rejected']}")
    for cat, n in e["by_category"].items():
        print(f"  {cat:<32}: {n}")
    r = report["ranking_evidence"]
    print(f"delta tie-break actually exercised   : {r['delta_tiebreak_exercised']}"
          f" / {r['decisions_with_passing_candidates']} decisions with passing candidates")
    print(f"full-key ties (order-dependent)      : {r['full_key_ties_order_dependent']}")
    if report["skipped_sessions"]:
        print(f"SKIPPED SESSIONS                     : {len(report['skipped_sessions'])}")
    print("-" * 62)
    print(f"PARITY_PASS: {report['PARITY_PASS']}")
    print("=" * 62)
    return 0 if report["PARITY_PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
