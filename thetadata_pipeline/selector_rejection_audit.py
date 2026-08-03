"""SELECTOR_REJECTION_AUDIT_v1 -- rejection funnel analysis for the frozen v2.2
B1 run (roadmap item: "NEXT AUTONOMOUS TASK -- selector rejection audit",
HANDOFF_2026-08-01.md).

Read-only diagnostic. Never edits selection LOGIC, cron, arming state, or
broker state. Pinned to one specific frozen run so its 1,804-signal / 406-fill
funnel is never confused with any other point-in-time/admission definition:

    run_id         20260801T043948Z-a51b7f (bt3_b1_heff_smc_indicator_only)
    git_commit     37ed48d6931a3b3b7d9af3f1169c8f4443fc7fa0
    events_path    data/thetadata/heff_smc_replay/triangle_events_v2.2.json
    indicator sha  360aeadf18f6c3247307fe54d154994d84f8dbd05c487a9ceeebc73c4f04fb76

The production ledger (bt2_simulator.simulate_trade -> bt2_selector.select_contract)
only ever records the COARSE outcome for a rejected signal
(data_quality="no_candidates_in_book" | "no_candidate_passed_all_rules"), not
which of Section 6's per-candidate rules did the rejecting. bt2_selector.
evaluate_candidate already computes every applicable reason for every
candidate and never short-circuits -- it just is not persisted anywhere. This
module recovers that detail by monkeypatching bt2_selector.evaluate_candidate
to a wrapper that calls the REAL function UNCHANGED (so every selection, fill,
admission decision and net_pnl stays byte-identical to the frozen run -- see
verify_parity(), which is asserted before any output here is trusted) and
additionally stashes a copy of each candidate's full per-rule reasons list.
Selector code itself is never edited; only an external capture is added.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import bt2_selector
from .bt2_selector import (
    NO_CONTRACT_REASON_NO_CANDIDATES, NO_CONTRACT_REASON_NONE_PASS,
    evaluate_candidate as _real_evaluate_candidate,
)
from .backfill import MIN_FREE_RAM_MB, _free_ram_mb
from .bt3_b0_random_control import BACKFILL_RAW_DIR, _load_raw_session_trades, _profit_factor
from .bt3_b1_indicator_only import (
    ADMISSION_POLICY, FILL_CONFIG, generate_b1_signals, load_triangle_events,
    session_bootstrap_mean_ci, simulate_b1_signal, summarize_b1, _release_due,
)
from .bt3_b1_v22 import V22_EVENTS_PATH, V21_BASELINE
from .bt_dedup import ADMIT, OpenBook, admit
from .bt_run import RUNS_DIR, _git_commit, _hash_file
from .heff_smc_replay import TRIANGLE_EVENTS_PATH as V21_EVENTS_PATH

ROOT = Path(__file__).resolve().parent.parent

FROZEN_RUN_ID = "20260801T043948Z-a51b7f"
FROZEN_EXPERIMENT_ID = "bt3_b1_heff_smc_indicator_only"
FROZEN_RUN_DIR = RUNS_DIR / FROZEN_EXPERIMENT_ID / FROZEN_RUN_ID
FROZEN_GIT_COMMIT = "37ed48d6931a3b3b7d9af3f1169c8f4443fc7fa0"
FROZEN_PINE_SHA256 = "360aeadf18f6c3247307fe54d154994d84f8dbd05c487a9ceeebc73c4f04fb76"

AUDIT_EXPERIMENT_ID = "selector_rejection_audit_v1"

# Same fixed order evaluate_candidate() itself checks rules in -- this is the
# waterfall order used for "first rejection reason", not an arbitrary choice.
RULE_ORDER = [
    "no_two_sided_quote", "dte", "premium_band", "spread_dollars",
    "spread_pct", "delta_floor", "quote_age", "ask_size",
]

# Same bucket vocabulary as live_heff_smc_selector.py's _near_miss_diagnostics,
# with one addition: a dedicated "no_two_sided_quote" bucket for
# evaluate_candidate's short-circuit reason, which the live diagnostic
# currently folds into "other" (see SELECTOR_REJECTION_AUDIT_v1.md
# Observations -- flagged, not changed, since it is diagnostic-only code
# with no effect on any PASS/FAIL selection decision).
CATEGORY_GROUP = {
    "no_candidates_in_book": "missing_chain_data",
    "no_two_sided_quote": "stale_or_incomplete_quotes",
    "quote_age": "stale_or_incomplete_quotes",
    "dte": "expiration_failure",
    "delta_floor": "strike_delta_failure",
    "spread_dollars": "spread",
    "spread_pct": "spread",
    "ask_size": "liquidity_ask_size",
    "premium_band": "premium_limit",
    "other": "implementation_or_unclassified",
}

_capture_buffer: list = []


def _capturing_evaluate_candidate(row, decision_date, config):
    result = _real_evaluate_candidate(row, decision_date, config)
    _capture_buffer.append(result)
    return result


def classify_reason(reason: str) -> str:
    if reason.startswith("no valid two-sided quote"):
        return "no_two_sided_quote"
    if "premium band" in reason:
        return "premium_band"
    if "delta" in reason:
        return "delta_floor"
    if "spread" in reason and "% of mid" in reason:
        return "spread_pct"
    if "spread" in reason:
        return "spread_dollars"
    if "quote age" in reason:
        return "quote_age"
    if "ask size" in reason:
        return "ask_size"
    if "DTE" in reason:
        return "dte"
    return "other"


def assert_frozen_inputs_unchanged() -> dict:
    """Refuses to proceed if the code that produced FROZEN_RUN_ID has since
    changed -- this audit's instrumentation only describes that run if it
    replays byte-identical code against the byte-identical cached events
    file. Raises rather than silently analyzing a different run."""
    manifest = json.loads((FROZEN_RUN_DIR / "manifest.json").read_text())
    mismatches = {}
    for rel, expected in manifest["code_hashes"].items():
        actual = _hash_file(rel)
        if actual != expected:
            mismatches[rel] = {"frozen": expected, "current": actual}
    if mismatches:
        raise RuntimeError(
            f"Code changed since frozen run {FROZEN_RUN_ID}; audit would not "
            f"describe that run. Mismatched files: {mismatches}"
        )
    current_commit = _git_commit()
    if current_commit != manifest["git_commit"]:
        raise RuntimeError(
            f"git HEAD moved since the frozen run ({manifest['git_commit']} -> "
            f"{current_commit}). Re-verify before trusting this audit."
        )
    if not V22_EVENTS_PATH.exists():
        raise RuntimeError(f"cached v2.2 events file missing: {V22_EVENTS_PATH}")
    events = json.loads(V22_EVENTS_PATH.read_text())
    if events.get("indicator_version") != "v2.2":
        raise RuntimeError("cached events file is not tagged indicator_version=v2.2")
    if len(events["events"]) != manifest["v22_actual"]["n_signals"]:
        raise RuntimeError(
            f"cached v2.2 events file has {len(events['events'])} events, "
            f"frozen run recorded {manifest['v22_actual']['n_signals']}"
        )
    return manifest


def _audited_simulate_b1_session(signals: list, trades_df: pd.DataFrame,
                                  policy=ADMISSION_POLICY) -> list:
    """Identical control flow to bt3_b1_indicator_only.simulate_b1_session,
    replicated (not imported) so each signal's full per-candidate
    evaluate_candidate() output can be captured alongside its ledger row.
    Selector/fill/exit/admission LOGIC is untouched -- only
    evaluate_candidate is wrapped, and the wrapper calls the real function
    unchanged before recording a copy, so this can never change what gets
    selected, admitted, or filled."""
    book = OpenBook()
    releases: dict = {}
    records = []
    for sig in sorted(signals, key=lambda item: pd.Timestamp(item["decision_ts"])):
        decision_ts = pd.Timestamp(sig["decision_ts"])
        _release_due(book, releases, decision_ts)
        _capture_buffer.clear()
        row = simulate_b1_signal(
            sig, trades_df,
            admission_check=lambda occ, qty: admit(book, occ, qty, policy),
        )
        candidates = list(_capture_buffer)
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
        records.append({"signal": sig, "row": row, "candidates": candidates})
    return records


def reproduce_with_candidates(events_path: Path = V22_EVENTS_PATH,
                               raw_dir: Path = BACKFILL_RAW_DIR) -> list:
    """Full 162-session replay, instrumented. Returns one record per signal:
    {"signal": ..., "row": ..., "candidates": [...]}, in the exact same
    order run_b1() would have produced the frozen ledger's rows in."""
    events = load_triangle_events(events_path)
    signals = generate_b1_signals(events)
    if len(events) != len(signals):
        raise RuntimeError("generate_b1_signals changed its 1:1 event->signal contract")
    # generate_b1_signals drops the raw indicator `factors` breakdown (rvol,
    # htf, structure, ...) -- reattach it here (read-only, extra key,
    # simulate_b1_signal only reads the specific keys it needs so this is
    # inert to production behavior) for the vol-regime split.
    for e, s in zip(events, signals):
        s["_factors"] = e.get("factors", {})
        s["_htf_bias"] = e.get("htf_bias")

    by_session: dict = {}
    for sig in signals:
        by_session.setdefault(sig["session"], []).append(sig)

    bt2_selector.evaluate_candidate = _capturing_evaluate_candidate
    try:
        all_records = []
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
            all_records.extend(_audited_simulate_b1_session(session_signals, trades_df))
            del trades_df
        return all_records
    finally:
        bt2_selector.evaluate_candidate = _real_evaluate_candidate


