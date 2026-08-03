#!/usr/bin/env python3
"""
greeks.py — IV solve + analytic Greeks + per-name daily IV surface over the
Databento OPRA parquet pull (build step 1 of BACKTEST_GREEKS_SPEC.md).

Greeks are COMPUTED (Black-Scholes, q=0, constant short-rate proxy), not bought:
daily OHLCV close (= last trade) is the mid proxy. Honest mitigations for not
having NBBO mid (per spec):
  * restrict to NEAR-THE-MONEY (band) MONTHLY expiries,
  * drop zero-volume contracts,
  * drop option prices below intrinsic (un-solvable / stale prints).

Modes:
  ./venv/bin/python greeks.py --selftest                 # BS unit tests, $0, instant
  ./venv/bin/python greeks.py --symbol PFE               # process one name -> parquet
  ./venv/bin/python greeks.py --symbol PFE --band 0.15   # widen NTM band

Outputs to data/options/:
  {SYM}_greeks.parquet   — per contract per day: S,K,T,cp,close,volume,oi,iv + greeks
  {SYM}_surface.parquet  — per day: atm_iv_front/back, term_slope, skew_25d

NOTE: --symbol does real per-contract IV solves over the full parquet (CPU-heavy on
the 1.9GB box). Per project discipline, run it AFTER MARKET CLOSE (~21:00 UTC), not
during RTH when the live scanners need the CPU. --selftest is free to run anytime.
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "options"
DEFAULT_R = 0.05  # constant 3m T-bill proxy (spec); override with --rate

# ----------------------------------------------------------------------------- BS core
def _d1d2(S, K, T, r, sigma):
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return d1, d2, sqrtT


def bs_price(S, K, T, r, sigma, is_call):
    """Black-Scholes price (q=0). Vectorized; is_call bool array/scalar."""
    S, K, T, r, sigma = map(np.asarray, np.broadcast_arrays(S, K, T, r, sigma))
    is_call = np.asarray(is_call)
    d1, d2, _ = _d1d2(S, K, T, r, sigma)
    disc = np.exp(-r * T)
    call = S * norm.cdf(d1) - K * disc * norm.cdf(d2)
    put = K * disc * norm.cdf(-d2) - S * norm.cdf(-d1)
    return np.where(is_call, call, put)


def bs_greeks(S, K, T, r, sigma, is_call):
    """All greeks (q=0). Returns dict of arrays.
    vega per 1% vol; theta & charm per CALENDAR DAY; vanna per 1% vol; delta per $1."""
    S, K, T, r, sigma = map(np.asarray, np.broadcast_arrays(S, K, T, r, sigma))
    is_call = np.asarray(is_call)
    d1, d2, sqrtT = _d1d2(S, K, T, r, sigma)
    nd1 = norm.pdf(d1)
    disc = np.exp(-r * T)
    delta_call = norm.cdf(d1)
    delta = np.where(is_call, delta_call, delta_call - 1.0)
    gamma = nd1 / (S * sigma * sqrtT)
    vega = S * nd1 * sqrtT                      # per 1.00 vol
    theta_call = -S * nd1 * sigma / (2 * sqrtT) - r * K * disc * norm.cdf(d2)
    theta_put = -S * nd1 * sigma / (2 * sqrtT) + r * K * disc * norm.cdf(-d2)
    theta = np.where(is_call, theta_call, theta_put)   # per year
    # vanna = dDelta/dVol; identical for calls/puts (q=0)
    vanna = -nd1 * d2 / sigma                    # per 1.00 vol
    # charm = dDelta/dt (t = calendar time); identical for calls/puts (q=0)
    charm = -nd1 * (2 * r * T - d2 * sigma * sqrtT) / (2 * T * sigma * sqrtT)  # per year
    return {
        "delta": delta,
        "gamma": gamma,
        "vega": vega / 100.0,        # report per 1% vol move
        "theta": theta / 365.0,      # report per calendar day
        "vanna": vanna / 100.0,      # report per 1% vol move
        "charm": charm / 365.0,      # report per calendar day
    }


def implied_vol(price, S, K, T, r, is_call, lo=1e-4, hi=5.0, iters=60):
    """Vectorized bisection IV solve. Bulletproof (monotone in sigma), no Newton blowups.
    Returns NaN where price is below intrinsic / non-positive / T<=0."""
    price = np.asarray(price, dtype=float)
    S, K, T, r = map(lambda x: np.asarray(x, dtype=float), np.broadcast_arrays(S, K, T, r))
    is_call = np.asarray(is_call)
    disc = np.exp(-r * T)
    intrinsic = np.where(is_call, np.maximum(S - K * disc, 0.0), np.maximum(K * disc - S, 0.0))
    valid = (price > 0) & (T > 0) & (price >= intrinsic - 1e-6) & (price < S * 2)
    a = np.full_like(price, lo)
    b = np.full_like(price, hi)
    for _ in range(iters):
        m = 0.5 * (a + b)
        pm = bs_price(S, K, T, r, m, is_call)
        too_low = pm < price          # need higher vol
        a = np.where(too_low, m, a)
        b = np.where(too_low, b, m)
    iv = 0.5 * (a + b)
    iv = np.where(valid, iv, np.nan)
    # reject pinned-to-bound solutions (no real signal there)
    iv = np.where((iv <= lo * 1.5) | (iv >= hi * 0.999), np.nan, iv)
    return iv


# ----------------------------------------------------------------------------- self-test
def selftest():
    # textbook: S=100,K=100,T=1,r=0.05,sigma=0.2
    S, K, T, r, sig = 100.0, 100.0, 1.0, 0.05, 0.20
    c = float(bs_price(S, K, T, r, sig, True))
    p = float(bs_price(S, K, T, r, sig, False))
    g = bs_greeks(S, K, T, r, sig, True)
    checks = [
        ("call price", c, 10.45058, 1e-3),
        ("put price", p, 5.57353, 1e-3),
        ("delta", float(g["delta"]), 0.636831, 1e-4),
        ("gamma", float(g["gamma"]), 0.018762, 1e-4),
        ("vega(1%)", float(g["vega"]), 0.375240, 1e-4),
        ("theta/day", float(g["theta"]), -0.017573, 1e-4),
    ]
    ok = True
    for name, got, want, tol in checks:
        good = abs(got - want) < tol
        ok &= good
        print(f"  [{'OK' if good else 'FAIL'}] {name:12s} got {got:.6f}  want {want:.6f}")
    # IV round-trip: price a call at 0.2, recover 0.2
    iv = float(implied_vol(c, S, K, T, r, True))
    good = abs(iv - 0.20) < 1e-4
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] iv round-trip got {iv:.6f}  want 0.200000")
    # put round-trip
    ivp = float(implied_vol(p, S, K, T, r, False))
    good = abs(ivp - 0.20) < 1e-4
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] iv put round-trip got {ivp:.6f}  want 0.200000")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


# ----------------------------------------------------------------------------- data layer
def load_defs(sym):
    """raw_symbol -> (strike, expiration, is_call), deduped across publishers."""
    df = pd.read_parquet(OUT / f"{sym}_defs.parquet",
                         columns=["raw_symbol", "strike_price", "expiration", "instrument_class"])
    df = df.drop_duplicates("raw_symbol").copy()
    sk = df["strike_price"].astype(float)
    if sk.max() > 1e6:        # some databento dumps scale strike by 1e9
        sk = sk / 1e9
    df["strike"] = sk
    df["expiration"] = pd.to_datetime(df["expiration"], utc=True).dt.tz_localize(None).dt.normalize()
    df["is_call"] = df["instrument_class"].astype(str).str.upper().str.startswith("C")
    return df[["raw_symbol", "strike", "expiration", "is_call"]]


def load_ohlcv(sym):
    """Daily close (volume-weighted across publishers) + total volume per (raw_symbol, date)."""
    # 2026-07-09 fix: restricting columns to [close,volume,symbol] silently dropped ts_event
    # for symbols where it is stored as a real data column rather than the parquet index
    # (confirmed: AMD has ts_event as the index, NVDA has it as a plain column). reset_index()
    # then recreated a meaningless default-int index column, and the df.columns[0] fallback
    # below picked THAT up as the date column -- every date silently collapsed to epoch
    # 1970-01-01, and the downstream spot-price join returned 0 rows with no error. Reading
    # the full file (no columns= restriction) guarantees ts_event survives either way.
    df = pd.read_parquet(OUT / f"{sym}_ohlcv1d.parquet")
    df = df.reset_index()  # ts_event index -> column (no-op if it is already a column)
    tcol = "ts_event" if "ts_event" in df.columns else df.columns[0]
    df["date"] = pd.to_datetime(df[tcol], utc=True).dt.tz_localize(None).dt.normalize()
    df["raw_symbol"] = df["symbol"].astype(str).str.strip()
    df["vw"] = df["close"] * df["volume"]
    g = df.groupby(["raw_symbol", "date"]).agg(vw=("vw", "sum"), volume=("volume", "sum")).reset_index()
    g["close"] = np.where(g["volume"] > 0, g["vw"] / g["volume"], np.nan)
    return g[["raw_symbol", "date", "close", "volume"]]


def load_oi(sym):
    """Open interest per (raw_symbol, date) from statistics schema (stat_type==9)."""
    path = OUT / f"{sym}_stats.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["ts_event", "quantity", "stat_type", "symbol"])
    df = df[df["stat_type"] == 9].copy()        # 9 = open_interest
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_localize(None).dt.normalize()
    df["raw_symbol"] = df["symbol"].astype(str).str.strip()
    # last reported OI per contract-day
    df = df.sort_values("ts_event").groupby(["raw_symbol", "date"]).agg(oi=("quantity", "last")).reset_index()
    return df


def underlying_closes(sym, dates):
    """Free Alpaca daily underlying closes for the date range. date(naive)->spot."""
    import requests
    A_KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
    A_SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
    h = {"APCA-API-KEY-ID": A_KEY, "APCA-API-SECRET-KEY": A_SEC}
    start = (min(dates)).strftime("%Y-%m-%d")
    end = (max(dates)).strftime("%Y-%m-%d")
    p = {"symbols": sym, "timeframe": "1Day", "start": start, "end": end,
         "limit": 10000, "adjustment": "raw", "feed": "iex"}
    r = requests.get("https://data.alpaca.markets/v2/stocks/bars", headers=h, params=p, timeout=30)
    bars = r.json().get("bars", {}).get(sym, [])
    out = {}
    for b in bars:
        d = pd.to_datetime(b["t"], utc=True).tz_localize(None).normalize()
        out[d] = b["c"]
    return out


def build_surface(g):
    """Per-day IV surface from the per-contract greeks frame g."""
    rows = []
    for date, day in g.groupby("date"):
        S = day["S"].iloc[0]
        exps = sorted(day["expiration"].unique())
        if not exps:
            continue
        front = exps[0]
        back = exps[1] if len(exps) > 1 else exps[0]

        def atm_iv(exp):
            sub = day[(day["expiration"] == exp) & day["iv"].notna()]
            if sub.empty:
                return np.nan
            k = sub.iloc[(sub["strike"] - S).abs().argmin()]
            # average call+put IV at the ATM strike if both present
            same = sub[np.isclose(sub["strike"], k["strike"])]
            return float(same["iv"].mean())

        atm_f = atm_iv(front)
        atm_b = atm_iv(back)
        fsub = day[(day["expiration"] == front) & day["iv"].notna()]
        call25 = fsub[fsub["is_call"]]
        put25 = fsub[~fsub["is_call"]]
        c_iv = (float(call25.iloc[(call25["delta"] - 0.25).abs().argmin()]["iv"])
                if not call25.empty else np.nan)
        p_iv = (float(put25.iloc[(put25["delta"] + 0.25).abs().argmin()]["iv"])
                if not put25.empty else np.nan)
        rows.append({
            "date": date, "S": S,
            "atm_iv_front": atm_f, "atm_iv_back": atm_b,
            "term_slope": (atm_b - atm_f) if (atm_b == atm_b and atm_f == atm_f) else np.nan,
            "skew_25d": (p_iv - c_iv) if (p_iv == p_iv and c_iv == c_iv) else np.nan,
        })
    return pd.DataFrame(rows)


def process_symbol(sym, band, rate, max_dte, monthly_only=True):
    print(f"=== greeks {sym}  band=±{band:.0%}  r={rate}  monthly_only={monthly_only} ===")
    defs = load_defs(sym)
    ohlcv = load_ohlcv(sym)
    df = ohlcv.merge(defs, on="raw_symbol", how="inner")
    df = df[df["volume"] > 0]                       # drop zero-volume contract-days
    if monthly_only:                                # standard monthly = 3rd Friday
        wd = df["expiration"].dt.weekday
        dom = df["expiration"].dt.day
        df = df[(wd == 4) & (dom.between(15, 21))]
    spots = underlying_closes(sym, list(df["date"].unique()))
    df["S"] = df["date"].map(spots)
    df = df[df["S"].notna()].copy()
    df["T"] = (df["expiration"] - df["date"]).dt.days / 365.0
    df = df[(df["T"] > 0) & (df["T"] <= max_dte / 365.0)]
    lo, hi = 1 - band, 1 + band
    df = df[(df["strike"] >= df["S"] * lo) & (df["strike"] <= df["S"] * hi)]
    if df.empty:
        print("  no contract-days after NTM/monthly/DTE filter"); return
    df["iv"] = implied_vol(df["close"].values, df["S"].values, df["strike"].values,
                           df["T"].values, rate, df["is_call"].values)
    gk = bs_greeks(df["S"].values, df["strike"].values, df["T"].values, rate,
                   np.where(df["iv"].notna(), df["iv"].values, 0.2), df["is_call"].values)
    for k, v in gk.items():
        df[k] = np.where(df["iv"].notna(), v, np.nan)
    oi = load_oi(sym)
    if oi is not None:
        df = df.merge(oi, on=["raw_symbol", "date"], how="left")
    else:
        df["oi"] = np.nan
    keep = ["date", "raw_symbol", "expiration", "strike", "is_call", "S", "T",
            "close", "volume", "oi", "iv", "delta", "gamma", "vega", "theta", "vanna", "charm"]
    out = df[keep].sort_values(["date", "expiration", "strike"]).reset_index(drop=True)
    gpath = OUT / f"{sym}_greeks.parquet"
    out.to_parquet(gpath)
    solved = out["iv"].notna().sum()
    print(f"  {len(out):,} contract-days  ({solved:,} IV-solved, "
          f"{100*solved/len(out):.0f}%)  -> {gpath.name}")
    surf = build_surface(out[out["iv"].notna()])
    spath = OUT / f"{sym}_surface.parquet"
    surf.to_parquet(spath)
    print(f"  {len(surf):,} surface-days  -> {spath.name}")
    with pd.option_context("display.width", 160):
        print(surf.tail(3).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--symbol")
    ap.add_argument("--band", type=float, default=0.12)
    ap.add_argument("--rate", type=float, default=DEFAULT_R)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--weeklies", action="store_true", help="include weekly expiries")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if a.symbol:
        process_symbol(a.symbol, a.band, a.rate, a.max_dte, monthly_only=not a.weeklies)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
