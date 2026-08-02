"""BT-3 B1 -- HEFF SMC indicator-only backtest (real triangle events -> BT-2).

BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf's B-series roadmap (see
bt3_b0_random_control.py's own docstring for the B0 null-baseline this
follows): B1 is "indicator only" -- the BT-2 selector/fill/exit machinery
B0 used, with the live system's entry TTL and canonical admission limits now
applied chronologically. The signal source is heff's real HEFF SMC Confluence
v2 indicator (heff_smc_engine.py's faithful replay of
HEFF_SMC_V2_REFERENCE.pine), replayed against real historical 1-min QQQ bars
(heff_smc_replay.py), instead of a random time/direction draw.

Per BT0_CHARTER.md Section 5, this is explicitly NOT evaluated against the
formal pass/fail promotion criteria there (min 30 sessions / positive CI
lower bound / five-session robustness) -- same category-error reasoning as
B0's own summarize_b0() docstring: this reports honest numbers for heff to
judge, not a promotion decision. These 162 sessions have already been used for
development and parameter exploration, so they are not untouched forward data.

Scope decisions made here, explicit and disclosed (not hidden in the
numbers):
  - invalidation_level is None for every B1 trade, identical to B0's wiring
    -- the exit engine's invalidation leg stays disabled here too, exactly as
    it was for B0. A future experiment
    could test wiring the indicator's own structural level in as an
    invalidation level; this one deliberately does not, to keep B1 a clean,
    single-variable upgrade over B0 (only the ENTRY signal source changed).
  - signal session_window is NOT restricted to BT0_CHARTER's 10:00-15:30 ET
    strategy-spec window (unlike B0's own random-draw window) -- the
    indicator's own kill-zone weighting favors 09:30-11:00, so cutting the
    first half hour would remove a meaningful share of its own
    highest-conviction signals. BT-2's simulate_trade() itself has no
    built-in session-window gate (B0 enforced its window only in its own
    signal-sampling code, not in shared BT-2 machinery), so this is a
    genuine, disclosed choice, not an accidental drift from B0's convention.
    The summary reports the in-window/out-of-window split for transparency.
  - GEX-level sweep contribution is inactive for every signal (see
    heff_smc_engine.py's own module docstring) -- inherited from the
    replay, not introduced here.

Isolation: reads heff_smc_replay/triangle_events.json (this project's own
output) and backfill_60session/raw (read-only, same as B0). Writes only
under data/thetadata/bt3_b1_indicator_only/.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .backfill import MIN_FREE_RAM_MB, _free_ram_mb
from .bt2_exits import ExitConfig
from .bt_dedup import ADMIT, AdmissionPolicy, OpenBook, admit
from .bt2_fills import FillConfig
from .bt2_schemas import DIRECTION_CALL_WATCH, DIRECTION_PUT_WATCH, GATE_PASS, SCHEMA_VERSION
from .bt2_selector import SelectorConfig
from .bt2_simulator import TradeInputs, simulate_trade
from .bt3_b0_random_control import (
    BACKFILL_DIR, BACKFILL_RAW_DIR, BOOTSTRAP_SEED, N_BOOTSTRAP,
    _load_raw_session_trades, _profit_factor,
)
from .heff_smc_replay import TRIANGLE_EVENTS_PATH

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("thetadata_pkg.bt3_b1_indicator_only")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"

BT3_B1_DIR = DATA / "bt3_b1_indicator_only"
BT3_B1_LEDGER_PATH = BT3_B1_DIR / "bt3_b1_indicator_only_ledger.json"
BT3_B1_REPORT_PATH = BT3_B1_DIR / "bt3_b1_indicator_only_report.json"
BT3_B1_SUMMARY_PATH = BT3_B1_DIR / "bt3_b1_indicator_only_summary.md"

EXPERIMENT_ID = "bt3_b1_heff_smc_indicator_only"
LABEL_B1 = "B1_HEFF_SMC_INDICATOR_ONLY_NOT_PROMOTION_EVALUATED"

SIDE_TO_DIRECTION = {"long": DIRECTION_CALL_WATCH, "short": DIRECTION_PUT_WATCH}

CHARTER_WINDOW_START = dt.time(10, 0)
CHARTER_WINDOW_END = dt.time(15, 30)

SELECTOR_CONFIG = SelectorConfig()
FILL_CONFIG = FillConfig()
EXIT_CONFIG = ExitConfig()
ADMISSION_POLICY = AdmissionPolicy()


def _atomic_write_json(path: Path, payload) -> None:
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def load_triangle_events(path: Path = TRIANGLE_EVENTS_PATH) -> list:
    payload = json.loads(Path(path).read_text())
    return payload.get("events", [])


def generate_b1_signals(events: list) -> list:
    """One signal per real triangle event -- session/decision_ts/direction/
    trigger/score all read directly from heff_smc_replay's own real output,
    no randomness, no resampling, no filtering (every real triangle event
    becomes a real B1 signal)."""
    signals = []
    for e in events:
        naive = dt.datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S")
        decision_ts = naive.replace(tzinfo=ET)
        signals.append({
            "symbol": e["ticker"], "session": e["session"], "decision_ts": decision_ts,
            "direction": SIDE_TO_DIRECTION[e["side"]], "trigger": e["trigger"], "score": e["score"],
            "underlying_price": e.get("price"),
            "bar_index": e["bar_index"], "in_charter_window": CHARTER_WINDOW_START <= decision_ts.time() < CHARTER_WINDOW_END,
        })
    return signals


def simulate_b1_signal(
    sig: dict, trades_df: pd.DataFrame, greeks: Optional[dict] = None,
    admission_check=None,
) -> dict:
    """Use the shared BT-2 pipeline without reimplementing selection/fills.
    underlying_bars is empty and invalidation_level remains None, matching
    B0's exit wiring; admission_check lets the session replay apply the live
    risk limits before fill evaluation."""
    context_snapshot_id = f"B1-HEFF-SMC:{sig['symbol']}:{sig['session']}:{sig['bar_index']}:{sig['trigger']}"
    inputs = TradeInputs(
        session=sig["session"], symbol=sig["symbol"],
        signal_ts=sig["decision_ts"], decision_ts=sig["decision_ts"],
        direction=sig["direction"], context_snapshot_id=context_snapshot_id,
        experiment_id=EXPERIMENT_ID, quantity=1, invalidation_level=None,
        underlying_price=sig.get("underlying_price"),
    )
    row = simulate_trade(
        inputs, trades_df, pd.DataFrame(), greeks=greeks,
        selector_config=SELECTOR_CONFIG, fill_config=FILL_CONFIG, exit_config=EXIT_CONFIG,
        admission_check=admission_check,
    )
    row["rule_flags"] = list(row["rule_flags"]) + [LABEL_B1]
    row["heff_smc_trigger"] = sig["trigger"]
    row["heff_smc_score"] = sig["score"]
    row["heff_smc_in_charter_window"] = sig["in_charter_window"]
    return row

def _release_due(book: OpenBook, releases: dict, decision_ts) -> None:
    now = pd.Timestamp(decision_ts)
    for occ, release_ts in list(releases.items()):
        if pd.Timestamp(release_ts) <= now:
            book.remove(occ)
            releases.pop(occ, None)


def simulate_b1_session(
    signals: list, trades_df: pd.DataFrame,
    policy: AdmissionPolicy = ADMISSION_POLICY,
) -> list:
    """Replay one session chronologically against the canonical open book.

    A selected contract reserves capacity immediately. A missed entry keeps
    that reservation through the live-equivalent latency plus entry TTL.
    """
    book = OpenBook()
    releases: dict = {}
    rows = []
    for sig in sorted(signals, key=lambda item: pd.Timestamp(item["decision_ts"])):
        decision_ts = pd.Timestamp(sig["decision_ts"])
        _release_due(book, releases, decision_ts)
        row = simulate_b1_signal(
            sig, trades_df,
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
    return rows


def run_b1(
    events_path: Path = TRIANGLE_EVENTS_PATH, raw_dir: Path = BACKFILL_RAW_DIR,
    backfill_dir: Path = BACKFILL_DIR, ledger_path: Path = BT3_B1_LEDGER_PATH,
) -> list:
    events = load_triangle_events(events_path)
    signals = generate_b1_signals(events)
    logger.info("B1: %d real triangle-event signals loaded across %d sessions",
                len(signals), len({s["session"] for s in signals}))

    by_session: dict = {}
    for sig in signals:
        by_session.setdefault(sig["session"], []).append(sig)

    rows = []
    for session in sorted(by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            reason = (
                f"B1 run aborted before session {session}: only {free_mb:.0f}MB RAM free "
                f"(< {MIN_FREE_RAM_MB}MB floor); refusing to overwrite the complete ledger"
            )
            logger.error(reason)
            raise RuntimeError(reason)
        session_signals = by_session[session]
        symbol = session_signals[0]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, raw_dir)
        rows.extend(simulate_b1_session(session_signals, trades_df))
        del trades_df

    _atomic_write_json(ledger_path, {"schema_version": SCHEMA_VERSION, "trades": rows})
    logger.info("B1 run complete: %d ledger rows written to %s", len(rows), ledger_path)
    return rows


# --- Report math -----------------------------------------------------------

def session_bootstrap_mean_ci(
    net_pnls_by_session: dict, n_bootstrap: int = N_BOOTSTRAP, seed: int = BOOTSTRAP_SEED, alpha: float = 0.05,
) -> Optional[dict]:
    """Real session-level (cluster) bootstrap, per BT0_CHARTER.md Section 4
    ("session-level, not trade-level... since intraday trades on the same
    day are not independent draws"). Unlike B0's own bootstrap_mean_ci
    (correct to resample individual TRADES there, since B0 draws exactly
    ONE trade per session so trade-level and session-level coincide), B1
    can have multiple triangle events on the same real session -- this
    resamples whole SESSIONS with replacement, pools every filled trade
    inside each resampled session, and takes the pooled mean net_pnl as the
    bootstrap statistic (a standard cluster/block bootstrap for correlated
    intraday draws). None below 2 sessions."""
    sessions = list(net_pnls_by_session.keys())
    if len(sessions) < 2:
        return None
    session_arrays = [np.asarray(net_pnls_by_session[s], dtype=float) for s in sessions]
    n = len(sessions)
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        pooled = np.concatenate([session_arrays[i] for i in idx])
        if len(pooled):
            means.append(pooled.mean())
    if not means:
        return None
    means = np.array(means)
    all_pnls = np.concatenate(session_arrays)
    return {
        "point_estimate": round(float(all_pnls.mean()), 4),
        "lower": round(float(np.percentile(means, 100 * alpha / 2)), 4),
        "upper": round(float(np.percentile(means, 100 * (1 - alpha / 2))), 4),
        "alpha": alpha, "n_sessions": n, "n_bootstrap": len(means), "seed": seed,
    }


def summarize_b1(rows: list) -> dict:
    """Honest B1 summary math. Deliberately does NOT apply BT0_CHARTER.md
    Section 5's pass/fail success criteria -- same category-error reasoning
    as summarize_b0(): 62 sessions is real data, not a promotion decision."""
    n_total = len(rows)
    no_contract = [r for r in rows if r.get("exit_reason") == "NO_CONTRACT"]
    admission_rejected = [r for r in rows if r.get("exit_reason") == "ADMISSION_REJECT"]
    no_fill = [r for r in rows if r.get("contract_gate") == GATE_PASS and r.get("net_pnl") is None and r.get("exit_reason") != "ADMISSION_REJECT"]
    filled = [r for r in rows if r.get("net_pnl") is not None]

    net_pnls = [float(r["net_pnl"]) for r in filled]
    wins = [p for p in net_pnls if p > 0]
    friction_shares = [
        r["friction_detail"]["friction_share_of_target"] for r in filled
        if r.get("friction_detail") and r["friction_detail"].get("friction_share_of_target") is not None
    ]

    by_session: dict = {}
    for r in filled:
        by_session.setdefault(r["session"], []).append(float(r["net_pnl"]))

    net_expectancy = float(np.mean(net_pnls)) if net_pnls else None
    win_rate = (len(wins) / len(filled)) if filled else None
    profit_factor = _profit_factor(net_pnls) if net_pnls else None
    avg_friction_share = float(np.mean(friction_shares)) if friction_shares else None
    ci = session_bootstrap_mean_ci(by_session) if len(by_session) >= 2 else None

    all_as_zero = [(float(r["net_pnl"]) if r.get("net_pnl") is not None else 0.0) for r in rows]
    expectancy_all_signals = float(np.mean(all_as_zero)) if all_as_zero else None

    by_trigger: dict = {}
    for r in rows:
        trig = r.get("heff_smc_trigger", "UNKNOWN")
        entry = by_trigger.setdefault(trig, {"n": 0, "n_filled": 0, "net_pnls": []})
        entry["n"] += 1
        if r.get("net_pnl") is not None:
            entry["n_filled"] += 1
            entry["net_pnls"].append(float(r["net_pnl"]))
    trigger_breakdown = {
        trig: {
            "n_signals": d["n"], "n_filled": d["n_filled"],
            "net_expectancy": round(float(np.mean(d["net_pnls"])), 2) if d["net_pnls"] else None,
        }
        for trig, d in by_trigger.items()
    }

    session_totals = {s: sum(v) for s, v in by_session.items()}
    top5 = {s for s, _ in sorted(session_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]}
    remainder_pnls = [float(r["net_pnl"]) for r in filled if r["session"] not in top5]
    expectancy_ex_top5 = float(np.mean(remainder_pnls)) if remainder_pnls else None

    in_window = [r for r in rows if r.get("heff_smc_in_charter_window")]
    in_window_filled_pnls = [float(r["net_pnl"]) for r in in_window if r.get("net_pnl") is not None]

    return {
        "label": LABEL_B1,
        "n_total_signals": n_total,
        "n_no_contract": len(no_contract),
        "n_admission_rejected": len(admission_rejected),
        "n_contract_pass_no_fill": len(no_fill),
        "n_filled": len(filled),
        "fill_rate_of_total_signals": (len(filled) / n_total) if n_total else None,
        "n_sessions_with_signal": len({r["session"] for r in rows}),
        "n_sessions_with_fill": len(by_session),
        "net_expectancy_per_filled_trade": round(net_expectancy, 2) if net_expectancy is not None else None,
        "net_expectancy_session_bootstrap_95ci": ci,
        "profit_factor": profit_factor,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_friction_share_of_target": round(avg_friction_share, 4) if avg_friction_share is not None else None,
        "n_friction_share_observations": len(friction_shares),
        "expectancy_all_signals_including_no_trade_as_zero": (
            round(expectancy_all_signals, 2) if expectancy_all_signals is not None else None
        ),
        "trigger_breakdown": trigger_breakdown,
        "robustness_expectancy_excluding_best_5_sessions": (
            round(expectancy_ex_top5, 2) if expectancy_ex_top5 is not None else None
        ),
        "n_signals_in_charter_10_1530_window": len(in_window),
        "net_expectancy_in_charter_window_only": (
            round(float(np.mean(in_window_filled_pnls)), 2) if in_window_filled_pnls else None
        ),
        "bootstrap_method": (
            "session-level (cluster) percentile bootstrap: resample SESSIONS with "
            "replacement, pool every filled trade inside each resampled session, "
            "take the pooled mean net_pnl as the bootstrap statistic -- per "
            "BT0_CHARTER.md Section 4's session-level (not trade-level) rule"
        ),
        "n_bootstrap": N_BOOTSTRAP, "bootstrap_seed": BOOTSTRAP_SEED,
    }


def render_summary_md(summary: dict) -> str:
    ci = summary.get("net_expectancy_session_bootstrap_95ci")
    ci_str = f"${ci['lower']:.2f} to ${ci['upper']:.2f} (n={ci['n_sessions']} sessions)" if ci else "N/A (fewer than 2 sessions with a fill)"
    pf = summary.get("profit_factor")
    pf_str = f"{pf:.2f}" if pf is not None else ("undefined (no losing trades)" if summary.get("n_filled", 0) > 0 else "N/A")
    exp = summary.get("net_expectancy_per_filled_trade")
    exp_str = f"${exp:.2f}" if exp is not None else "N/A"
    wr = summary.get("win_rate")
    wr_str = f"{wr:.1%}" if wr is not None else "N/A"
    fr = summary.get("avg_friction_share_of_target")
    fr_str = f"{fr:.1%} of planned gross target (n={summary.get('n_friction_share_observations', 0)})" if fr is not None else "N/A"
    fill_rate = summary.get("fill_rate_of_total_signals")
    fill_rate_str = f"{fill_rate:.1%}" if fill_rate is not None else "N/A"
    robust = summary.get("robustness_expectancy_excluding_best_5_sessions")
    robust_str = f"${robust:.2f}" if robust is not None else "N/A"
    in_window_exp = summary.get("net_expectancy_in_charter_window_only")
    in_window_str = f"${in_window_exp:.2f}" if in_window_exp is not None else "N/A"

    lines = [
        "# BT-3 B1 -- HEFF SMC Indicator-Only Backtest",
        "",
        "**NOT evaluated against BT0_CHARTER.md Section 5's promotion criteria.**",
        "Real triangle events from a faithful Python port of HEFF_SMC_V2_REFERENCE.pine,",
        "replayed against real historical 1-min QQQ bars through BT-2 selection/fills/exits,",
        "plus the live-matching 20-second entry TTL and chronological admission limits.",
        "quantity=1 and invalidation_level=None; results are not directly comparable to",
        "older B1 ledgers that omitted those execution constraints.",
        "",
        "## Sample",
        f"- Total real triangle-event signals: {summary['n_total_signals']}",
        f"- Sessions with at least one signal: {summary['n_sessions_with_signal']}",
        f"- No contract passed the selector gate (NO CONTRACT): {summary['n_no_contract']}",
        f"- Contract selected but blocked by live-parity admission: {summary['n_admission_rejected']}",
        f"- Contract passed the gate but never filled (NO FILL): {summary['n_contract_pass_no_fill']}",
        f"- Filled trades: {summary['n_filled']}",
        f"- Fill rate (of all {summary['n_total_signals']} signals): {fill_rate_str}",
        f"- Sessions with at least one filled trade: {summary['n_sessions_with_fill']}",
        "",
        "## Result (filled trades only)",
        f"- Net expectancy per filled trade: {exp_str}",
        f"- Session-level (cluster) bootstrap 95% CI on net expectancy: {ci_str}",
        f"- Profit factor: {pf_str}",
        f"- Win rate: {wr_str}",
        f"- Avg friction share of planned target: {fr_str}",
        "",
        "## Robustness / breakdown",
        f"- Net expectancy excluding the best 5 sessions: {robust_str}",
        f"- Signals inside BT0_CHARTER's 10:00-15:30 ET strategy-spec window: {summary['n_signals_in_charter_10_1530_window']} / {summary['n_total_signals']}",
        f"- Net expectancy, charter-window-only signals: {in_window_str}",
        "",
        "## By trigger type",
    ]
    for trig, d in summary.get("trigger_breakdown", {}).items():
        exp_t = f"${d['net_expectancy']:.2f}" if d["net_expectancy"] is not None else "N/A"
        lines.append(f"- {trig}: {d['n_signals']} signals, {d['n_filled']} filled, net expectancy {exp_t}")
    lines += [
        "",
        "Per BT0_CHARTER.md Section 5, this run is NOT evaluated against the success",
        "criteria there. Numbers above are reported as-is, honestly, with no pass/fail",
        "verdict attached -- heff's own call, not this code's.",
    ]
    return "\n".join(lines)


def write_report(
    rows: list, report_path: Path = BT3_B1_REPORT_PATH, summary_path: Path = BT3_B1_SUMMARY_PATH,
) -> dict:
    summary = summarize_b1(rows)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": "B1 HEFF SMC INDICATOR-ONLY -- NOT PROMOTION-EVALUATED",
        "roadmap_reference": "BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf Section 9, B1",
        "signal_source": "heff_smc_replay.py real triangle events (HEFF_SMC_V2_REFERENCE.pine faithful port)",
        "strategy_parameters_held_constant": {
            "symbol": "QQQ", "quantity": 1, "fill_model": FILL_CONFIG.fill_model,
            "selector": {
                "allowed_dte": sorted(SELECTOR_CONFIG.allowed_dte),
                "premium_band": [SELECTOR_CONFIG.premium_low, SELECTOR_CONFIG.premium_high],
                "min_abs_delta": SELECTOR_CONFIG.min_abs_delta, "target_delta": SELECTOR_CONFIG.target_delta,
            },
            "execution": {
                "reaction_latency_seconds": FILL_CONFIG.reaction_latency_seconds,
                "entry_ttl_seconds": FILL_CONFIG.entry_ttl_seconds,
                "admission_policy": {
                    "max_concurrent_positions": ADMISSION_POLICY.max_concurrent_positions,
                    "max_correlated_contracts": ADMISSION_POLICY.max_correlated_contracts,
                    "block_same_contract": ADMISSION_POLICY.block_same_contract,
                },
            },
            "exit": {
                "target_return": EXIT_CONFIG.target_return, "premium_stop_pct": EXIT_CONFIG.premium_stop_pct,
                "time_stop_minutes": EXIT_CONFIG.time_stop_minutes,
                "forced_close_time": EXIT_CONFIG.forced_close_time.isoformat(),
            },
        },
        "data_limitations": [
            "delta is derived point-in-time from the triangle event's underlying price and "
            "latest eligible option midpoint using Black-Scholes (q=0, constant rate); this "
            "removes EOD lookahead but remains a model estimate, not a historical OPRA Greek",
            "the 3-second reaction latency is a frozen research assumption, not a measured "
            "distribution from broker acknowledgements; latency sensitivity must be reported",
            "historical option observations are trade-associated NBBO, not a continuous quote "
            "stream; the replay enforces the 20-second entry window but cannot identify queue "
            "priority, partial fills between observations, or fills racing a cancel when the "
            "source contains no corresponding event",
            "GEX-level sweep contribution is inactive for every signal -- no historical "
            "log of heff's manual daily GEX entries exists (see heff_smc_engine.py)",
            "1-min QQQ bars are RTH-only (no ONH/ONL) and ~0.7% of minutes were "
            "synthetic-filled (Alpaca IEX free feed returns no bar for a no-trade "
            "minute) -- see heff_smc_replay.py's build_continuous_1min_series docstring",
        ],
        "summary": summary,
    }
    _atomic_write_json(report_path, payload)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(render_summary_md(summary))
    logger.info("B1 report written to %s and %s", report_path, summary_path)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result_rows = run_b1()
    result_summary = write_report(result_rows)
    print(json.dumps(result_summary, indent=2, default=str))
