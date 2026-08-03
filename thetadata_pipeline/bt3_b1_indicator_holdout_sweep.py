"""BT-3 B1 indicator-parameter HOLDOUT sweep (heff's 2026-07-30 ask).

heff wants to know if any HEFF SMC indicator parameter setting finds
genuinely better entries than the live defaults -- and, if one does, whether
it's still better when combined with the moderate_combo selector config
chosen from the 2026-07-30 selector sweep. The earlier param sweep (30
variants, 62 sessions) found nothing beat baseline beyond noise and was
deliberately not re-run casually to avoid the appearance of fishing after a
strong B1 number. heff explicitly asked for the disciplined version this
time: a real out-of-sample holdout, not just picking whichever variant looks
best on the same data it was chosen from.

DESIGN

Chronological split, not random: the most recent HOLDOUT_N sessions are held
out untouched; everything older is the "dev" set used to pick a candidate.
Tuning on the past and checking the future mirrors real deployment and avoids
serial-correlation leakage between adjacent sessions that a random split
would risk.

CRITICAL correctness detail: heff_smc_replay.run_replay carries continuous
indicator state (structure/FVG/OB/liquidity pools/HTF bias) across session
boundaries, matching the live Pine script's forever-`var` state, which never
resets intraday-to-intraday. Splitting the underlying 1-min BARS into
dev/holdout and replaying each independently would give the holdout period a
false cold start with none of the state it would actually have accumulated
by that point live. Fixed correctly here: ONE continuous replay per variant
across the full 162-session bar series (real state, unmodified), then the
resulting SIGNALS are partitioned by session date into dev/holdout buckets.
Only the evaluation window is split -- never the replay input.

PRE-REGISTERED, NOT POST-HOC

- Reuses bt3_b1_param_sweep.PARAM_VARIANTS verbatim: the same 30 variants
  already tried once, in the same order. No new variant is added after
  seeing any result from this run.
- Reuses bt3_b1_param_sweep.compare_to_baseline's own champion bar
  unmodified: a variant only counts as a dev-set candidate if its net
  expectancy point estimate beats baseline's AND its own session-bootstrap
  CI lower bound clears baseline's point estimate (not just a higher
  number). This bar was written before this run existed; not adjusted here.
- The SAME bar, freshly computed against the holdout-only baseline, is what
  decides whether a dev-set candidate actually validates.

PHASES (each gated on the previous; stops honestly if nothing clears the bar)

  A. Dev sweep: all 30 variants, DEFAULT SelectorConfig, dev-set sessions
     only. Isolates the indicator's own effect (selector held constant).
  B. Holdout validation: ONLY variants that cleared Phase A's bar get
     re-evaluated on the untouched holdout sessions, against a holdout-only
     baseline. Reports plainly whether the improvement replicates, shrinks,
     or vanishes -- regardless of outcome.
  C. Combination: ONLY if a champion survives Phase B does its full
     162-session (dev+holdout) signal set get re-run through the
     moderate_combo SelectorConfig (heff's 2026-07-30 choice from the
     selector sweep) to see if indicator-improvement and selector-loosening
     compound. This keeps the search space additive (one validated change x
     one already-known selector option), not a blind cross-product of two
     unvalidated sweeps.

Isolation: reads backfill_60session/raw and qqq_1min_bars (read-only, same
as every other BT-3 script). Writes only under
data/thetadata/bt3_b1_indicator_holdout_sweep/.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from thetadata_pipeline.backfill import MIN_FREE_RAM_MB, _free_ram_mb
from thetadata_pipeline.bt2_selector import SelectorConfig
from thetadata_pipeline.bt3_b0_random_control import (
    BACKFILL_DIR, BACKFILL_RAW_DIR, _load_raw_session_trades, _load_session_greeks,
)
from thetadata_pipeline.bt3_b1_indicator_only import (
    generate_b1_signals, simulate_b1_signal, summarize_b1,
)
from thetadata_pipeline.bt3_b1_param_sweep import BASELINE_ID, PARAM_VARIANTS, compare_to_baseline
from thetadata_pipeline.bt3_b1_selector_sweep import simulate_variant_signal
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.heff_smc_replay import build_continuous_1min_series, run_replay
from thetadata_pipeline.qqq_bars_fetch import BARS_RAW_DIR, SYMBOL, list_target_sessions, load_all_bars

logger = logging.getLogger("thetadata_pkg.bt3_b1_indicator_holdout_sweep")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "thetadata" / "bt3_b1_indicator_holdout_sweep"
MARKER_PATH = OUT_DIR / "status.json"

HOLDOUT_N = 40  # most recent N sessions held out; rest ("dev") used to pick a candidate

# heff's 2026-07-30 selector choice, reused verbatim from bt3_b1_selector_sweep.VARIANTS
MODERATE_COMBO = SelectorConfig(min_abs_delta=0.10, premium_low=0.15, premium_high=0.40)


def write_marker(status: str, **kw) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), **kw}
    tmp = MARKER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(MARKER_PATH)
    logger.info("status -> %s %s", status, kw)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def build_all_variant_signals(continuous_bars) -> dict:
    """One continuous replay per variant across ALL sessions (real carried
    state) -- returns {variant_id: signals_list}. Splitting happens after
    this, on the signals' own session field, never on the bars."""
    out = {}
    for variant_id, desc, overrides in PARAM_VARIANTS:
        cfg = HeffSmcConfig(**overrides)
        events, _diag = run_replay(continuous_bars, cfg)
        signals = generate_b1_signals(events)
        out[variant_id] = signals
        logger.info("variant %s (%s): %d real triangle events (full 162-session replay)",
                    variant_id, desc, len(events))
    return out


