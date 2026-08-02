#!/usr/bin/env python3
"""validate_riskoff_20260617.py — the 2026-06-17 case test for the intraday risk-off gate.

The walkforward proves the gate carries on the 2yr LOCKED HOLDOUT. This script proves the
OTHER thing the handoff demands: that the gate would actually have SUPPRESSED yesterday's
bad signals — the 27 LONG fades into the rate-hike selloff that produced 14 straight stops.

It rebuilds 2026-06-17's intraday market proxy the SAME way the backtest builds it
(cross-sectional mean return-since-open across the S&P-100, per 5-min bar), looks up the
proxy at each trigger's entry bar, and — joining the realized R/$ from paper_trades.csv —
reports, for each candidate threshold, how many of the 27 it blocks and how much realized
loss that would have avoided vs. how much winning R it would have given up.

Network-heavy (fetches the universe's 6/17 bars) -> run AFTER market close.
Run: ./venv/bin/python validate_riskoff_20260617.py
"""
import csv
import json
from pathlib import Path
from datetime import date, datetime

import numpy as np
import pandas as pd

import mean_reversion_scanner as mrs

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DAY = date(2026, 6, 17)
TRIGGERS = DATA / f"mr_triggers_{DAY.isoformat()}.jsonl"
PAPER = DATA / "paper_trades.csv"
THRESHOLDS = [-0.005, -0.0075, -0.010, -0.015]


def load_triggers():
    rows = []
    for line in TRIGGERS.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_outcomes():
    """trade_id -> (outcome_r, dollar_pnl) from the evaluated paper CSV."""
    out = {}
    if PAPER.exists():
        with PAPER.open() as f:
            for r in csv.DictReader(f):
                try:
                    out[r["trade_id"]] = (float(r["outcome_r"]), float(r.get("dollar_pnl") or 0))
                except Exception:
                    pass
    return out


def build_day_proxy():
    """proxy[Timestamp(5-min, naive ET)] = cross-sectional mean return-since-open on 6/17."""
    acc = {}  # ts -> [sum, count]
    syms = mrs.fetch_sp100()
    got = 0
    for tk in syms:
        try:
            df = mrs.get_5m_data(tk, days=5)
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        if df.index.tz is not None:
            df.index = df.index.tz_convert(mrs.ET).tz_localize(None)
        d = df[df.index.date == DAY]
        if d.empty:
            continue
        o0 = float(d["Open"].iloc[0])
        if o0 <= 0:
            continue
        got += 1
        for ts, c in d["Close"].items():
            if pd.isna(c):
                continue
            a = acc.setdefault(ts.floor("5min"), [0.0, 0])
            a[0] += float(c) / o0 - 1.0
            a[1] += 1
    proxy = {ts: s / n for ts, (s, n) in acc.items() if n > 0}
    return proxy, got


def proxy_at(proxy, entry_time):
    """Proxy value at the 5-min bar containing entry_time (the bar the decision saw)."""
    ts = pd.Timestamp(entry_time).floor("5min")
    if ts in proxy:
        return float(proxy[ts]), ts
    # fall back to the most recent prior bar
    prior = [k for k in proxy if k <= ts]
    if prior:
        k = max(prior)
        return float(proxy[k]), k
    return None, ts


def main():
    trigs = load_triggers()
    outc = load_outcomes()
    proxy, got = build_day_proxy()
    print(f"6/17 proxy built from {got} symbols, {len(proxy)} 5-min bars\n")

    # annotate each trigger with its proxy reading + realized outcome
    enriched = []
    for t in trigs:
        pv, bar = proxy_at(proxy, t["entry_time"])
        r, d = outc.get(t["trade_id"], (None, None))
        enriched.append({**t, "proxy": pv, "bar": str(bar), "r": r, "dollar": d})

    print(f"{'ticker':<6} {'side':<5} {'entry_time':<20} {'mkt%':>7} {'R':>7} {'$':>7}")
    for e in sorted(enriched, key=lambda x: x["entry_time"]):
        pv = f"{e['proxy']*100:+.2f}" if e["proxy"] is not None else "  n/a"
        rr = f"{e['r']:+.2f}" if e["r"] is not None else "   --"
        dd = f"{e['dollar']:+.2f}" if e["dollar"] is not None else "   --"
        print(f"{e['ticker']:<6} {e['side']:<5} {e['entry_time']:<20} {pv:>7} {rr:>7} {dd:>7}")

    print("\nthreshold sweep — LONG fires blocked when market <= threshold at the entry bar:")
    print(f"{'thresh':>8} {'blocked':>8} {'kept':>6} | {'R avoided':>10} {'R kept':>8} | "
          f"{'$ avoided':>10} {'$ kept':>8}")
    have = [e for e in enriched if e["proxy"] is not None and e["r"] is not None and e["side"] == "LONG"]
    for thr in THRESHOLDS:
        blk = [e for e in have if e["proxy"] <= thr]
        kep = [e for e in have if e["proxy"] > thr]
        r_avoid = -sum(e["r"] for e in blk)      # negate: R we would NOT have taken
        d_avoid = -sum(e["dollar"] for e in blk)
        r_kept = sum(e["r"] for e in kep)
        d_kept = sum(e["dollar"] for e in kep)
        print(f"{thr*100:>7.2f}% {len(blk):>8} {len(kep):>6} | {r_avoid:>+10.2f} {r_kept:>+8.2f} | "
              f"{d_avoid:>+10.2f} {d_kept:>+8.2f}")

    tot_r = sum(e["r"] for e in have)
    tot_d = sum(e["dollar"] for e in have)
    print(f"\nUNFILTERED 6/17 LONG total (evaluated): {tot_r:+.2f}R / ${tot_d:+.2f} over {len(have)} fires")
    print("Read: a good threshold turns a big chunk of 'R avoided' positive (cuts losers) "
          "while keeping 'R kept' from collapsing (doesn't kill the day's few winners).")


if __name__ == "__main__":
    main()
