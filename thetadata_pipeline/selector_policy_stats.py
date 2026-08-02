"""Statistics layer for selector_policy_experiment.py -- turns raw ledger
rows (one per signal, per variant) into every metric heff's experiment spec
asked for. Pure functions over already-simulated rows; runs no replay
itself and never touches the frozen baseline.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Optional

import numpy as np

from .bt2_schemas import GATE_PASS
from .bt3_b0_random_control import _profit_factor
from .bt3_b1_indicator_only import session_bootstrap_mean_ci
from .selector_rejection_audit import trigger_comparison_with_ci, wilson_ci


def filter_segment(rows: list, segment: str) -> list:
    if segment == "full":
        return list(rows)
    return [r for r in rows if r.get("segment") == segment]


def max_drawdown_dollars(rows: list) -> Optional[dict]:
    """Chronological cumulative-P&L max drawdown over FILLED trades only,
    ordered by exit_ts (falls back to decision_ts if exit_ts missing --
    never happens for a filled trade in this dataset, guarded anyway).
    Single-contract, non-compounding: this is a dollar drawdown on the
    realized-trade P&L stream, not an account-equity/percentage figure --
    no capital base is defined anywhere else in the B-series either."""
    filled = [r for r in rows if r.get("net_pnl") is not None]
    if not filled:
        return None
    ordered = sorted(filled, key=lambda r: r.get("exit_ts") or r["decision_ts"])
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    peak_ts, trough_ts = None, None
    running_peak_ts = ordered[0].get("exit_ts") or ordered[0]["decision_ts"]
    for r in ordered:
        cum += float(r["net_pnl"])
        if cum > peak:
            peak = cum
            running_peak_ts = r.get("exit_ts") or r["decision_ts"]
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
            peak_ts, trough_ts = running_peak_ts, (r.get("exit_ts") or r["decision_ts"])
    return {
        "max_drawdown_dollars": round(max_dd, 2),
        "final_cumulative_pnl": round(cum, 2),
        "peak_ts": peak_ts, "trough_ts": trough_ts,
        "n_trades_in_curve": len(ordered),
    }


def delta_distribution(rows: list) -> Optional[dict]:
    """Distribution of the ACTUAL selected contract's delta -- for every
    signal that cleared contract SELECTION (contract_gate == GATE_PASS),
    not just filled ones, so a variant that selects contracts it then
    fails to fill still shows what it was trying to select."""
    deltas = [
        abs(r["_selected_delta"]) for r in rows
        if r.get("contract_gate") == GATE_PASS and r.get("_selected_delta") is not None
    ]
    if not deltas:
        return None
    buckets = Counter()
    for d in deltas:
        if d < 0.10:
            buckets["<0.10"] += 1
        elif d < 0.15:
            buckets["0.10-0.15"] += 1
        elif d < 0.20:
            buckets["0.15-0.20"] += 1
        elif d < 0.30:
            buckets["0.20-0.30"] += 1
        elif d < 0.40:
            buckets["0.30-0.40"] += 1
        elif d < 0.50:
            buckets["0.40-0.50"] += 1
        else:
            buckets["0.50+"] += 1
    return {
        "n": len(deltas),
        "mean_abs_delta": round(statistics.mean(deltas), 4),
        "median_abs_delta": round(statistics.median(deltas), 4),
        "stdev_abs_delta": round(statistics.pstdev(deltas), 4) if len(deltas) > 1 else 0.0,
        "min_abs_delta": round(min(deltas), 4),
        "max_abs_delta": round(max(deltas), 4),
        "target_delta_0.35_mean_absolute_error": round(statistics.mean(abs(d - 0.35) for d in deltas), 4),
        "histogram": dict(sorted(buckets.items())),
    }


def premium_stats(rows: list) -> dict:
    selected_asks = [
        r["_selected_ask_at_selection"] for r in rows
        if r.get("contract_gate") == GATE_PASS and r.get("_selected_ask_at_selection") is not None
    ]
    filled_asks = [
        r["entry_ask"][0] for r in rows
        if r.get("net_pnl") is not None and r.get("entry_ask")
    ]
    out = {"selected_at_selection": None, "filled_entry": None}
    if selected_asks:
        out["selected_at_selection"] = {
            "n": len(selected_asks),
            "mean": round(statistics.mean(selected_asks), 4),
            "median": round(statistics.median(selected_asks), 4),
            "min": round(min(selected_asks), 4), "max": round(max(selected_asks), 4),
        }
    if filled_asks:
        out["filled_entry"] = {
            "n": len(filled_asks),
            "mean": round(statistics.mean(filled_asks), 4),
            "median": round(statistics.median(filled_asks), 4),
            "min": round(min(filled_asks), 4), "max": round(max(filled_asks), 4),
        }
    return out


def by_time_of_day(rows: list) -> dict:
    out = {}
    buckets = sorted({r["time_of_day_bucket"] for r in rows})
    for b in buckets:
        b_rows = [r for r in rows if r["time_of_day_bucket"] == b]
        filled = [r for r in b_rows if r.get("net_pnl") is not None]
        pnls = [float(r["net_pnl"]) for r in filled]
        out[b] = {
            "n_signals": len(b_rows), "n_filled": len(filled),
            "fill_rate": round(len(filled) / len(b_rows), 4) if b_rows else None,
            "net_expectancy_per_filled_trade": round(statistics.mean(pnls), 2) if pnls else None,
            "total_net_pnl": round(sum(pnls), 2) if pnls else 0.0,
        }
    return out


def gross_vs_net(rows: list) -> Optional[dict]:
    """'results after fees and realistic bid/ask fills' vs the naive
    contemporaneous-midpoint gross figure both fill models already
    compute (bt2_fills) -- surfaces the friction gap explicitly rather
    than reporting only the fee/slippage-inclusive net number."""
    filled = [r for r in rows if r.get("net_pnl") is not None]
    if not filled:
        return None
    net = [float(r["net_pnl"]) for r in filled]
    gross = [float(r["gross_pnl"]) for r in filled if r.get("gross_pnl") is not None]
    fees = [float(r.get("fees") or 0.0) for r in filled]
    return {
        "n_filled": len(filled),
        "net_expectancy_per_trade": round(statistics.mean(net), 2),
        "gross_expectancy_per_trade_midpoint_model": round(statistics.mean(gross), 2) if gross else None,
        "avg_fees_per_trade": round(statistics.mean(fees), 4) if fees else 0.0,
        "total_net_pnl": round(sum(net), 2),
        "total_gross_pnl_midpoint_model": round(sum(gross), 2) if gross else None,
        "friction_dollars_total": round(sum(gross) - sum(net), 2) if gross else None,
    }


def variant_summary(rows: list) -> dict:
    """The full battery heff's experiment spec asked for, for ONE
    variant/segment slice of rows (rows already pre-filtered by
    filter_segment)."""
    n_signals = len(rows)
    selectable = [r for r in rows if r.get("contract_gate") == GATE_PASS]
    filled = [r for r in rows if r.get("net_pnl") is not None]
    admission_rejected = [r for r in rows if r.get("exit_reason") == "ADMISSION_REJECT"]
    net_pnls = [float(r["net_pnl"]) for r in filled]
    wins = [p for p in net_pnls if p > 0]
    by_session: dict = defaultdict(list)
    for r in filled:
        by_session[r["session"]].append(float(r["net_pnl"]))

    return {
        "n_signals": n_signals,
        "n_sessions_with_signal": len({r["session"] for r in rows}),
        "n_selectable_contracts": len(selectable),
        "selection_rate": round(len(selectable) / n_signals, 4) if n_signals else None,
        "n_admission_rejected": len(admission_rejected),
        "n_filled": len(filled),
        "fill_rate_of_all_signals": round(len(filled) / n_signals, 4) if n_signals else None,
        "fill_rate_of_selectable": round(len(filled) / len(selectable), 4) if selectable else None,
        "n_sessions_with_fill": len(by_session),
        "premium": premium_stats(rows),
        "delta_distribution": delta_distribution(rows),
        "net_expectancy_per_filled_trade": round(statistics.mean(net_pnls), 2) if net_pnls else None,
        "net_expectancy_session_bootstrap_95ci": (
            session_bootstrap_mean_ci(dict(by_session)) if len(by_session) >= 2 else None
        ),
        "total_net_pnl": round(sum(net_pnls), 2) if net_pnls else 0.0,
        "max_drawdown": max_drawdown_dollars(rows),
        "win_rate": round(len(wins) / len(filled), 4) if filled else None,
        "win_rate_wilson_95ci": wilson_ci(len(wins), len(filled)) if filled else None,
        "profit_factor": _profit_factor(net_pnls) if net_pnls else None,
        "gross_vs_net": gross_vs_net(rows),
        "by_trigger": trigger_comparison_with_ci(rows),
        "by_time_of_day": by_time_of_day(rows),
    }


def full_report(all_rows: dict) -> dict:
    """all_rows: {variant_name: [ledger_rows]} from run_all_variants()."""
    report = {}
    for variant, rows in all_rows.items():
        report[variant] = {
            seg: variant_summary(filter_segment(rows, seg))
            for seg in ("full", "train", "holdout")
        }
    return report


def sweep_reclaim_premium_relief_check(all_rows: dict) -> dict:
    """Directly answers: does SWEEP_RECLAIM's poor fill rate persist once
    the premium restriction is relaxed/removed? Compares SWEEP_RECLAIM's
    fill rate across variants against the OTHER-triggers' fill rate in the
    SAME variant, on the full 162-session set (segment mixing would just
    add noise to a trigger-level comparison already using session-level
    bootstrap elsewhere)."""
    out = {}
    for variant, rows in all_rows.items():
        sr = [r for r in rows if r.get("heff_smc_trigger") == "SWEEP_RECLAIM"]
        other = [r for r in rows if r.get("heff_smc_trigger") != "SWEEP_RECLAIM"]
        sr_filled = sum(1 for r in sr if r.get("net_pnl") is not None)
        other_filled = sum(1 for r in other if r.get("net_pnl") is not None)
        out[variant] = {
            "sweep_reclaim_n_signals": len(sr),
            "sweep_reclaim_fill_rate": round(sr_filled / len(sr), 4) if sr else None,
            "other_triggers_fill_rate": round(other_filled / len(other), 4) if other else None,
            "gap_percentage_points": (
                round(100 * (other_filled / len(other) - sr_filled / len(sr)), 1)
                if sr and other else None
            ),
        }
    return out
