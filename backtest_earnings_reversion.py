#!/usr/bin/env python3
"""backtest_earnings_reversion.py -- first-ever holdout test of gen_earnings_reversion
(fade intraday gap-reversions on post-earnings-announcement days), 2026-07-08.

Two real fixes needed vs. just calling comp_earn()/wf.load_earnings_cache() directly:
1. wf.load_earnings_cache() points at data/bt_earnings.json, which does not exist -- the
   real, live cron-built cache is data/earnings_cache.json (built daily, 45 12 * * 1-5).
   Loading it directly here instead, bypassing the broken path (not patching the shared
   module without heff's review).
2. load_earnings_cache() as written flattens ALL tickers' dates into one global set --
   but gen_earnings_reversion never checks which ticker it's running on, so with a global
   set + a 200-name universe, almost every trading day has SOMEONE'S earnings, defeating
   the whole point of an earnings-specific strategy. Fixed by generating PER-SYMBOL,
   passing each symbol only ITS OWN tagged dates (data/earnings_cache.json IS keyed
   per-ticker; the bug was in how it gets flattened downstream, not the data itself).

Real coverage check (2026-07-08): 100 tickers, ~74 tagged dates/ticker (~25 events x 3-day
window each) back to 2020 -- expected to be event-starved vs the daily-signal strategies
(see the 2026-06-15 VIX-filter precedent: a real-but-small effect can fail to clear a
total-R bar even if directionally fine), but the per-ticker sample should be large enough
in aggregate to at least get an honest read, unlike a single-name test would be.
"""
import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
import walkforward_search as wf

ROOT = Path("/home/heff/.openclaw/workspace")
OUT_LEDGER = ROOT / "data" / "earnings_reversion_wf_ledger.csv"
OUT_RESULT = ROOT / "data" / "earnings_reversion_wf_result.json"
COST = wf.COST_BPS / 10000.0
MRF = wf.MIN_RISK_FRAC


def load_per_ticker_earnings():
    p = ROOT / "data" / "earnings_cache.json"
    d = json.loads(p.read_text())
    return d.get("tickers", {})


def to_frame(rows):
    out = pd.DataFrame(rows, columns=["date", "side", "r_gross", "risk_frac"])
    if not out.empty:
        out["net_r"] = out["r_gross"] - COST / out["risk_frac"].clip(lower=MRF)
    else:
        out["net_r"] = []
    return out


def gen_earnings(cached, per_ticker_dates, hold_until, min_rr):
    rows = []
    for sym, df in cached:
        dates = per_ticker_dates.get(sym)
        if not dates:
            continue
        p = dict(earn_dates=tuple(dates), hold_until=hold_until, max_price=250.0,
                 stop_buf=0.0015, min_rr=min_rr, _ticker=sym)
        for r in wf.gen_earnings_reversion(df, p):
            rows.append(r)
    return to_frame(rows)


def sc(df, region):
    t = df[df["date"].isin(region)]
    n = len(t)
    tot = float(t["net_r"].sum())
    per = tot / n if n else 0.0
    d = t.groupby("date")["net_r"].sum()
    sh = float(d.mean() / d.std() * np.sqrt(252)) if len(d) > 1 and d.std() > 0 else 0.0
    return dict(total_r=tot, n=n, per_trade=per, sharpe=sh)


def concentration(df, region, topn=10):
    t = df[df["date"].isin(region)]
    if t.empty:
        return {"n": 0}
    t = t.sort_values("net_r", ascending=False)
    total = t["net_r"].sum()
    topn = min(topn, len(t))
    top_sum = t.head(topn)["net_r"].sum()
    return {
        "win_rate": round(float((t["net_r"] > 0).mean()), 3),
        "median_r": round(float(t["net_r"].median()), 3),
        "mean_r": round(float(t["net_r"].mean()), 3),
        "top10_pct_of_profit": (round(float(top_sum / total * 100), 1) if total != 0 else None),
    }


def fmt(s):
    return "{:+7.1f}R  n={:<5} {:+.4f}R/tr  Sharpe={:+.2f}".format(
        s["total_r"], s["n"], s["per_trade"], s["sharpe"])


def verdict(h, diag, min_n=30):
    if h["n"] < min_n:
        return "INSUFFICIENT SAMPLE / EVENT-STARVED (n={} < {} -- same failure mode as the 2026-06-15 VIX filter)".format(h["n"], min_n)
    if h["per_trade"] <= 0:
        return "MIRAGE -- non-positive holdout expectancy ({:+.4f}R/tr)".format(h["per_trade"])
    if diag.get("top10_pct_of_profit") and diag["top10_pct_of_profit"] > 60:
        return "SUSPECT -- {:.0f}% of profit from top 10 trades".format(diag["top10_pct_of_profit"])
    return "LOOKS REAL -- positive on holdout, not outlier-concentrated"


def main():
    from datetime import time as dtime
    import mean_reversion_scanner as mr

    syms = wf.wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
    per_ticker = load_per_ticker_earnings()
    overlap = [s for s in syms if s in per_ticker and per_ticker[s]]
    print("universe: {} symbols, {} have real per-ticker earnings dates".format(len(syms), len(overlap)))
    cached = wf.load_cached(overlap)
    print("cached: {}/{} symbols".format(len(cached), len(overlap)))

    CANDIDATES = [
        ("earnings_reversion: hold09:45 minrr1.0 (default)", dtime(9, 45), 1.0),
        ("earnings_reversion: hold09:45 minrr1.5 (stricter)", dtime(9, 45), 1.5),
    ]

    t0 = time.time()
    frames = {}
    for name, hold_until, min_rr in CANDIDATES:
        f = gen_earnings(cached, per_ticker, hold_until, min_rr)
        frames[name] = f
        print("  {:<48} {:>5} tr ({:.0f}s)".format(name, len(f), time.time() - t0))

    all_dates = []
    for f in frames.values():
        all_dates += f["date"].tolist()
    if not all_dates:
        print("\nZERO trades from any candidate. Not proceeding.")
        return
    search, holdout = wf.date_split(all_dates)
    print("\ndate split: {} search days / {} locked holdout days".format(len(search), len(holdout)))

    print("\n" + "=" * 88)
    results = {}
    for name, _, _ in CANDIDATES:
        f = frames[name]
        s, h = sc(f, search), sc(f, holdout)
        results[name] = (s, h)
    print("SEARCH (in-sample, orientation only):")
    for name, (s, h) in results.items():
        print("  {:<48} {}".format(name, fmt(s)))
    print("\nLOCKED HOLDOUT:")
    for name, (s, h) in results.items():
        print("  {:<48} {}".format(name, fmt(h)))

    print("\nDIAGNOSTICS + VERDICT:")
    led = []
    for name, _, _ in CANDIDATES:
        f = frames[name]
        s, h = results[name]
        diag = concentration(f, holdout)
        v = verdict(h, diag)
        print("\n  {}: {}".format(name, v))
        print("    diagnostics: {}".format(json.dumps(diag)))
        led.append(dict(name=name, verdict=v, search_total_r=s["total_r"], search_n=s["n"],
                        search_per_trade=s["per_trade"], search_sharpe=s["sharpe"],
                        holdout_total_r=h["total_r"], holdout_n=h["n"],
                        holdout_per_trade=h["per_trade"], holdout_sharpe=h["sharpe"], **diag))

    pd.DataFrame(led).to_csv(OUT_LEDGER, index=False)
    OUT_RESULT.write_text(json.dumps(led, indent=2, default=str))
    print("\nwrote {}\nwrote {}".format(OUT_LEDGER, OUT_RESULT))


if __name__ == "__main__":
    main()