def verify_parity(records: list, frozen_report: dict) -> dict:
    """Recomputes summarize_b1() on the reproduced rows and compares against
    the frozen run's own report.json summary. Raises on any mismatch --
    this audit's per-signal detail is only trustworthy if the aggregate it
    reproduces is the SAME aggregate already reported for FROZEN_RUN_ID."""
    rows = [r["row"] for r in records]
    resummarized = summarize_b1(rows)
    frozen_summary = frozen_report["summary"]
    check_fields = [
        "n_total_signals", "n_no_contract", "n_admission_rejected",
        "n_contract_pass_no_fill", "n_filled", "n_sessions_with_signal",
        "n_sessions_with_fill", "net_expectancy_per_filled_trade",
        "profit_factor", "win_rate",
    ]
    mismatches = {}
    for f in check_fields:
        a, b = resummarized.get(f), frozen_summary.get(f)
        if a is None or b is None:
            if a != b:
                mismatches[f] = {"reproduced": a, "frozen": b}
            continue
        if abs(float(a) - float(b)) > 1e-6:
            mismatches[f] = {"reproduced": a, "frozen": b}
    total_pnl_repro = round(sum(r["net_pnl"] for r in rows if r.get("net_pnl") is not None), 2)
    total_pnl_frozen = frozen_report["v22_actual"]["total_net_pnl"]
    if abs(total_pnl_repro - total_pnl_frozen) > 0.02:
        mismatches["total_net_pnl"] = {"reproduced": total_pnl_repro, "frozen": total_pnl_frozen}
    if mismatches:
        raise RuntimeError(f"Reproduction does NOT match frozen run {FROZEN_RUN_ID}: {mismatches}")
    return {"status": "PARITY_CONFIRMED", "run_id": FROZEN_RUN_ID, "fields_checked": check_fields}


