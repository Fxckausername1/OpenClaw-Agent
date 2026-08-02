#!/usr/bin/env python3
"""backtest_pead.py -- first-ever holdout test of Post-Earnings-Announcement Drift
(PEAD), 2026-07-09. Multi-day SWING continuation thesis: large EPS surprises tend to
keep drifting in the surprise direction for days afterward, rather than reverting.
This is NOT the same idea as earnings_reversion (2026-07-08, killed event-starved) --
that was a same-day intraday FADE; this is a several-trading-day HOLD in the surprise
direction, using real EPS-surprise MAGNITUDE (not just "did it report").

Data: EPS surprise % from yfinance get_earnings_dates() (free, same library
build_earnings_cache.py already uses daily -- confirmed 'Surprise(%)' column exists,
yfinance 1.4.1). fetch_pead_surprise.py (one-shot, run separately) wrote
data/pead_surprise_cache.json: 180 wide_universe symbols, 8254 raw reported-EPS
events back to 2013, 0 fetch failures. Price data = wf_daily_cache (Alpaca daily
bars, split-adjusted, back to ~2020-07-27) -- this is the real binding constraint,
not the earnings data itself.

Methodology:
  - Entry = Open of the first trading day strictly AFTER the report's calendar
    date (BMO/AMC not disambiguated -- literal "day after the report" per the
    research brief, a documented simplification).
  - Direction: LONG on positive surprise, SHORT on negative, threshold on
    |surprise%| (first-cut, tunable, tested at two levels: 10% and 20%).
  - Risk unit = 2x ATR(14) as of the day before entry (no lookahead, same
    T-1 convention as gen_volprofile). A real stop sits under the trade for the
    whole hold -- this is not naked buy-and-hold.
  - Exit = stop hit (-1R) OR close of day `hold_days` trading days after entry,
    whichever comes first. hold_days in {3, 5, 10, 20}, per the brief.
  - Same LOCKED holdout split (75/25 by date, wf.date_split) and same
    concentration diagnostics (win rate, MEDIAN r -- not just mean, top-10-trade
    profit share) that caught the volume-profile mirage earlier today.

Outlier guard: |surprise%| > 500 dropped as EPS-near-zero-denominator data
artifacts (est $0.01 vs actual $0.13 = +1200%-type noise), not real signal -- 57
of 8254 raw events (0.7%).
"""
import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
import walkforward_search as wf

ROOT = Path("/home/heff/.openclaw/workspace")
SURPRISE_CACHE = ROOT / "data" / "pead_surprise_cache.json"
OUT_LEDGER = ROOT / "data" / "pead_wf_ledger.csv"
OUT_RESULT = ROOT / "data" / "pead_wf_result.json"
COST = wf.COST_BPS / 10000.0
MRF = wf.MIN_RISK_FRAC

SURPRISE_ARTIFACT_CAP = 500.0
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0


def load_surprises():
    d = json.loads(SURPRISE_CACHE.read_text())
    out = {}
    for t, rows in d["tickers"].items():
        clean = [r for r in rows if abs(r["surprise_pct"]) <= SURPRISE_ARTIFACT_CAP]
        if clean:
            out[t] = clean
    return out


