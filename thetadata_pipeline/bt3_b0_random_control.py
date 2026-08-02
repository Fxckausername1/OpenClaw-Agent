"""BT-3 B0 -- random-time/random-direction control baseline.

BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf Section 9: "B0 Random-time
control -- Same symbols/time/budget; random eligible direction/time --
Question answered: Is any result just broad market drift?"

THIS IS A NULL-HYPOTHESIS REFERENCE POINT, NOT A STRATEGY RESULT. Every
symbol/session_window/selector/fill-model/exit-engine/quantity choice below
is held IDENTICAL to what a real signal-driven experiment (B1 onward) would
use -- the only thing randomized is WHICH direction (CALL/PUT) and WHAT TIME
within the eligible window each simulated trade enters. If a future real
experiment can't beat these numbers, that experiment has no demonstrated edge
over noise. BT0_CHARTER.md Section 5's pass/fail success criteria are for
judging a real strategy (B6) and are deliberately NOT applied here -- see
summarize_b0()'s docstring.

Reuses bt2_selector/bt2_fills/bt2_exits/bt2_simulator EXACTLY as a real
signal would flow through them -- this module never reimplements selection,
fills, or exits, only the random (symbol, date, time, direction) draw that
feeds simulate_trade(), and the post-hoc report math.

Isolation rule (same as the rest of this package): reads the existing
backfill_60session/ raw parquet + EOD-greeks files (never writes there),
and writes its own outputs only under data/thetadata/bt3_b0_control/. Never
touches live_gex_snapshot.json, vex_history.json, iv_intraday_state.json, or
anything bt1_pilot.py/backfill_60session.py/bt2_simulator.py already own.

Known, inherited data limitation (not introduced here, applies to ANY BT-2
run against this same 124-session backfill, B0 included): greeks are an
EOD-only snapshot (option_history_greeks_eod, backfill_60session.py's own
convention) -- this backfill never persisted intraday/point-in-time greeks,
so bt2_selector's delta gate uses EOD delta as the best available proxy for
point-in-time delta, not a true point-in-time read. Similarly, underlying
1-min bars were fetched by backfill_60session.py only to be counted, never
persisted -- irrelevant for B0 specifically (no invalidation_level exists
for a random draw, so that exit leg is correctly disabled by simulate_trade
already), but flagged here so it isn't mistaken for a B0-specific gap.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .backfill import MIN_FREE_RAM_MB, _free_ram_mb
from .bt2_exits import ExitConfig
from .bt2_fills import FillConfig
from .bt2_schemas import GATE_FAIL, GATE_PASS, VALID_DIRECTION_GATES, build_ledger_row
from .bt2_selector import SelectorConfig
from .bt2_simulator import TradeInputs, append_ledger_rows, simulate_trade
from .collector import _atomic_write_json
from .schemas import contract_id as build_contract_id
from .schemas import parse_expiration

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("thetadata_pkg.bt3_b0_random_control")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"

# Read-only inputs -- BT-1's real 124-session backfill (never written to here).
BACKFILL_DIR = DATA / "backfill_60session"
BACKFILL_RAW_DIR = BACKFILL_DIR / "raw"
BACKFILL_MANIFEST_PATH = BACKFILL_DIR / "backfill_60session_manifest.json"

# B0's own isolated output directory.
BT3_B0_DIR = DATA / "bt3_b0_control"
BT3_B0_LEDGER_PATH = BT3_B0_DIR / "bt3_b0_control_ledger.json"
BT3_B0_REPORT_PATH = BT3_B0_DIR / "bt3_b0_control_report.json"
BT3_B0_SUMMARY_PATH = BT3_B0_DIR / "bt3_b0_control_summary.md"

# Fixed so a re-run reproduces the exact same 124 draws (RANDOM_SEED) and the
# exact same bootstrap resamples (BOOTSTRAP_SEED) -- two separate constants
# so changing one never silently perturbs the other.
RANDOM_SEED = 20260728
BOOTSTRAP_SEED = 20260729
N_BOOTSTRAP = 10000

# BT0_CHARTER.md Section 3's frozen session_window.
SESSION_WINDOW_START_ET = dt.time(10, 0)
SESSION_WINDOW_END_ET = dt.time(15, 30)

EXPERIMENT_ID = "bt3_b0_random_control"
LABEL_NULL_BASELINE = "B0_RANDOM_CONTROL_NULL_BASELINE_NOT_A_STRATEGY_RESULT"
FLAG_B0_RANDOM_CONTROL = LABEL_NULL_BASELINE

EXIT_REASON_NO_SIGNAL_TIME = "NO_ELIGIBLE_TIMESTAMP"
DATA_QUALITY_NO_ELIGIBLE_TIMESTAMP = "NO_ELIGIBLE_TIMESTAMP_IN_WINDOW"

# Held identical to a real experiment's defaults, imported and passed
# explicitly (not implicitly relied on) so this module's code is itself the
# audit trail for "nothing about the strategy machinery was changed."
SELECTOR_CONFIG = SelectorConfig()
FILL_CONFIG = FillConfig()
EXIT_CONFIG = ExitConfig()


# --- Session list -----------------------------------------------------

def list_b0_sessions(manifest_path: Path = BACKFILL_MANIFEST_PATH) -> list[dict]:
    """Every (symbol, date) unit in the real BT-1 backfill manifest,
    sorted (date, symbol) for a fixed, documented draw order. Includes
    PASS and PARTIAL sessions alike (the manifest's own overall.usable_for_bt2
    already gated the backfill as a whole; B0 draws from all 124 units per
    the task's own instruction, not a further-filtered subset)."""
    payload = json.loads(Path(manifest_path).read_text())
    sessions = payload.get("sessions", [])
    out = [
        {"symbol": s["symbol"], "date": s["date"], "quality_grade": s.get("quality_grade")}
        for s in sessions
    ]
    out.sort(key=lambda s: (s["date"], s["symbol"]))
    return out


# --- Random draw (pure, injectable I/O) --------------------------------

def session_window_bounds(session_date: dt.date) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(dt.datetime.combine(session_date, SESSION_WINDOW_START_ET), tz=ET)
    end = pd.Timestamp(dt.datetime.combine(session_date, SESSION_WINDOW_END_ET), tz=ET)
    return start, end


def sample_decision_ts(timestamps, session_date: dt.date, rng: np.random.Generator) -> Optional[pd.Timestamp]:
    """Draws a uniformly random instant within the frozen 10:00-15:30 ET
    window, then snaps to the NEAREST real timestamp actually present in
    `timestamps` (any contract's trade tick for that session/symbol) --
    never fabricates a decision_ts that has no underlying data behind it.
    Returns None (a real, reportable outcome, not silently skipped) if
    `timestamps` has nothing inside the window at all."""
    window_start, window_end = session_window_bounds(session_date)
    if timestamps is None:
        return None
    ts = timestamps if isinstance(timestamps, pd.Series) else pd.Series(list(timestamps))
    if ts.empty:
        return None
    ts = pd.to_datetime(ts)
    in_window = ts[(ts >= window_start) & (ts < window_end)]
    if in_window.empty:
        return None

    draw_frac = float(rng.uniform(0.0, 1.0))
    target = window_start + draw_frac * (window_end - window_start)

    sorted_ts = in_window.sort_values().reset_index(drop=True)
    idx = sorted_ts.searchsorted(target)
    candidates = []
    if idx < len(sorted_ts):
        candidates.append(sorted_ts.iloc[idx])
    if idx > 0:
        candidates.append(sorted_ts.iloc[idx - 1])
    return min(candidates, key=lambda t: abs((t - target).total_seconds()))


def sample_direction(rng: np.random.Generator) -> str:
    return VALID_DIRECTION_GATES[int(rng.integers(0, len(VALID_DIRECTION_GATES)))]


def generate_b0_signals(sessions: list[dict], timestamp_loader, seed: int = RANDOM_SEED) -> list[dict]:
    """sessions: list_b0_sessions()'s output (or a test fixture in the same
    shape), consumed in the given order -- caller owns sort order.
    timestamp_loader(symbol, date) -> real trade timestamps for that
    session/symbol, injected so this function never touches disk itself and
    can be fully exercised with synthetic fixtures in tests.

    One rng draw sequence per call: for every session, in order, draw the
    time-fraction float first, then the direction index -- always in that
    order, always exactly once per session regardless of whether that
    session ends up with a usable timestamp, so the same (sessions, seed)
    input always reproduces the exact same output."""
    rng = np.random.default_rng(seed)
    signals = []
    for s in sessions:
        symbol, date = s["symbol"], s["date"]
        session_date = dt.date.fromisoformat(date)
        timestamps = timestamp_loader(symbol, date)
        decision_ts = sample_decision_ts(timestamps, session_date, rng)
        direction = sample_direction(rng)
        signals.append({
            "symbol": symbol, "session": date, "decision_ts": decision_ts,
            "direction": direction, "data_gap": decision_ts is None,
        })
    return signals


# --- Real (I/O) data loaders --------------------------------------------

def _session_files(symbol: str, date: str, raw_dir: Path) -> list[Path]:
    return sorted((raw_dir / symbol / date).glob("part-*.parquet"))


def _load_session_trade_timestamps(symbol: str, date: str, raw_dir: Path = BACKFILL_RAW_DIR) -> pd.Series:
    """Column-projected read (trade_timestamp only) -- used during the
    up-front signal-generation scan across all 124 sessions, kept cheap on
    a single-core/1.9GB box by never pulling the other 28 columns for a
    pass that only needs timestamps."""
    files = _session_files(symbol, date, raw_dir)
    if not files:
        return pd.Series([], dtype="datetime64[ns]")
    parts = [pd.read_parquet(f, columns=["trade_timestamp"])["trade_timestamp"] for f in files]
    return pd.concat(parts, ignore_index=True)


def _load_raw_session_trades(symbol: str, date: str, raw_dir: Path = BACKFILL_RAW_DIR) -> pd.DataFrame:
    """Full classified-trade columns -- loaded only once a session has
    actually been selected for simulation (never during the up-front scan),
    and only ever one session at a time (the caller discards it before
    moving to the next), keeping peak memory well under this box's limits
    even though the full raw/ directory is ~3.2GB on disk."""
    files = _session_files(symbol, date, raw_dir)
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def _load_session_greeks(symbol: str, date: str, trades_df: pd.DataFrame, backfill_dir: Path = BACKFILL_DIR) -> dict:
    """Maps this session's EOD greeks pull (iv_{symbol}_{date}.json) to
    {contract_id: {"delta": ...}} for bt2_selector's delta gate. See this
    module's docstring for the EOD-vs-point-in-time caveat this inherits
    from backfill_60session.py -- not something introduced here."""
    path = backfill_dir / f"iv_{symbol}_{date}.json"
    if not path.exists() or trades_df is None or trades_df.empty:
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    rows = payload.get("rows", [])
    if not rows:
        return {}
    exp_values = trades_df["expiration"].dropna().unique()
    if len(exp_values) == 0:
        return {}
    exp_date = parse_expiration(exp_values[0])
    greeks: dict = {}
    for row in rows:
        try:
            cid = build_contract_id(symbol, exp_date, row["strike"], row["right"])
        except Exception:
            continue
        greeks[cid] = {"delta": row.get("delta")}
    return greeks


# --- Feed one signal through the REAL BT-2 pipeline ---------------------

def simulate_b0_signal(sig: dict, trades_df: pd.DataFrame, greeks: dict) -> dict:
    """Calls bt2_simulator.simulate_trade() exactly as a real signal would
    -- no bypass, no reimplementation. underlying_bars is always an empty
    frame: B0 has no invalidation_level (there is no real indicator to
    derive one from), so resolve_exit's invalidation leg is correctly
    disabled by construction, matching NO_CONTRACT/NO_FILL tests in
    test_bt2_simulator.py's own use of pd.DataFrame() for the same param.

    A session/symbol with no eligible timestamp in the window (sig
    decision_ts is None) never reaches simulate_trade -- there's no
    decision_ts to hand it -- and is instead recorded as its own honest
    ledger outcome (EXIT_REASON_NO_SIGNAL_TIME), never silently dropped."""
    context_snapshot_id = f"B0-RANDOM-CONTROL:{sig['symbol']}:{sig['session']}"

    if sig["decision_ts"] is None:
        return build_ledger_row(
            trade_id=str(uuid.uuid4()), session=sig["session"], symbol=sig["symbol"],
            signal_ts=None, decision_ts=None, contract_id=None, contract_gate=GATE_FAIL,
            entry_quote_ts=[], entry_bid=[], entry_ask=[], entry_fill=[], quantity=[],
            context_snapshot_id=context_snapshot_id,
            target=None, stop=None, invalidation=None,
            exit_ts=None, exit_bid=[], exit_ask=[], exit_fill=[], exit_reason=EXIT_REASON_NO_SIGNAL_TIME,
            gross_pnl=None, fees=0.0, slippage=None, net_pnl=None, mae=None, mfe=None,
            rule_flags=[FLAG_B0_RANDOM_CONTROL, "NO_ELIGIBLE_TIMESTAMP_IN_SESSION_WINDOW"],
            data_quality=DATA_QUALITY_NO_ELIGIBLE_TIMESTAMP, experiment_id=EXPERIMENT_ID,
        )

    inputs = TradeInputs(
        session=sig["session"], symbol=sig["symbol"],
        signal_ts=sig["decision_ts"], decision_ts=sig["decision_ts"],
        direction=sig["direction"], context_snapshot_id=context_snapshot_id,
        experiment_id=EXPERIMENT_ID, quantity=1, invalidation_level=None,
    )
    row = simulate_trade(
        inputs, trades_df, pd.DataFrame(), greeks=greeks,
        selector_config=SELECTOR_CONFIG, fill_config=FILL_CONFIG, exit_config=EXIT_CONFIG,
    )
    row["rule_flags"] = list(row["rule_flags"]) + [FLAG_B0_RANDOM_CONTROL]
    return row


# --- Report math ---------------------------------------------------------

def bootstrap_mean_ci(
    values: list, n_bootstrap: int = N_BOOTSTRAP, seed: int = BOOTSTRAP_SEED, alpha: float = 0.05,
) -> Optional[dict]:
    """Session-level (here: trade-level, since B0 draws exactly one trade
    per session-symbol unit -- BT0_CHARTER.md Section 4's own convention,
    simplified exactly the way the task anticipated) percentile bootstrap
    on the mean. None (not a fabricated interval) below n=2."""
    if values is None or len(values) < 2:
        return None
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_bootstrap, n))
    means = arr[idx].mean(axis=1)
    lower = float(np.percentile(means, 100 * alpha / 2))
    upper = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return {
        "point_estimate": round(float(arr.mean()), 4), "lower": round(lower, 4), "upper": round(upper, 4),
        "alpha": alpha, "n": n, "n_bootstrap": n_bootstrap, "seed": seed,
    }