def time_of_day_bucket(ts) -> str:
    t = pd.Timestamp(ts).time()
    if t < dt.time(9, 30):
        return "pre_open"
    if t < dt.time(10, 0):
        return "09:30-10:00"
    if t < dt.time(11, 0):
        return "10:00-11:00"
    if t < dt.time(12, 0):
        return "11:00-12:00"
    if t < dt.time(13, 0):
        return "12:00-13:00"
    if t < dt.time(14, 0):
        return "13:00-14:00"
    if t < dt.time(15, 0):
        return "14:00-15:00"
    if t < dt.time(15, 30):
        return "15:00-15:30"
    return "15:30-close"


def outcome_of(row: dict) -> str:
    if row.get("net_pnl") is not None:
        return "FILLED"
    if row.get("exit_reason") == "ADMISSION_REJECT":
        return "ADMISSION_REJECT"
    if row.get("exit_reason") == "NO_CONTRACT":
        return "NO_CONTRACT"
    if row.get("exit_reason") == "NO_FILL":
        return "NO_FILL"
    return "UNKNOWN:" + str(row.get("exit_reason"))


def _first_and_all_reasons(candidates: list) -> dict:
    """candidates: every evaluate_candidate() dict for one signal (already
    the direction-appropriate right only -- exactly what select_contract
    itself evaluated, no more, no less). 'First rejection reason' is read
    off the single least-bad candidate (fewest failing rules; ties broken
    by spread_pct_mid, same quality convention as bt2_selector._quality_key)
    in evaluate_candidate's own fixed check order (RULE_ORDER). 'Every
    applicable reason' is the union of categories across ALL candidates
    checked, not just the closest one."""
    if not candidates:
        return {"primary_category": None, "all_categories": [], "n_candidates_checked": 0,
                "closest_candidate_reasons": [], "closest_candidate_strike": None,
                "closest_candidate_expiration": None}
    least_bad = min(candidates, key=lambda c: (len(c["reasons"]), c.get("spread_pct_mid", float("inf"))))
    primary = None
    least_bad_cats = {classify_reason(r) for r in least_bad["reasons"]}
    for rule in RULE_ORDER:
        if rule in least_bad_cats:
            primary = rule
            break
    all_cats = sorted({classify_reason(r) for c in candidates for r in c["reasons"]})
    return {
        "primary_category": primary,
        "all_categories": all_cats,
        "n_candidates_checked": len(candidates),
        "closest_candidate_reasons": least_bad["reasons"],
        "closest_candidate_strike": least_bad.get("strike"),
        "closest_candidate_expiration": least_bad.get("expiration"),
    }