def _simulate_bucket(
    variant_signals: dict, allowed_sessions: set, raw_dir: Path, backfill_dir: Path,
    selector_config: SelectorConfig = None,
) -> dict:
    """Filters every variant's signals to allowed_sessions, then simulates
    with one raw-data load per session shared across every variant that has
    a signal that session (same efficiency discipline as the original
    param sweep). selector_config=None uses simulate_b1_signal's own
    DEFAULT SelectorConfig (phases A/B); a real SelectorConfig routes
    through simulate_variant_signal instead (phase C only)."""
    signals_by_session: dict = {}
    for variant_id, sigs in variant_signals.items():
        for sig in sigs:
            if sig["session"] in allowed_sessions:
                signals_by_session.setdefault(sig["session"], []).append((variant_id, sig))

    rows_by_variant = {vid: [] for vid in variant_signals}
    for session in sorted(signals_by_session):
        free_mb = _free_ram_mb()
        if free_mb < MIN_FREE_RAM_MB:
            logger.warning("bucket sim aborted early at session %s: %.0fMB free RAM", session, free_mb)
            break
        pairs = signals_by_session[session]
        symbol = pairs[0][1]["symbol"]
        trades_df = _load_raw_session_trades(symbol, session, raw_dir)
        greeks = _load_session_greeks(symbol, session, trades_df, backfill_dir)
        for variant_id, sig in pairs:
            if selector_config is None:
                row = simulate_b1_signal(sig, trades_df, greeks)
            else:
                row = simulate_variant_signal(sig, trades_df, greeks, selector_config)
            rows_by_variant[variant_id].append(row)
        del trades_df
    return rows_by_variant


def summarize_variants(rows_by_variant: dict) -> dict:
    results = {}
    variant_meta = {vid: (desc, overrides) for vid, desc, overrides in PARAM_VARIANTS}
    for variant_id, rows in rows_by_variant.items():
        desc, overrides = variant_meta.get(variant_id, ("", {}))
        results[variant_id] = {"description": desc, "overrides": overrides, "summary": summarize_b1(rows)}
    return results


