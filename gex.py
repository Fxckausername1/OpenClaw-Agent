#!/usr/bin/env python3
"""
gex.py — net Gamma Exposure, gamma-flip, and call/put walls per name/day.
Build step 2 of BACKTEST_GREEKS_SPEC.md, implementing THE GAMMA PLAYBOOK math exactly.

PLAYBOOK FORMULA (Section 4):
    per strike:  gamma × OI × 100 × S² × 0.01    (dealer sign: calls +, puts −)
    sum across the chain → net GEX.  Positive ⇒ SUPPRESSIVE/long-gamma regime
    (range, mean-revert, pinned).  Negative ⇒ AMPLIFYING/short-gamma regime
    (trending, momentum).  [matches BT1 hypothesis: POS→mean-rev, NEG→ORB]

    gamma-flip = spot level where net GEX(S) crosses zero (recomputed on a spot grid,
                 sticky-strike: each contract's solved IV held fixed).  Above flip =
                 suppressed; below = amplified.
    call wall  = strike (calls, above spot) with the largest gamma×OI — magnet/ceiling.
    put wall   = strike (puts, below spot) with the largest gamma×OI — magnet/floor.

PLAYBOOK HONESTY RULES (Sections 4 & 6) — enforced in output:
  * greeks are COMPUTED (BS), not exchange truth → flagged modeled.
  * coverage: every row reports n_oi / m_contracts ("computed over N of M strikes").
    Thin coverage ⇒ regime tagged 'none' (a blank read is a valid read; never fabricate).
  * T-1 OI lag: OI is prior-settle; for a daily backtest that PREDICTS day t from the
    OI known at t's open this is the honest, no-lookahead input. Documented, not hidden.

Reads {SYM}_greeks.parquet (from greeks.py). Writes {SYM}_gex.parquet.
Modes:  --selftest (synthetic chain, $0, anytime)  ·  --symbol SYM (post-close; light).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "options"
DEFAULT_R = 0.05
MIN_COVERAGE = 6          # need >= this many OI-bearing strikes for a trusted read
FLIP_BAND = 0.15          # search flip within ±15% of spot
FLIP_STEPS = 121


def bs_gamma(S, K, T, r, sigma):
    """BS gamma (q=0), vectorized. Same for calls/puts."""
    from scipy.stats import norm
    S, K, T, r, sigma = map(np.asarray, np.broadcast_arrays(S, K, T, r, sigma))
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    return norm.pdf(d1) / (S * sigma * sqrtT)


def net_gex_at(spot, K, T, iv, oi, sign, r):
    """Playbook aggregation at a hypothetical spot: Σ sign·γ(spot)·OI·100·spot²·0.01."""
    g = bs_gamma(spot, K, T, r, iv)
    return float(np.sum(sign * g * oi * 100.0 * spot * spot * 0.01))


def find_flip(spot, K, T, iv, oi, sign, r):
    """Spot level where net GEX crosses zero, nearest to current spot (sticky-strike)."""
    grid = np.linspace(spot * (1 - FLIP_BAND), spot * (1 + FLIP_BAND), FLIP_STEPS)
    vals = np.array([net_gex_at(s, K, T, iv, oi, sign, r) for s in grid])
    sgn = np.sign(vals)
    cross = np.where(np.diff(sgn) != 0)[0]
    if len(cross) == 0:
        return np.nan
    # crossing whose midpoint is nearest current spot
    mids = (grid[cross] + grid[cross + 1]) / 2.0
    i = cross[np.argmin(np.abs(mids - spot))]
    x0, x1, y0, y1 = grid[i], grid[i + 1], vals[i], vals[i + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))  # linear interp of the zero


def compute_day(day, spot, r):
    """day: rows for one date with strike,is_call,T,iv,oi,gamma. Returns a result dict."""
    d = day[(day["oi"] > 0) & day["iv"].notna() & (day["T"] > 0)].copy()
    m = len(day)
    n = len(d)
    base = {"date": day["date"].iloc[0], "spot": spot, "m_contracts": m, "n_oi": n,
            "coverage": (n / m if m else 0.0), "net_gex": np.nan, "regime": "none",
            "flip": np.nan, "call_wall": np.nan, "put_wall": np.nan, "greeks": "BS_modeled"}
    if n < MIN_COVERAGE or not np.isfinite(spot):
        return base                                   # thin chain → no clean read
    K = d["strike"].values.astype(float)
    T = d["T"].values.astype(float)
    iv = d["iv"].values.astype(float)
    oi = d["oi"].values.astype(float)
    sign = np.where(d["is_call"].values, 1.0, -1.0)   # dealer convention: calls +, puts −
    base["net_gex"] = net_gex_at(spot, K, T, iv, oi, sign, r)
    base["regime"] = "positive" if base["net_gex"] > 0 else "negative"
    base["flip"] = find_flip(spot, K, T, iv, oi, sign, r)
    # walls: largest gamma×OI contribution per side, on the correct side of spot
    contrib = bs_gamma(spot, K, T, r, iv) * oi
    calls = d["is_call"].values & (K > spot)
    puts = (~d["is_call"].values) & (K < spot)
    if calls.any():
        base["call_wall"] = float(K[calls][np.argmax(contrib[calls])])
    if puts.any():
        base["put_wall"] = float(K[puts][np.argmax(contrib[puts])])
    return base


def process_symbol(sym, r):
    gpath = OUT / f"{sym}_greeks.parquet"
    if not gpath.exists():
        print(f"  {gpath.name} missing — run greeks.py --symbol {sym} first (post-close)."); return
    g = pd.read_parquet(gpath)
    rows = [compute_day(day, day["S"].iloc[0], r) for _, day in g.groupby("date")]
    out = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    opath = OUT / f"{sym}_gex.parquet"
    out.to_parquet(opath)
    clean = out[out["regime"] != "none"]
    pos = (clean["regime"] == "positive").sum()
    print(f"=== gex {sym}: {len(out)} days, {len(clean)} clean reads "
          f"({pos} positive / {len(clean)-pos} negative), "
          f"mean coverage {out['coverage'].mean():.0%} -> {opath.name} ===")
    with pd.option_context("display.width", 170):
        print(out.tail(4)[["date", "spot", "net_gex", "regime", "flip",
                            "call_wall", "put_wall", "n_oi", "m_contracts"]].to_string(index=False))


def selftest():
    """Synthetic chain: spot=100, call-heavy OI above / put-heavy below.
    Expect well-defined net GEX, a finite flip near spot, walls on the right sides."""
    spot, r = 100.0, DEFAULT_R
    strikes = np.arange(80, 121, 5.0)
    rows = []
    for K in strikes:
        for is_call in (True, False):
            # more call OI above spot, more put OI below → classic long-gamma profile
            oi = (800 if (is_call and K >= spot) or ((not is_call) and K <= spot) else 150)
            rows.append({"date": pd.Timestamp("2026-01-15"), "strike": K, "is_call": is_call,
                         "T": 0.08, "iv": 0.25, "oi": oi, "S": spot})
    day = pd.DataFrame(rows)
    day["gamma"] = bs_gamma(spot, day["strike"].values, day["T"].values, r, day["iv"].values)
    res = compute_day(day, spot, r)
    ok = True
    checks = [
        ("coverage==1.0", abs(res["coverage"] - 1.0) < 1e-9),
        ("regime resolved", res["regime"] in ("positive", "negative")),
        ("net_gex finite", np.isfinite(res["net_gex"])),
        ("flip finite", np.isfinite(res["flip"])),
        ("flip within band", (res["spot"]*0.85) <= res["flip"] <= (res["spot"]*1.15)),
        ("call wall above spot", res["call_wall"] > spot),
        ("put wall below spot", res["put_wall"] < spot),
    ]
    for name, good in checks:
        ok &= good
        print(f"  [{'OK' if good else 'FAIL'}] {name}")
    # thin-chain guard: a 2-strike day must return 'none' (never fabricate)
    thin = day.iloc[:4].copy()
    rthin = compute_day(thin, spot, r)
    good = rthin["regime"] == "none"
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] thin chain -> 'no clean read' (regime=none)")
    print(f"  net_gex={res['net_gex']:.3e}  regime={res['regime']}  flip={res['flip']:.2f}  "
          f"call_wall={res['call_wall']}  put_wall={res['put_wall']}")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--symbol")
    ap.add_argument("--rate", type=float, default=DEFAULT_R)
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if a.symbol:
        process_symbol(a.symbol, a.rate)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