def build_signal_record(rec: dict) -> dict:
    sig, row, candidates = rec["signal"], rec["row"], rec["candidates"]
    outcome = outcome_of(row)
    base = {
        "context_snapshot_id": row.get("context_snapshot_id"),
        "session": sig["session"],
        "decision_ts": sig["decision_ts"].isoformat(),
        "bar_index": sig["bar_index"],
        "time_of_day_bucket": time_of_day_bucket(sig["decision_ts"]),
        "trigger": sig["trigger"], "side": sig["direction"], "score": sig["score"],
        "underlying_price": sig.get("underlying_price"),
        "rvol_regime": "rvol_elevated" if sig.get("_factors", {}).get("rvol", 0) > 0 else "rvol_normal",
        "in_charter_window": sig["in_charter_window"],
        "outcome": outcome,
        "net_pnl": row.get("net_pnl"),
        "rejection_group": None, "primary_category": None,
    }
    if outcome == "NO_CONTRACT":
        coarse = row.get("data_quality")
        if coarse == NO_CONTRACT_REASON_NO_CANDIDATES:
            base["rejection_group"] = "missing_chain_data"
            base["primary_category"] = "no_candidates_in_book"
            base["all_categories"] = ["no_candidates_in_book"]
            base["n_candidates_checked"] = 0
        elif coarse == NO_CONTRACT_REASON_NONE_PASS:
            detail = _first_and_all_reasons(candidates)
            base.update({
                "primary_category": detail["primary_category"],
                "all_categories": detail["all_categories"],
                "n_candidates_checked": detail["n_candidates_checked"],
                "closest_candidate_reasons": detail["closest_candidate_reasons"],
                "closest_candidate_strike": detail["closest_candidate_strike"],
                "closest_candidate_expiration": detail["closest_candidate_expiration"],
            })
            base["rejection_group"] = (
                CATEGORY_GROUP.get(detail["primary_category"], "implementation_or_unclassified")
                if detail["primary_category"] else "implementation_or_unclassified"
            )
        else:
            base["rejection_group"] = "implementation_or_unclassified"
            base["primary_category"] = f"unrecognized_data_quality:{coarse}"
    elif outcome == "ADMISSION_REJECT":
        base["rejection_group"] = "admission_risk_policy"
        base["primary_category"] = row.get("admission_decision")
        base["admission_reason_detail"] = row.get("admission_reason")
    elif outcome == "NO_FILL":
        base["rejection_group"] = "no_fill_after_admission"
        base["primary_category"] = row.get("data_quality")
    return base