def _profit_factor(net_pnls: list) -> Optional[float]:
    wins = sum(p for p in net_pnls if p > 0)
    losses = sum(p for p in net_pnls if p < 0)
    if losses == 0:
        return None  # undefined (no losing trades) -- never silently reported as inf
    return round(wins / abs(losses), 4)


def summarize_b0(rows: list) -> dict:
    """Honest B0 summary math. Deliberately does NOT apply BT0_CHARTER.md
    Section 5's pass/fail success criteria (minimum-sample / positive-CI-
    lower-bound / five-session-robustness) -- those are written for judging
    a real strategy (B6), and applying them to a random control would be a
    category error (there is no promotion decision to gate here). This
    function only computes and reports the numbers."""
    n_total = len(rows)
    no_time = [r for r in rows if r.get("exit_reason") == EXIT_REASON_NO_SIGNAL_TIME]
    no_contract = [r for r in rows if r.get("exit_reason") == "NO_CONTRACT"]
    no_fill = [r for r in rows if r.get("contract_gate") == GATE_PASS and r.get("net_pnl") is None]
    filled = [r for r in rows if r.get("net_pnl") is not None]

    net_pnls = [float(r["net_pnl"]) for r in filled]
    wins = [p for p in net_pnls if p > 0]
    friction_shares = [
        r["friction_detail"]["friction_share_of_target"]
        for r in filled
        if r.get("friction_detail") and r["friction_detail"].get("friction_share_of_target") is not None
    ]

    net_expectancy = float(np.mean(net_pnls)) if net_pnls else None
    win_rate = (len(wins) / len(filled)) if filled else None
    profit_factor = _profit_factor(net_pnls) if net_pnls else None
    avg_friction_share = float(np.mean(friction_shares)) if friction_shares else None
    ci = bootstrap_mean_ci(net_pnls) if len(net_pnls) >= 2 else None

    all_as_zero = [(float(r["net_pnl"]) if r.get("net_pnl") is not None else 0.0) for r in rows]
    expectancy_all_draws = float(np.mean(all_as_zero)) if all_as_zero else None

    return {
        "label": LABEL_NULL_BASELINE,
        "n_total_draws": n_total,
        "n_no_eligible_timestamp": len(no_time),
        "n_no_contract": len(no_contract),
        "n_contract_pass_no_fill": len(no_fill),
        "n_filled": len(filled),
        "fill_rate_of_total_draws": (len(filled) / n_total) if n_total else None,
        "net_expectancy_per_filled_trade": round(net_expectancy, 2) if net_expectancy is not None else None,
        "net_expectancy_bootstrap_95ci": ci,
        "profit_factor": profit_factor,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_friction_share_of_target": round(avg_friction_share, 4) if avg_friction_share is not None else None,
        "n_friction_share_observations": len(friction_shares),
        "expectancy_all_draws_including_no_trade_as_zero": (
            round(expectancy_all_draws, 2) if expectancy_all_draws is not None else None
        ),
        "bootstrap_method": (
            "percentile bootstrap over filled trades' net_pnl "
            "(1 trade per session-symbol unit in B0, so session-level == trade-level here)"
        ),
        "n_bootstrap": N_BOOTSTRAP,
        "bootstrap_seed": BOOTSTRAP_SEED,
    }


