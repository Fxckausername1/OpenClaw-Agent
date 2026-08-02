#!/usr/bin/env python3
"""gex_regime_backtest.py — standalone BT1 test (BACKTEST_GREEKS_SPEC.md / GAMMA_PLAYBOOK.md):
does T-1 GEX regime conditioning lift MR/ORB quality, on the names+window where we actually
have Databento OPRA-derived GEX data?

Hypothesis (a priori, from the playbook — NOT tuned here): POS-gamma (suppressive/pinned)
favors mean-reversion; NEG-gamma (amplifying) favors ORB/momentum.

HONESTY CAVEATS (own up front, per this project's discipline):
 - GEX data exists for 11 names (the options-pull universe) over ~6mo (2025-12-01 to
   2026-06-01). Only 5 of those are ALSO in the equity wf_cache used by walkforward_search.py
   (BAC CSCO INTC NVDA PFE) — F/PLTR/XLF/GDX/HOOD/AMD are not in the 99-symbol mr/orb universe
   at all, so this test is necessarily restricted to 5 names.
 - That 6mo window sits almost entirely inside what would be the LAST 25% (locked holdout) of
   the existing 2yr equity cache — reusing walkforward_search.py's global SEARCH_FRAC split
   would starve "search" to near-zero trades. This is a single a-priori hypothesis test (no
   parameter tuning / no ratchet search), so that split isn't the right tool here; the real
   risk is plain sample size, reported explicitly below using the project's own MIN_TRADES=30
   quality bar.
 - "baseline" below = mr_z1.5+orb run ONLY on days with a clean GEX regime read for that symbol
   (not the symbol's full 2yr history) — isolates the conditioning effect from the
   window-selection effect (apples to apples).
 - T-1 OI lag is inherited from gex.py (OI is prior-settle, so a date's regime is already the
   honest no-lookahead read for predicting that date's session).

Usage: ./venv/bin/python gex_regime_backtest.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walkforward_search import gen_mean_rev, gen_orb, load_cached, MR_CAP, ORB_CAP, COST_BPS, MIN_RISK_FRAC

ROOT = Path(__file__).resolve().parent
GEX_DIR = ROOT / "data" / "options"
SYMS = ["BAC", "CSCO", "INTC", "NVDA", "PFE"]
MIN_TRADES = 30   # same quality guard walkforward_search.py uses everywhere else


def load_regime_dates(sym):
    p = GEX_DIR / f"{sym}_gex.parquet"
    if not p.exists():
        return set(), set()
    g = pd.read_parquet(p)
    clean = g[g["regime"].isin(["positive", "negative"])]
    pos = set(clean.loc[clean["regime"] == "positive", "date"].astype(str).str[:10])
    neg = set(clean.loc[clean["regime"] == "negative", "date"].astype(str).str[:10])
    return pos, neg


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
    return f"{s['total_r']:+.1f}R | {s['n']:>3} tr | {s['per_trade']:+.3f}R/tr | Sharpe {s['sharpe']:.2f}"


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
        pos_dates, neg_dates = load_regime_dates(sym)
        all_regime_dates = pos_dates | neg_dates
        if not all_regime_dates:
            print(f"{sym}: no clean GEX reads (thin chain / missing) -> skip")
            continue

        d = df.index.date
        in_window = np.array([str(x) in all_regime_dates for x in d])
        in_pos = np.array([str(x) in pos_dates for x in d])
        in_neg = np.array([str(x) in neg_dates for x in d])

        df_window = df[in_window]
        df_pos = df[in_pos]
        df_neg = df[in_neg]

        b_mr = gen_mean_rev(df_window, MR_CAP["p"])
        b_orb = gen_orb(df_window, ORB_CAP["p"])
        c_mr = gen_mean_rev(df_pos, MR_CAP["p"])
        c_orb = gen_orb(df_neg, ORB_CAP["p"])

        base_rows += b_mr + b_orb
        cond_rows += c_mr + c_orb

        per_symbol[sym] = dict(
            n_days=len(all_regime_dates), n_pos=len(pos_dates), n_neg=len(neg_dates),
            base_mr=score(b_mr), base_orb=score(b_orb),
            cond_mr=score(c_mr), cond_orb=score(c_orb),
        )

    print("=== BT1: GEX regime conditioning, per symbol (T-1 OI, no lookahead) ===")
    for sym, r in per_symbol.items():
        print(f"\n{sym}: {r['n_days']} GEX-read days ({r['n_pos']} pos / {r['n_neg']} neg)")
        print(f"  MR  baseline (all GEX-window days): {fmt(r['base_mr'])}")
        print(f"  MR  GEX-conditioned (pos days only): {fmt(r['cond_mr'])}")
        print(f"  ORB baseline (all GEX-window days): {fmt(r['base_orb'])}")
        print(f"  ORB GEX-conditioned (neg days only): {fmt(r['cond_orb'])}")

    print("\n=== POOLED (baseline mr+orb vs GEX-conditioned mr+orb, same symbols+window) ===")
    base_port = score(base_rows)
    cond_port = score(cond_rows)
    print(f"baseline:        {fmt(base_port)}")
    print(f"GEX-conditioned: {fmt(cond_port)}")

    if base_port["n"] < MIN_TRADES or cond_port["n"] < MIN_TRADES:
        print(f"\n*** SAMPLE TOO SMALL ({base_port['n']} base / {cond_port['n']} conditioned trades, "
              f"need >= {MIN_TRADES}) to trust either direction. Honest 'no clean read' — "
              f"NOT a carry, NOT a kill, just event/data-starved like the VIX filter. ***")
    elif cond_port["per_trade"] > base_port["per_trade"] and cond_port["sharpe"] > base_port["sharpe"]:
        print("\n-> Lifts BOTH per-trade R and Sharpe vs the same-window baseline. CARRIES at this sample size.")
    else:
        print("\n-> Does NOT lift both metrics. KILL (same bar as risk-off / regime-ORB / VIX).")


if __name__ == "__main__":
    main()
