#!/usr/bin/env python3
"""pairs_screener.py — out-of-sample pair selection via Engle-Granger cointegration testing,
the fix this project's own research history flagged as necessary to make pairs/stat-arb
trustworthy (see "Dead-ends ruled out" 2026-06-13: the original pairs_backtest.py result —
96% win, +1.30R, PF38 — was identified as a MIRAGE precisely because pairs were hand-picked
by known correlation (selection bias) with no real cointegration test and no transaction-cost
model. This module is the "real pair selection" half of that fix; it does NOT do the cost-
modeled backtest itself — that is a follow-up once a screened candidate list exists).

METHODOLOGY:
  1. Correlation pre-filter: cheap, on already-cached DAILY closes (via walkforward_search's
     load_daily_cached, which builds+caches data/wf_daily_cache/ from Alpaca on first call —
     deliberately DAILY not 5-min: cointegration is a slower-moving, multi-month relationship,
     and testing it on raw 5-min bars would be ~78x the compute for no benefit). Only pairs
     above min_corr proceed to the expensive step — standard practice in pairs-trading
     research, and what keeps the combinatorial space tractable on this single-core box (a
     full pairwise screen is O(n^2); even a couple hundred names makes the naive approach
     prohibitively slow without this cut).
  2. Engle-Granger cointegration test (statsmodels.tsa.stattools.coint) on the correlation-
     surviving pairs, plus the OLS hedge ratio (beta) from statsmodels.api.OLS — needed to
     build the actual spread = price_a - beta*price_b if a pair is later traded.
  3. Incremental, budget-capped, ledger-tracked (same pattern as continuous_search.py): every
     pair tried is hashed into pair_key and logged so a screen can run across many nights
     without ever re-testing a pair, and a single run can be capped to fit a post-close window
     on this 1.9GB single-core box.

THIS IS A SCREENING ENGINE ONLY — it ranks candidate pairs by cointegration p-value, it does
NOT backtest or size a spread-trading strategy. Per heff's explicit instruction (Phase 3,
2026-06-29): write-only, do NOT run the real screen against market data yet — only the
synthetic --selftest has been exercised.
"""
import sys
import argparse
from pathlib import Path
from itertools import combinations

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.stattools import coint

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walkforward_search import load_daily_cached, ROOT
import wide_universe

from log_setup import get_logger
log = get_logger("pairs_screener")

DATA = ROOT / "data"
LEDGER = DATA / "pairs_screener_ledger.csv"
CANDIDATES_OUT = DATA / "pairs_candidates.csv"
MIN_CORR = 0.70
PVAL_THRESHOLD = 0.05
MIN_OVERLAP_DAYS = 120


def pair_key(sym_a, sym_b):
    a, b = sorted((sym_a, sym_b))
    return f"{a}|{b}"


def load_tried():
    if not LEDGER.exists():
        return set()
    try:
        return set(pd.read_csv(LEDGER)["pair"].tolist())
    except Exception:
        return set()


def append_ledger(rows):
    if not rows:
        return
    df = pd.DataFrame(rows)
    DATA.mkdir(parents=True, exist_ok=True)
    if LEDGER.exists():
        df.to_csv(LEDGER, mode="a", header=False, index=False)
    else:
        df.to_csv(LEDGER, index=False)


def correlation_prefilter(closes_by_sym, min_corr=MIN_CORR, min_overlap=MIN_OVERLAP_DAYS):
    """Cheap O(n^2) correlation pass on daily closes -> candidate pairs above min_corr.
    Aligns each pair on its overlapping dates only (symbols can have different cache
    histories); skips pairs with too little overlap to trust a correlation estimate."""
    syms = list(closes_by_sym.keys())
    cands = []
    for sym_a, sym_b in combinations(syms, 2):
        a, b = closes_by_sym[sym_a], closes_by_sym[sym_b]
        joined = pd.concat([a.rename("a"), b.rename("b")], axis=1, join="inner").dropna()
        if len(joined) < min_overlap:
            continue
        corr = joined["a"].corr(joined["b"])
        if corr is not None and corr >= min_corr:
            cands.append((sym_a, sym_b, float(corr), len(joined)))
    cands.sort(key=lambda x: -x[2])
    return cands


def test_cointegration(series_a, series_b):
    """Engle-Granger test (statsmodels.tsa.stattools.coint) + the OLS hedge ratio needed to
    build the spread. Returns (p_value, beta, n_obs) — p_value/beta are None if the test
    errors (e.g. degenerate/constant series) or there isn't enough overlap; never raises, the
    caller treats None as 'skip'."""
    joined = pd.concat([series_a.rename("a"), series_b.rename("b")], axis=1,
                       join="inner").dropna()
    if len(joined) < MIN_OVERLAP_DAYS:
        return None, None, len(joined)
    try:
        _, pvalue, _ = coint(joined["a"], joined["b"])
        ols = sm.OLS(joined["a"], sm.add_constant(joined["b"])).fit()
        beta = float(ols.params["b"])
        return float(pvalue), beta, len(joined)
    except Exception as e:
        log.warning(f"test_cointegration failed: {e}")
        return None, None, len(joined)