def render_summary_md(summary: dict) -> str:
    ci = summary.get("net_expectancy_bootstrap_95ci")
    ci_str = f"${ci['lower']:.2f} to ${ci['upper']:.2f}" if ci else "N/A (fewer than 2 filled trades)"
    pf = summary.get("profit_factor")
    if pf is not None:
        pf_str = f"{pf:.2f}"
    elif summary.get("n_filled", 0) > 0:
        pf_str = "undefined (no losing trades in the sample)"
    else:
        pf_str = "N/A (no filled trades)"
    exp = summary.get("net_expectancy_per_filled_trade")
    exp_str = f"${exp:.2f}" if exp is not None else "N/A (no filled trades)"
    wr = summary.get("win_rate")
    wr_str = f"{wr:.1%}" if wr is not None else "N/A"
    fr = summary.get("avg_friction_share_of_target")
    fr_str = f"{fr:.1%} of planned gross target (n={summary.get('n_friction_share_observations', 0)})" if fr is not None else "N/A"
    fill_rate = summary.get("fill_rate_of_total_draws")
    fill_rate_str = f"{fill_rate:.1%}" if fill_rate is not None else "N/A"
    all_exp = summary.get("expectancy_all_draws_including_no_trade_as_zero")
    all_exp_str = f"${all_exp:.2f}" if all_exp is not None else "N/A"

    return "\n".join([
        "# BT-3 B0 -- Random-Time/Random-Direction Control Baseline",
        "",
        "**THIS IS A NULL-HYPOTHESIS REFERENCE POINT, NOT A STRATEGY RESULT.**",
        "Same symbols (SPY/QQQ), session window (10:00-15:30 ET), BT-2 selector/fill/exit",
        "engine, and quantity=1 sizing as any real signal-driven experiment would use. The",
        "ONLY randomized inputs are entry time and CALL/PUT direction. This exists so a",
        "future real signal (B1 onward) has an honest \"is this just market drift\" number",
        "to beat -- BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf Section 9.",
        "",
        "## Sample",
        f"- Total random draws: {summary['n_total_draws']} (124 expected: 62 SPY + 62 QQQ sessions)",
        f"- No eligible timestamp in the 10:00-15:30 ET window: {summary['n_no_eligible_timestamp']}",
        f"- No contract passed the selector gate (NO CONTRACT): {summary['n_no_contract']}",
        f"- Contract passed the gate but never filled (NO FILL): {summary['n_contract_pass_no_fill']}",
        f"- Filled trades: {summary['n_filled']}",
        f"- Fill rate (of all {summary['n_total_draws']} draws): {fill_rate_str}",
        "",
        "## Result (filled trades only)",
        f"- Net expectancy per filled trade: {exp_str}",
        f"- Session-level bootstrap 95% CI on net expectancy: {ci_str}",
        f"- Profit factor: {pf_str}",
        f"- Win rate: {wr_str}",
        f"- Avg friction share of planned target: {fr_str}",
        "",
        "## Supplementary (all draws, no-trade counted as $0)",
        f"- Expectancy across all {summary['n_total_draws']} draws: {all_exp_str}",
        "",
        "Per BT0_CHARTER.md Section 5, this run is NOT evaluated against the success",
        "criteria there (minimum sample / positive CI lower bound / five-session robustness)",
        "-- those apply to a real strategy result (B6), not a random control. Numbers above",
        "are reported as-is, honestly, with no pass/fail verdict attached.",
    ])


