#!/usr/bin/env python3
"""backtest_charm_regime.py -- standalone test: does CHARM exposure magnitude predict ORB/MR fit,
alongside (not instead of) the existing GEX regime filter?

HYPOTHESIS (stated a priori, 2026-07-03, before running -- per this project's discipline of never
tuning a hypothesis after seeing results): charm = dDelta/dTime, a pure time-decay effect on dealer
delta exposure, independent of price. High |charm| exposure should predict stronger intraday
directional continuation (favors ORB/momentum); low |charm| should be calmer, more range-bound
(favors mean-reversion). Unlike gamma, there is no pre-existing documented playbook hypothesis for
charm in this codebase (BACKTEST_GREEKS_SPEC.md computes it only as a byproduct of the vega/BT2
build) -- this hypothesis was formulated fresh, before running, not reverse-engineered from results.

HONESTY CAVEATS (own up front, per this project's discipline):
 - Charm exposure data exists for the 6 names with historical greeks (AMD, BAC, CSCO, F, INTC, PFE),
   Dec 2025 - Jun 2026 -- unlike the GEX-regime test, ALL 6 overlap the equity mr/orb universe (a
   better starting position than BT1 had, verified 2026-07-03).
 - Same T-1 OI lag inherited from gex.py/greeks.py (OI is prior-settle, honest no-lookahead read).
 - Magnitude split (HIGH/LOW |charm|) is computed PER SYMBOL at the median, since raw charm exposure
   scales with each name's own OI/price and isn't comparable cross-sectionally.
 - "baseline" = mr_z1.5+orb run ONLY on days with a clean charm read for that symbol (not the
   symbol's full history) -- isolates the conditioning effect from window-selection, same as BT1.

Usage: ./venv/bin/python backtest_charm_regime.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walkforward_search import gen_mean_rev, gen_orb, load_cached, MR_CAP, ORB_CAP, COST_BPS, MIN_RISK_FRAC

ROOT = Path(__file__).resolve().parent
OPTIONS_DIR = ROOT / "data" / "options"
SYMS = ["AMD", "BAC", "CSCO", "F", "INTC", "PFE"]
MIN_TRADES = 30


def daily_charm_exposure(sym):
    """Per-day charm exposure: sum(sign * charm * OI * 100 * S^2 * 0.01) -- same dealer-sign
    convention and scaling as gex.py's net_gex_at, applied to charm instead of gamma, for direct
    methodological consistency with the existing GEX work."""
    p = OPTIONS_DIR / f"{sym}_greeks.parquet"
    if not p.exists():
        return None
    g = pd.read_parquet(p)
    g = g[g["oi"] > 0].copy()
    sign = np.where(g["is_call"], 1.0, -1.0)
    g["contrib"] = sign * g["charm"] * g["oi"] * 100.0 * g["S"] * g["S"] * 0.01
    daily = g.groupby("date").agg(charm_exp=("contrib", "sum"), S=("S", "first"), n_oi=("oi", "count"))
    daily = daily[daily["n_oi"] >= 6]  # same MIN_COVERAGE bar gex.py uses
    return daily


def score(rows):
    if not rows:
        return dict(n=0, total_r=0.0, per_trade=0.0, sharpe=0.0)
    df = pd.DataFrame(rows, columns=["date", "side", "r_gross", "risk_frac"])
    df["net_r"] = df["r_gross"] - (COST_BPS / 10000.0) / df["risk_frac"].clip(lower=MIN_RISK_FRAC)
    n = len(df)
    total = float(df["net_r"].sum())
    daily = df.groupby("date")["net_r"].sum()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    return dict(n=n, total_r=total, per_trade=total / n, sharpe=sharpe)


def fmt(s):
    return f"{s['total_r']:+.1f}R | {s['n']:>4} tr | {s['per_trade']:+.3f}R/tr | Sharpe {s['sharpe']:.2f}"


def main():
    cached = dict(load_cached(SYMS))
    missing = set(SYMS) - set(cached)
    if missing:
        print(f"WARNING missing from wf_cache: {missing}")

    base_rows, cond_rows = [], []
    per_symbol = {}

    for sym in SYMS:
        df = cached.get(sym)
        if df is None:
            continue
        daily = daily_charm_exposure(sym)
        if daily is None or daily.empty:
            print(f"{sym}: no clean charm reads (thin chain / missing) -> skip")
            continue
        median_abs = daily["charm_exp"].abs().median()
        high_dates = set(daily[daily["charm_exp"].abs() >= median_abs].index.astype(str).str[:10])
        low_dates = set(daily[daily["charm_exp"].abs() < median_abs].index.astype(str).str[:10])
        all_dates = high_dates | low_dates

        d = df.index.date
        in_window = np.array([str(x) in all_dates for x in d])
        in_high = np.array([str(x) in high_dates for x in d])
        in_low = np.array([str(x) in low_dates for x in d])

        df_window = df[in_window]
        df_high = df[in_high]
        df_low = df[in_low]

        b_mr = gen_mean_rev(df_window, MR_CAP["p"])
        b_orb = gen_orb(df_window, ORB_CAP["p"])
        c_mr = gen_mean_rev(df_low, MR_CAP["p"])     # hypothesis: MR favored on LOW |charm|
        c_orb = gen_orb(df_high, ORB_CAP["p"])       # hypothesis: ORB favored on HIGH |charm|

        base_rows += b_mr + b_orb
        cond_rows += c_mr + c_orb

        per_symbol[sym] = dict(
            n_days=len(all_dates), n_high=len(high_dates), n_low=len(low_dates),
            base_mr=score(b_mr), base_orb=score(b_orb),
            cond_mr=score(c_mr), cond_orb=score(c_orb),
        )

    print("=== Charm-magnitude regime test, per symbol (T-1 OI, no lookahead) ===")
    for sym, r in per_symbol.items():
        print(f"\n{sym}: {r['n_days']} charm-read days ({r['n_high']} high-|charm| / {r['n_low']} low-|charm|)")
        print(f"  MR  baseline (all charm-window days): {fmt(r['base_mr'])}")
        print(f"  MR  charm-conditioned (low-|charm| only): {fmt(r['cond_mr'])}")
        print(f"  ORB baseline (all charm-window days): {fmt(r['base_orb'])}")
        print(f"  ORB charm-conditioned (high-|charm| only): {fmt(r['cond_orb'])}")

    print("\n=== POOLED (baseline mr+orb vs charm-conditioned mr+orb, same symbols+window) ===")
    base_port = score(base_rows)
    cond_port = score(cond_rows)
    print(f"baseline:          {fmt(base_port)}")
    print(f"charm-conditioned: {fmt(cond_port)}")

    if base_port["n"] < MIN_TRADES or cond_port["n"] < MIN_TRADES:
        print(f"\n*** SAMPLE TOO SMALL ({base_port['n']} base / {cond_port['n']} conditioned trades, "
              f"need >= {MIN_TRADES}) to trust either direction. ***")
    elif cond_port["per_trade"] > base_port["per_trade"] and cond_port["sharpe"] > base_port["sharpe"]:
        print("\n-> Lifts BOTH per-trade R and Sharpe vs the same-window baseline. CARRIES at this sample size.")
    else:
        print("\n-> Does NOT lift both metrics. KILL (same bar as risk-off / regime-ORB / VIX).")


if __name__ == "__main__":
    main()