def build_funnel(records: list) -> tuple:
    per_signal = [build_signal_record(r) for r in records]
    rejected = [r for r in per_signal if r["outcome"] != "FILLED"]

    def counter_by(key_fn) -> dict:
        c = Counter(key_fn(r) for r in per_signal)
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    def split_by(key_fn) -> dict:
        buckets: dict = defaultdict(Counter)
        for r in per_signal:
            buckets[key_fn(r)][r["outcome"]] += 1
        return {k: dict(v) for k, v in sorted(buckets.items())}

    funnel = {
        "n_total_signals": len(per_signal),
        "by_outcome": counter_by(lambda r: r["outcome"]),
        "by_rejection_group": dict(sorted(Counter(r["rejection_group"] for r in rejected).items(), key=lambda kv: -kv[1])),
        "by_primary_category": dict(sorted(Counter(r["primary_category"] for r in rejected).items(), key=lambda kv: -kv[1])),
        "splits": {
            "by_trigger": split_by(lambda r: r["trigger"]),
            "by_side": split_by(lambda r: r["side"]),
            "by_time_of_day": split_by(lambda r: r["time_of_day_bucket"]),
            "by_session": split_by(lambda r: r["session"]),
            "by_vol_regime": split_by(lambda r: r["rvol_regime"]),
        },
    }
    return per_signal, funnel


# --- One-rule-at-a-time counterfactual sensitivity (item 5) ----------------
#
# Rule threshold values are SelectorConfig fields, not booleans, so "remove
# rule X" is defined as: does this candidate have zero REMAINING failing
# reasons once every reason classified as X is discarded? That is exactly
# equivalent to relaxing X's threshold to a value nothing could ever fail
# (e.g. premium_high=+inf), applied to the SAME point-in-time book already
# captured -- it never re-queries data or re-derives delta/IV.
#
# Scope, disclosed rather than hidden: this measures SELECTOR-GATE
# sensitivity only ("would at least one already-checked candidate now
# pass Section 6's rules") -- NOT fill rate, admission, or expectancy.
# Answering those would require a full chronological re-simulation (new
# OpenBook state, new entry-fill/exit-fill checks) per rule, i.e. one more
# full 162-session replay PER rule. This box is single-core and RAM-
# constrained (see backfill.MIN_FREE_RAM_MB); running 7 more full replays
# in one sitting was not attempted. Never combines relaxations -- each
# candidate is tested against removing exactly one rule at a time,
# independently of every other rule.
RELAXABLE_RULES = [
    "dte", "premium_band", "spread_dollars", "spread_pct",
    "delta_floor", "quote_age", "ask_size",
]


def _candidate_passes_without_rule(candidate: dict, rule: str) -> bool:
    remaining = [r for r in candidate["reasons"] if classify_reason(r) != rule]
    return len(remaining) == 0


def counterfactual_gate_sensitivity(records: list) -> dict:
    """For every NO_CONTRACT signal whose selector reason was
    'no_candidate_passed_all_rules' (candidates were actually evaluated,
    as opposed to 'no_candidates_in_book' where there was nothing to
    relax), tests each rule in RELAXABLE_RULES independently."""
    overall = {rule: {"signals_would_pass_gate": 0, "by_trigger": Counter()} for rule in RELAXABLE_RULES}
    n_eligible = 0
    for rec in records:
        sig, row, candidates = rec["signal"], rec["row"], rec["candidates"]
        if outcome_of(row) != "NO_CONTRACT":
            continue
        if row.get("data_quality") != NO_CONTRACT_REASON_NONE_PASS:
            continue
        if not candidates:
            continue
        n_eligible += 1
        for rule in RELAXABLE_RULES:
            if any(_candidate_passes_without_rule(c, rule) for c in candidates):
                overall[rule]["signals_would_pass_gate"] += 1
                overall[rule]["by_trigger"][sig["trigger"]] += 1
    return {
        "n_eligible_no_contract_signals": n_eligible,
        "scope_note": (
            "Gate-pass-only counterfactual: counts signals that would clear "
            "contract SELECTION if this one rule alone were relaxed. Does NOT "
            "simulate the resulting admission/fill/exit cascade or PnL -- "
            "see module docstring above counterfactual_gate_sensitivity(). "
            "Exploratory, non-promotable: BT0_CHARTER's development window "
            "closed 2026-07-30."
        ),
        "by_rule": {
            rule: {
                "signals_would_pass_gate": v["signals_would_pass_gate"],
                "pct_of_eligible_no_contract_signals": (
                    round(100 * v["signals_would_pass_gate"] / n_eligible, 1) if n_eligible else None
                ),
                "by_trigger": dict(sorted(v["by_trigger"].items(), key=lambda kv: -kv[1])),
            }
            for rule, v in overall.items()
        },
    }