# --- Orchestration (real I/O) --------------------------------------------

def run_b0(
    manifest_path: Path = BACKFILL_MANIFEST_PATH,
    raw_dir: Path = BACKFILL_RAW_DIR,
    backfill_dir: Path = BACKFILL_DIR,
    ledger_path: Path = BT3_B0_LEDGER_PATH,
    seed: int = RANDOM_SEED,
) -> list:
    sessions = list_b0_sessions(manifest_path)
    logger.info("B0 random control: %d sessions loaded from manifest", len(sessions))

    def _ts_loader(symbol, date):
        return _load_session_trade_timestamps(symbol, date, raw_dir)

    signals = generate_b0_signals(sessions, _ts_loader, seed=seed)

    rows = []
    for sig in signals:
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning(
                "B0 run aborted early at %s %s: only %.0fMB RAM free (< %dMB floor) -- "
                "resumable, re-run once load subsides", sig["symbol"], sig["session"], free_mb, MIN_FREE_RAM_MB,
            )
            break
        if sig["data_gap"]:
            rows.append(simulate_b0_signal(sig, pd.DataFrame(), {}))
            continue
        trades_df = _load_raw_session_trades(sig["symbol"], sig["session"], raw_dir)
        greeks = _load_session_greeks(sig["symbol"], sig["session"], trades_df, backfill_dir)
        rows.append(simulate_b0_signal(sig, trades_df, greeks))
        del trades_df

    append_ledger_rows(rows, path=ledger_path)
    logger.info("B0 run complete: %d ledger rows written to %s", len(rows), ledger_path)
    return rows


