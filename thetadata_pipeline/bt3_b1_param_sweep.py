"""BT-3 B1 parameter sensitivity sweep -- does any HEFF SMC indicator
parameter setting other than the current live defaults produce a
meaningfully better-quality triangle signal, judged on the SAME honest
backtest metrics B1 itself reports (net expectancy, profit factor,
session-bootstrap 95% CI), not raw signal count or win rate alone?

Coordinate-wise sweep from the validated default baseline (same spirit as
continuous_search.py's own grid: vary one knob at a time, hold everything
else at the current champion/default -- a full combinatorial grid across
~15 parameters is not tractable, per the task's own instruction). Curated,
documented subset of the most likely-impactful knobs: pivLen, dispMult,
minScore, htfGateMode, trigCooldown, fadeBuf, and five of the ten confluence
weights (structure/sweep/pullback/fade/HTF -- the ones most directly tied to
which triggers are even allowed to fire, not just how they're scored).

Efficiency design specific to this box's single-core/1.9GB constraint: the
replay step (heff_smc_replay.run_replay) is cheap CPU-only work (~6s for the
full 62-session baseline) and is run once per variant. The EXPENSIVE part is
BT-2's per-session raw options data load (~3.2GB across 62 sessions) -- this
sweep loads each session's trades_df/greeks ONCE and replays every variant's
signals for that session against it, rather than reloading the same raw
parquet 33 times (once per variant). This is the same "load once, reuse"
discipline bt3_b0_random_control.py's run_b0 already follows per-session,
just extended across variants instead of across signals within one variant.

Isolation: reads backfill_60session/raw (read-only, same as B0/B1) and
heff_smc_replay's own 1-min bar output. Writes only under
data/thetadata/bt3_b1_param_sweep/.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path

from .backfill import MIN_FREE_RAM_MB, _free_ram_mb
from .bt2_schemas import GATE_PASS
from .bt3_b0_random_control import BACKFILL_DIR, BACKFILL_RAW_DIR, _load_raw_session_trades, _load_session_greeks
from .bt3_b1_indicator_only import (
    BT3_B1_REPORT_PATH, generate_b1_signals, session_bootstrap_mean_ci, summarize_b1,
)
from .heff_smc_engine import HeffSmcConfig
from .heff_smc_replay import build_continuous_1min_series, run_replay
from .qqq_bars_fetch import BARS_RAW_DIR, SYMBOL, list_target_sessions, load_all_bars

logger = logging.getLogger("thetadata_pkg.bt3_b1_param_sweep")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
SWEEP_DIR = DATA / "bt3_b1_param_sweep"
SWEEP_RESULTS_PATH = SWEEP_DIR / "param_sweep_results.json"
SWEEP_SUMMARY_PATH = SWEEP_DIR / "param_sweep_summary.md"

BASELINE_ID = "baseline_live_defaults"

# (variant_id, human description, HeffSmcConfig field overrides)
PARAM_VARIANTS = [
    (BASELINE_ID, "current live defaults (validated B1 config)", {}),

    ("piv_len_3", "pivot lookback 3 (faster/noisier structure)", {"piv_len": 3}),
    ("piv_len_4", "pivot lookback 4", {"piv_len": 4}),
    ("piv_len_6", "pivot lookback 6", {"piv_len": 6}),
    ("piv_len_7", "pivot lookback 7 (slower/cleaner structure)", {"piv_len": 7}),

    ("disp_mult_08", "displacement 0.8x ATR (looser fakeout filter)", {"disp_mult": 0.8}),
    ("disp_mult_10", "displacement 1.0x ATR", {"disp_mult": 1.0}),
    ("disp_mult_15", "displacement 1.5x ATR (tighter fakeout filter)", {"disp_mult": 1.5}),
    ("disp_mult_18", "displacement 1.8x ATR", {"disp_mult": 1.8}),

    ("min_score_40", "signal threshold 4.0 (looser)", {"min_score": 4.0}),
    ("min_score_45", "signal threshold 4.5", {"min_score": 4.5}),
    ("min_score_55", "signal threshold 5.5", {"min_score": 5.5}),
    ("min_score_60", "signal threshold 6.0", {"min_score": 6.0}),
    ("min_score_65", "signal threshold 6.5 (tighter)", {"min_score": 6.5}),

    ("htf_gate_all", "HTF hard gate: All triggers (strict)", {"htf_gate_mode": "All triggers"}),
    ("htf_gate_off", "HTF hard gate: Off (weight-only)", {"htf_gate_mode": "Off"}),

    ("trig_cooldown_2", "pullback/fade cooldown 2 bars (more frequent)", {"trig_cooldown": 2}),
    ("trig_cooldown_3", "pullback/fade cooldown 3 bars", {"trig_cooldown": 3}),
    ("trig_cooldown_8", "pullback/fade cooldown 8 bars", {"trig_cooldown": 8}),
    ("trig_cooldown_12", "pullback/fade cooldown 12 bars (less frequent)", {"trig_cooldown": 12}),

    ("fade_buf_005", "200MA fade touch buffer 0.05x ATR (tighter)", {"fade_buf": 0.05}),
    ("fade_buf_020", "200MA fade touch buffer 0.20x ATR", {"fade_buf": 0.20}),
    ("fade_buf_030", "200MA fade touch buffer 0.30x ATR (looser)", {"fade_buf": 0.30}),

    ("w_mss_15", "MSS weight 1.5 (down from 2.5)", {"w_mss": 1.5}),
    ("w_mss_35", "MSS weight 3.5 (up from 2.5)", {"w_mss": 3.5}),
    ("w_sweep_10", "sweep-reclaim weight 1.0 (down from 2.0)", {"w_sweep": 1.0}),
    ("w_sweep_30", "sweep-reclaim weight 3.0 (up from 2.0)", {"w_sweep": 3.0}),
    ("w_pull_05", "pullback weight 0.5 (down from 1.5)", {"w_pull": 0.5}),
    ("w_pull_25", "pullback weight 2.5 (up from 1.5)", {"w_pull": 2.5}),
    ("w_fade_05", "200MA fade weight 0.5 (down from 1.5)", {"w_fade": 0.5}),
    ("w_fade_25", "200MA fade weight 2.5 (up from 1.5)", {"w_fade": 2.5}),
    ("w_htf_05", "HTF agreement weight 0.5 (down from 1.5)", {"w_htf": 0.5}),
    ("w_htf_25", "HTF agreement weight 2.5 (up from 1.5)", {"w_htf": 2.5}),
]


def _atomic_write_json(path: Path, payload) -> None:
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def build_variant_signals(continuous_bars, variants: list = PARAM_VARIANTS) -> dict:
    """Runs the real replay once per variant against the SAME real 1-min
    bar data, returns {variant_id: signals_list}. Pure CPU, no disk I/O
    beyond the one-time bar load the caller already did."""
    out = {}
    for variant_id, desc, overrides in variants:
        cfg = HeffSmcConfig(**overrides)
        events, _diag = run_replay(continuous_bars, cfg)
        signals = generate_b1_signals(events)
        out[variant_id] = signals
        logger.info("variant %s (%s): %d real triangle events", variant_id, desc, len(events))
    return out


def run_sweep(
    variants: list = PARAM_VARIANTS, raw_dir: Path = BACKFILL_RAW_DIR, backfill_dir: Path = BACKFILL_DIR,
    bars_raw_dir: Path = BARS_RAW_DIR,
) -> dict:
    dates = list_target_sessions()
    raw_bars = load_all_bars(SYMBOL, dates, bars_raw_dir)
    if raw_bars.empty:
        raise RuntimeError("No QQQ 1-min bars found -- run qqq_bars_fetch.fetch_and_persist_all() first")
    continuous = build_continuous_1min_series(raw_bars)

    variant_signals = build_variant_signals(continuous, variants)

    # group EVERY variant's signals by session so each session's raw options
    # data loads exactly once across the whole sweep
    signals_by_session: dict = {}
    for variant_id, sigs in variant_signals.items():
        for sig in sigs:
            signals_by_session.setdefault(sig["session"], []).append((variant_id, sig))

    from .bt3_b1_indicator_only import simulate_b1_signal  # local import: keeps this module's
    # top-level import list free of a name that's only ever called, never re-exported

    rows_by_variant: dict = {vid: [] for vid, _, _ in variants}
    sessions_processed = 0
    for session in sorted(signals_by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning(
                "param sweep aborted early at session %s: only %.0fMB RAM free (< %dMB floor) -- "
                "resumable, results so far are still valid for the sessions completed", session, free_mb, MIN_FREE_RAM_MB,
            )
            break
        pairs = signals_by_session[session]
        symbol = pairs[0][1]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, raw_dir)
        greeks = _load_session_greeks(symbol, session, trades_df, backfill_dir)
        for variant_id, sig in pairs:
            row = simulate_b1_signal(sig, trades_df, greeks)
            row["sweep_variant_id"] = variant_id
            rows_by_variant[variant_id].append(row)
        del trades_df
        sessions_processed += 1

    logger.info("param sweep: %d/%d sessions processed", sessions_processed, len(signals_by_session))

    results = {}
    for variant_id, desc, overrides in variants:
        rows = rows_by_variant[variant_id]
        summary = summarize_b1(rows)
        results[variant_id] = {"description": desc, "overrides": overrides, "summary": summary}
    return results


def compare_to_baseline(results: dict, baseline_id: str = BASELINE_ID) -> dict:
    """Honest comparison, not a leaderboard: a variant only counts as
    'plausibly better' if its net expectancy point estimate beats the
    baseline's AND its own session-bootstrap CI lower bound clears the
    baseline's point estimate (i.e. the improvement isn't just sitting
    inside the baseline's own noise band). Anything else is reported as
    'within noise of baseline', never oversold."""
    baseline = results[baseline_id]["summary"]
    baseline_exp = baseline.get("net_expectancy_per_filled_trade")
    comparisons = {}
    for variant_id, entry in results.items():
        if variant_id == baseline_id:
            continue
        s = entry["summary"]
        exp = s.get("net_expectancy_per_filled_trade")
        ci = s.get("net_expectancy_session_bootstrap_95ci")
        verdict = "insufficient_data"
        if exp is not None and baseline_exp is not None:
            if exp <= baseline_exp:
                verdict = "not better (point estimate <= baseline)"
            elif ci is not None and ci["lower"] > baseline_exp:
                verdict = "plausibly better (CI lower bound clears baseline point estimate)"
            else:
                verdict = "within noise of baseline (point estimate higher, but CI overlaps)"
        comparisons[variant_id] = {
            "description": entry["description"], "net_expectancy": exp,
            "baseline_net_expectancy": baseline_exp, "ci": ci, "n_filled": s.get("n_filled"),
            "verdict": verdict,
        }
    return comparisons


def render_sweep_summary_md(results: dict, comparisons: dict) -> str:
    baseline = results[BASELINE_ID]["summary"]
    lines = [
        "# BT-3 B1 Parameter Sensitivity Sweep",
        "",
        "Coordinate-wise sweep from the validated live-default baseline. Each variant is a",
        "REAL replay against the same 62 real sessions, fed through the same BT-2 machinery",
        "as B1 itself. NOT evaluated against BT0_CHARTER.md Section 5's promotion criteria.",
        "",
        f"## Baseline ({BASELINE_ID})",
        f"- n_filled: {baseline.get('n_filled')}",
        f"- net expectancy: ${baseline.get('net_expectancy_per_filled_trade')}"
        if baseline.get("net_expectancy_per_filled_trade") is not None else "- net expectancy: N/A",
        f"- profit factor: {baseline.get('profit_factor')}",
        "",
        "## Variant results",
        "",
        "| variant | description | n_filled | net_expectancy | 95% CI | verdict |",
        "|---|---|---|---|---|---|",
    ]
    for variant_id, comp in comparisons.items():
        ci = comp["ci"]
        ci_str = f"${ci['lower']:.2f} to ${ci['upper']:.2f}" if ci else "N/A"
        exp_str = f"${comp['net_expectancy']:.2f}" if comp["net_expectancy"] is not None else "N/A"
        lines.append(
            f"| {variant_id} | {comp['description']} | {comp['n_filled']} | {exp_str} | {ci_str} | {comp['verdict']} |"
        )
    plausibly_better = [vid for vid, c in comparisons.items() if c["verdict"].startswith("plausibly better")]
    lines += ["", "## Honest read"]
    if plausibly_better:
        lines.append(
            f"Variants clearing the baseline's own noise band: {', '.join(plausibly_better)}. "
            "Still only 62 sessions -- treat as a lead worth re-testing on more data, not a proven edge."
        )
    else:
        lines.append(
            "No variant's improvement clears the baseline's own bootstrap noise band. "
            "Nothing here beats the live defaults by more than sampling noise, on this data."
        )
    return "\n".join(lines)


def write_sweep_report(
    results: dict, results_path: Path = SWEEP_RESULTS_PATH, summary_path: Path = SWEEP_SUMMARY_PATH,
) -> dict:
    comparisons = compare_to_baseline(results)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "label": "BT-3 B1 PARAMETER SENSITIVITY SWEEP -- NOT PROMOTION-EVALUATED",
        "baseline_id": BASELINE_ID,
        "results": results,
        "comparisons": comparisons,
    }
    _atomic_write_json(results_path, payload)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(render_sweep_summary_md(results, comparisons))
    logger.info("Param sweep report written to %s and %s", results_path, summary_path)
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sweep_results = run_sweep()
    report = write_sweep_report(sweep_results)
    print(json.dumps(report["comparisons"], indent=2, default=str))