def screen_pairs(symbols=None, min_corr=MIN_CORR, pval_threshold=PVAL_THRESHOLD,
                 budget_pairs=200):
    """Main driver. symbols=None -> the live equity universe (wide_universe.load_universe,
    the S&P-500-index-based universe as of 2026-06-29).
    1) load_daily_cached for every symbol (cache-or-fetch via Alpaca, same data source
       market_regime.py uses).
    2) correlation_prefilter to shortlist candidate pairs.
    3) cointegration test on up to `budget_pairs` NOT-already-tried candidates this run
       (ledger-tracked, same incremental pattern as continuous_search.py).
    Writes/append-only CANDIDATES_OUT (every pair that passed pval_threshold, re-sorted by
    p-value ascending each run) and returns the list of newly-tested ledger rows."""
    symbols = symbols or wide_universe.load_universe(rebuild_if_stale=False)
    cached = load_daily_cached(symbols)
    log.info(f"loaded daily closes for {len(cached)}/{len(symbols)} symbols")
    closes_by_sym = {sym: df["Close"] for sym, df in cached}

    candidates = correlation_prefilter(closes_by_sym, min_corr=min_corr)
    n_possible = len(closes_by_sym) * (len(closes_by_sym) - 1) // 2
    log.info(f"{len(candidates)} pairs above corr>={min_corr} out of {n_possible} possible")

    tried = load_tried()
    batch = [c for c in candidates if pair_key(c[0], c[1]) not in tried][:budget_pairs]
    log.info(f"testing {len(batch)} new pairs ({len(tried)} already tried)")

    ledger_rows, passed = [], []
    for sym_a, sym_b, corr, n_corr in batch:
        pval, beta, n_obs = test_cointegration(closes_by_sym[sym_a], closes_by_sym[sym_b])
        decision = "error" if pval is None else ("PASS" if pval <= pval_threshold else "fail")
        ledger_rows.append({"pair": pair_key(sym_a, sym_b), "sym_a": sym_a, "sym_b": sym_b,
                            "corr": corr, "pvalue": pval, "beta": beta, "n_obs": n_obs,
                            "decision": decision})
        if decision == "PASS":
            passed.append(ledger_rows[-1])

    append_ledger(ledger_rows)
    if passed:
        df_new = pd.DataFrame(passed)
        if CANDIDATES_OUT.exists():
            df_all = pd.concat([pd.read_csv(CANDIDATES_OUT), df_new], ignore_index=True)
        else:
            df_all = df_new
        df_all.sort_values("pvalue", inplace=True)
        df_all.to_csv(CANDIDATES_OUT, index=False)
    log.info(f"screen_pairs: {len(passed)}/{len(batch)} passed p<={pval_threshold} this run")
    return ledger_rows


def _selftest():
    """Synthetic series only — no network, no cached market data. Builds one genuinely
    cointegrated pair (B = A + stationary noise, by construction) and one pair of independent
    random walks (should NOT cointegrate), confirms the test tells them apart."""
    rng = np.random.default_rng(7)
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="D")

    a = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)), index=idx)
    coint_b = a + rng.normal(0, 0.5, n)                                # stationary spread -> cointegrated
    indep_b = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)), index=idx)  # independent walk

    p_coint, beta_coint, _ = test_cointegration(a, coint_b)
    p_indep, beta_indep, _ = test_cointegration(a, indep_b)

    assert p_coint is not None and p_coint < 0.05, f"constructed-cointegrated pair should pass, p={p_coint}"
    assert p_indep is None or p_indep > 0.05, f"independent walks should NOT cointegrate, p={p_indep}"
    assert beta_coint is not None and 0.5 < beta_coint < 1.5, f"hedge ratio should be ~1, got {beta_coint}"

    closes = {"A": a, "B": coint_b, "C": indep_b}
    cands = correlation_prefilter(closes, min_corr=0.5, min_overlap=50)
    pairs_found = {pair_key(x[0], x[1]) for x in cands}
    assert pair_key("A", "B") in pairs_found, "A/B should pass the correlation prefilter"

    print(f"selftest OK: cointegrated pair p={p_coint:.4f} beta={beta_coint:.3f}, "
          f"independent pair p={p_indep}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--screen", action="store_true",
                    help="run the REAL screen against cached market data")
    ap.add_argument("--budget-pairs", type=int, default=200)
    ap.add_argument("--min-corr", type=float, default=MIN_CORR)
    ap.add_argument("--pval-threshold", type=float, default=PVAL_THRESHOLD)
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    if not a.screen:
        print("nothing to do — pass --selftest (synthetic, safe) or --screen (real data, "
              "NOT yet authorized for an unattended run — see module docstring).")
        return
    screen_pairs(min_corr=a.min_corr, pval_threshold=a.pval_threshold,
                budget_pairs=a.budget_pairs)


if __name__ == "__main__":
    main()