# --- Trigger comparison with confidence intervals (item 4) -----------------

def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> Optional[dict]:
    """Wilson score 95% CI on a proportion -- the standard small-sample
    interval, appropriate here because some triggers (SWEEP_RECLAIM: 27
    filled trades) are far too small for a normal approximation to the
    binomial to be trustworthy. Not the same statistic as
    session_bootstrap_mean_ci (that's a cluster bootstrap on net_pnl dollars,
    this is a score interval on a win/loss count) -- both are reported side
    by side, never substituted for each other."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z ** 2 / n
    center = (p + z ** 2 / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z ** 2 / (4 * n ** 2)) ** 0.5) / denom
    return {
        "point_estimate": round(p, 4), "lower": round(max(0.0, center - margin), 4),
        "upper": round(min(1.0, center + margin), 4), "n": n, "k": k,
        "method": "wilson_score_95ci",
    }


def trigger_comparison_with_ci(rows: list) -> dict:
    """Per-trigger comparison using the SAME session-level cluster bootstrap
    already used everywhere else in this codebase for expectancy CIs
    (summarize_b1's own net_expectancy_session_bootstrap_95ci, per
    BT0_CHARTER.md Section 4's session-level-not-trade-level rule), plus a
    Wilson score 95% CI on win rate. `rows` is any list of BT-2-shaped
    ledger rows carrying heff_smc_trigger/session/net_pnl -- pass the
    frozen ledger's own rows directly for a result that requires no replay
    at all."""
    out = {}
    triggers = sorted({r.get("heff_smc_trigger", "UNKNOWN") for r in rows})
    for trig in triggers:
        trig_rows = [r for r in rows if r.get("heff_smc_trigger") == trig]
        filled = [r for r in trig_rows if r.get("net_pnl") is not None]
        n_signals = len(trig_rows)
        n_filled = len(filled)
        wins = [r for r in filled if r["net_pnl"] > 0]
        by_session: dict = {}
        for r in filled:
            by_session.setdefault(r["session"], []).append(float(r["net_pnl"]))
        ci = session_bootstrap_mean_ci(by_session) if len(by_session) >= 2 else None
        pnls = [float(r["net_pnl"]) for r in filled]
        out[trig] = {
            "n_signals": n_signals,
            "n_filled": n_filled,
            "fill_rate": round(n_filled / n_signals, 4) if n_signals else None,
            "win_rate": round(len(wins) / n_filled, 4) if n_filled else None,
            "win_rate_wilson_95ci": wilson_ci(len(wins), n_filled) if n_filled else None,
            "net_expectancy_per_filled_trade": round(sum(pnls) / n_filled, 2) if n_filled else None,
            "net_expectancy_session_bootstrap_95ci": ci,
            "profit_factor": _profit_factor(pnls) if pnls else None,
            "n_sessions_with_fill": len(by_session),
        }
    return out


def candidates_index(records: list) -> dict:
    """Persists every captured candidate for every NO_CONTRACT/none_pass
    signal, keyed by context_snapshot_id, so counterfactual_gate_sensitivity
    (and any future one-rule check) is reproducible from saved artifacts
    without re-running the 162-session replay. Omits FILLED/admission-
    rejected/no-candidates-in-book signals -- nothing was evaluated for
    those, there is nothing to index."""
    index = {}
    for rec in records:
        row, candidates = rec["row"], rec["candidates"]
        if outcome_of(row) != "NO_CONTRACT":
            continue
        if row.get("data_quality") != NO_CONTRACT_REASON_NONE_PASS or not candidates:
            continue
        index[row["context_snapshot_id"]] = candidates
    return index