def main():
    logging.basicConfig(level=logging.INFO)
    write_marker("phase_a_dev_sweep_starting")

    all_dates = sorted(list_target_sessions())
    dev_dates = set(all_dates[:-HOLDOUT_N])
    holdout_dates = set(all_dates[-HOLDOUT_N:])
    logger.info("split: %d dev sessions (%s..%s), %d holdout sessions (%s..%s)",
                len(dev_dates), min(dev_dates), max(dev_dates),
                len(holdout_dates), min(holdout_dates), max(holdout_dates))

    raw_bars = load_all_bars(SYMBOL, all_dates, BARS_RAW_DIR)
    continuous = build_continuous_1min_series(raw_bars)

    logger.info("=== PHASE A: dev-set sweep (30 variants, default selector) ===")
    variant_signals = build_all_variant_signals(continuous)
    dev_rows = _simulate_bucket(variant_signals, dev_dates, BACKFILL_RAW_DIR, BACKFILL_DIR)
    dev_results = summarize_variants(dev_rows)
    dev_comparisons = compare_to_baseline(dev_results, baseline_id=BASELINE_ID)
    _write_json(OUT_DIR / "phase_a_dev_results.json", {
        "dev_sessions": sorted(dev_dates), "n_dev_sessions": len(dev_dates),
        "results": dev_results, "comparisons": dev_comparisons,
    })
    candidates = [vid for vid, c in dev_comparisons.items() if c["verdict"].startswith("plausibly better")]
    logger.info("Phase A complete. Dev-set candidates clearing the pre-registered bar: %s", candidates or "NONE")
    write_marker("phase_a_complete", n_dev_sessions=len(dev_dates), candidates=candidates)

    if not candidates:
        write_marker("stopped_no_candidate", n_dev_sessions=len(dev_dates),
                     reason="no variant's dev-set CI lower bound cleared baseline's dev-set point estimate")
        logger.info("No dev-set candidate cleared the bar. Stopping here -- same honest null-result "
                    "shape as the original 62-session sweep. No holdout check needed (nothing to "
                    "validate), no Phase C.")
        return

    logger.info("=== PHASE B: holdout validation for %s ===", candidates)
    holdout_variant_ids = candidates + [BASELINE_ID]
    holdout_signals_subset = {vid: variant_signals[vid] for vid in holdout_variant_ids}
    holdout_rows = _simulate_bucket(holdout_signals_subset, holdout_dates, BACKFILL_RAW_DIR, BACKFILL_DIR)
    holdout_results = summarize_variants(holdout_rows)
    holdout_comparisons = compare_to_baseline(holdout_results, baseline_id=BASELINE_ID)
    _write_json(OUT_DIR / "phase_b_holdout_results.json", {
        "holdout_sessions": sorted(holdout_dates), "n_holdout_sessions": len(holdout_dates),
        "dev_candidates_tested": candidates,
        "results": holdout_results, "comparisons": holdout_comparisons,
    })
    validated = [vid for vid, c in holdout_comparisons.items() if c["verdict"].startswith("plausibly better")]
    logger.info("Phase B complete. Candidates that ALSO clear the bar on untouched holdout data: %s",
                validated or "NONE")
    write_marker("phase_b_complete", candidates=candidates, validated=validated)

    if not validated:
        write_marker("stopped_not_validated", candidates=candidates,
                     reason="dev-set candidate(s) did not replicate on the untouched holdout sessions "
                            "-- consistent with a dev-set false positive, not a real edge")
        logger.info("No dev-set candidate replicated on holdout. Stopping here. No Phase C -- there is "
                    "no validated indicator champion to combine with the selector config.")
        return

    logger.info("=== PHASE C: combine validated champion(s) %s with moderate_combo selector ===", validated)
    champion_signals = {vid: variant_signals[vid] for vid in validated + [BASELINE_ID]}
    full_dates = dev_dates | holdout_dates
    combo_rows = _simulate_bucket(champion_signals, full_dates, BACKFILL_RAW_DIR, BACKFILL_DIR,
                                   selector_config=MODERATE_COMBO)
    combo_results = summarize_variants(combo_rows)
    _write_json(OUT_DIR / "phase_c_combined_results.json", {
        "n_sessions": len(full_dates), "champions_combined": validated,
        "selector_config": "moderate_combo (min_abs_delta=0.10, premium_band=0.15-0.40)",
        "results": combo_results,
    })
    write_marker("all_done", candidates=candidates, validated=validated,
                 phase_c_champions=validated)
    logger.info("Phase C complete. Full holdout sweep finished: %s", validated)


if __name__ == "__main__":
    main()