def write_report(
    rows: list, report_path: Path = BT3_B0_REPORT_PATH, summary_path: Path = BT3_B0_SUMMARY_PATH,
) -> dict:
    summary = summarize_b0(rows)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": "B0 RANDOM-TIME/RANDOM-DIRECTION CONTROL -- NULL-HYPOTHESIS REFERENCE, NOT A STRATEGY RESULT",
        "roadmap_reference": "BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf Section 9, B0",
        "random_seed": RANDOM_SEED,
        "strategy_parameters_held_constant": {
            "symbols": ["SPY", "QQQ"],
            "session_window_et": [SESSION_WINDOW_START_ET.isoformat(), SESSION_WINDOW_END_ET.isoformat()],
            "quantity": 1,
            "fill_model": FILL_CONFIG.fill_model,
            "selector": {
                "allowed_dte": sorted(SELECTOR_CONFIG.allowed_dte),
                "premium_band": [SELECTOR_CONFIG.premium_low, SELECTOR_CONFIG.premium_high],
                "min_abs_delta": SELECTOR_CONFIG.min_abs_delta,
                "target_delta": SELECTOR_CONFIG.target_delta,
            },
            "exit": {
                "target_return": EXIT_CONFIG.target_return,
                "premium_stop_pct": EXIT_CONFIG.premium_stop_pct,
                "time_stop_minutes": EXIT_CONFIG.time_stop_minutes,
                "forced_close_time": EXIT_CONFIG.forced_close_time.isoformat(),
            },
        },
        "data_limitations": [
            "greeks are an EOD-only snapshot (option_history_greeks_eod), not true "
            "point-in-time -- inherited from backfill_60session.py, applies to any BT-2 "
            "run against this dataset, not introduced by B0",
            "underlying 1-min bars were never persisted by the backfill -- irrelevant to "
            "B0 specifically since there is no invalidation_level for a random draw",
        ],
        "summary": summary,
    }
    _atomic_write_json(report_path, payload)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(render_summary_md(summary))
    logger.info("B0 report written to %s and %s", report_path, summary_path)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result_rows = run_b0()
    result_summary = write_report(result_rows)
    print(json.dumps(result_summary, indent=2, default=str))