def gen_pead(df, events, surprise_thresh, hold_days):
    if len(df) < ATR_PERIOD + 5:
        return []
    high = df["High"]; low = df["Low"]; close = df["Close"]; open_ = df["Open"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(ATR_PERIOD).mean().shift(1)
    idx = df.index
    dates = [d.date() for d in idx]
    O = open_.to_numpy(); H = high.to_numpy(); L = low.to_numpy(); C = close.to_numpy()
    ATR = atr.to_numpy()

    rows = []
    for ev in events:
        surp = ev["surprise_pct"]
        if abs(surp) < surprise_thresh:
            continue
        ev_date = pd.Timestamp(ev["date"]).date()
        entry_idx = None
        for i, d in enumerate(dates):
            if d > ev_date:
                entry_idx = i
                break
        if entry_idx is None or entry_idx == 0:
            continue
        if np.isnan(ATR[entry_idx]) or entry_idx + hold_days >= len(df):
            continue
        entry = float(O[entry_idx])
        if entry <= 0:
            continue
        risk = ATR_STOP_MULT * float(ATR[entry_idx])
        if risk <= 0 or risk / entry < wf.MIN_RISK_FRAC:
            continue
        side = "LONG" if surp > 0 else "SHORT"
        stop = entry - risk if side == "LONG" else entry + risk
        end = entry_idx + 1 + hold_days
        out = wf.sim_forward(side, entry, stop, None, H[entry_idx + 1:end], L[entry_idx + 1:end], C[entry_idx + 1:end])
        if out is not None:
            rows.append((str(dates[entry_idx]), side, out, risk / entry))
    return rows


def to_frame(rows):
    out = pd.DataFrame(rows, columns=["date", "side", "r_gross", "risk_frac"])
    if not out.empty:
        out["net_r"] = out["r_gross"] - COST / out["risk_frac"].clip(lower=MRF)
    else:
        out["net_r"] = []
    return out


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
        return "INSUFFICIENT SAMPLE / EVENT-STARVED (n={} < {})".format(h["n"], min_n)
    if h["per_trade"] <= 0:
        return "MIRAGE -- non-positive holdout expectancy ({:+.4f}R/tr)".format(h["per_trade"])
    med = diag.get("median_r")
    top10 = diag.get("top10_pct_of_profit")
    if med is not None and med <= 0 and top10 is not None and top10 > 50:
        return "MIRAGE -- median trade loses ({:+.3f}R) and profit concentrated ({:.0f}% top10) -- lottery-ticket shape, same as the volume-profile mirage".format(med, top10)
    if top10 is not None and top10 > 60:
        return "SUSPECT -- {:.0f}% of profit from top 10 trades".format(top10)
    if med is not None and med <= 0:
        return "SUSPECT -- positive mean but median trade loses ({:+.3f}R) -- not a comfortable hold".format(med)
    return "CARRIES -- positive on holdout, median trade holds up, not outlier-concentrated"


def main():
    surprises = load_surprises()
    syms = sorted(surprises.keys())
    cached = wf.load_daily_cached(syms, refresh=False)
    print("{}/{} symbols with daily bars cached".format(len(cached), len(syms)))
    cached = [(s, d) for s, d in cached if s in surprises]

    THRESHOLDS = [10.0, 20.0]
    HOLDS = [3, 5, 10, 20]
    CANDIDATES = [("PEAD surprise>={:.0f}% hold={}d".format(th, hd), th, hd)
                  for th in THRESHOLDS for hd in HOLDS]

    t0 = time.time()
    frames = {}
    for name, th, hd in CANDIDATES:
        rows = []
        for sym, df in cached:
            rows += gen_pead(df, surprises.get(sym, []), th, hd)
        frames[name] = to_frame(rows)
        print("  {:<40} {:>5} tr ({:.0f}s)".format(name, len(rows), time.time() - t0))

    all_dates = []
    for f in frames.values():
        all_dates += f["date"].tolist()
    if not all_dates:
        print("\nZERO trades from any candidate. Not proceeding.")
        return
    search, holdout = wf.date_split(all_dates)
    print("\ndate split: {} search days / {} locked holdout days".format(len(search), len(holdout)))

    print("\n" + "=" * 96)
    results = {}
    for name, _, _ in CANDIDATES:
        f = frames[name]
        results[name] = (sc(f, search), sc(f, holdout))
    print("SEARCH (in-sample, orientation only):")
    for name, (s, h) in results.items():
        print("  {:<40} {}".format(name, fmt(s)))
    print("\nLOCKED HOLDOUT:")
    for name, (s, h) in results.items():
        print("  {:<40} {}".format(name, fmt(h)))

    print("\nDIAGNOSTICS + VERDICT:")
    led = []
    for name, th, hd in CANDIDATES:
        f = frames[name]
        s, h = results[name]
        diag = concentration(f, holdout)
        v = verdict(h, diag)
        print("\n  {}: {}".format(name, v))
        print("    diagnostics: {}".format(json.dumps(diag)))
        led.append(dict(name=name, threshold_pct=th, hold_days=hd, verdict=v,
                         search_total_r=s["total_r"], search_n=s["n"],
                         search_per_trade=s["per_trade"], search_sharpe=s["sharpe"],
                         holdout_total_r=h["total_r"], holdout_n=h["n"],
                         holdout_per_trade=h["per_trade"], holdout_sharpe=h["sharpe"], **diag))

    pd.DataFrame(led).to_csv(OUT_LEDGER, index=False)
    OUT_RESULT.write_text(json.dumps(led, indent=2, default=str))
    print("\nwrote {}\nwrote {}".format(OUT_LEDGER, OUT_RESULT))


if __name__ == "__main__":
    main()
