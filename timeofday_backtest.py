#!/usr/bin/env python3
"""timeofday_backtest.py — Backtest B: time-of-day expectancy slicing.

Buckets the LIVE edges' (mean-rev z=1.5 + sharpened ORB) per-trade net R by entry
time-of-day, computed on the SEARCH region only, finds the best contiguous 90-min window
there, then validates THAT window ONCE on the locked HOLDOUT — so the winning window is
never cherry-picked on the data it's judged on (the same anti-mirage discipline as
walkforward_search). Net-of-cost (6bp), same cache + conventions.

Requires gen_mean_rev / gen_orb to support collect_time=True (5th tuple field = entry
minute-of-day). Run AFTER market close. Run: ./venv/bin/python timeofday_backtest.py
"""
import sys
import subprocess

import numpy as np
import pandas as pd

import walkforward_search as wf

WIN_MIN = 90          # candidate trading-window width (minutes)
STEP = 30             # slide the window start every 30 min
OPEN_MIN = 9 * 60 + 30   # 09:30 ET = 570
CLOSE_MIN = 16 * 60      # 16:00 ET = 960
TG_TARGET = "7590346809"


def hhmm(m):
    return f"{int(m)//60:02d}:{int(m)%60:02d}"


def tg(msg):
    try:
        subprocess.run(["/usr/bin/openclaw", "message", "send", "--channel", "telegram",
                        "--target", TG_TARGET, "--message", msg], timeout=40, check=False)
    except Exception:
        pass


def collect():
    syms = wf.mr.fetch_sp100()
    cached = wf.load_cached(syms)
    if not cached:
        raise SystemExit("no cached symbols; build the wf_cache first")
    mr_p = wf.comp_mr("mr", z=1.5, max_price=250.0)["p"]
    orb_p = wf.comp_orb("orb")["p"]
    rows = []
    for sym, df in cached:
        for r in wf.gen_mean_rev(df, mr_p, collect_time=True):
            rows.append(("mr",) + r)
        for r in wf.gen_orb(df, orb_p, collect_time=True):
            rows.append(("orb",) + r)
    t = pd.DataFrame(rows, columns=["strat", "date", "side", "r_gross", "risk_frac", "emin"])
    t["net_r"] = t["r_gross"] - (wf.COST_BPS / 10000.0) / t["risk_frac"].clip(lower=wf.MIN_RISK_FRAC)
    return t


def stats(df):
    n = len(df)
    if not n:
        return dict(n=0, per=0.0, tot=0.0, wr=0.0)
    r = df["net_r"]
    return dict(n=n, per=float(r.mean()), tot=float(r.sum()),
                wr=float((r > 0).mean() * 100))


def analyze(t, label):
    """Time-of-day window analysis for ONE strategy's trades. Best 90-min window picked
    on SEARCH, confirmed ONCE on the locked HOLDOUT."""
    out = [f"\n===== {label} — {len(t)} trades ====="]
    if len(t) < wf.MIN_TRADES:
        out.append("too few trades; skipped.")
        return "\n".join(out)
    search, holdout = wf.date_split(t["date"].tolist())
    ts = t[t["date"].isin(search)]
    th = t[t["date"].isin(holdout)]

    out.append("SEARCH net-R by 30-min bucket (entry ET):")
    tb = ts.assign(bucket=(ts["emin"] // 30 * 30).astype(int))
    g = tb.groupby("bucket")["net_r"].agg(["count", "sum", "mean"])
    for b, row in g.iterrows():
        bar = "#" * max(0, int(row["mean"] * 50))
        out.append(f"  {hhmm(b)}-{hhmm(b + 30)}  {int(row['count']):>5} tr  "
                   f"{row['sum']:>+7.1f}R  {row['mean']:>+.3f}R/tr {bar}")

    best = None
    for start in range(OPEN_MIN, CLOSE_MIN - WIN_MIN + 1, STEP):
        win = ts[(ts["emin"] >= start) & (ts["emin"] < start + WIN_MIN)]
        if len(win) < wf.MIN_TRADES:
            continue
        s = stats(win)
        if best is None or s["per"] > best[1]["per"]:
            best = (start, s)
    if best is None:
        out.append("no 90-min window cleared the min-trade floor; inconclusive.")
        return "\n".join(out)
    start, s_search = best
    base_s = stats(ts); base_h = stats(th)
    win_h = stats(th[(th["emin"] >= start) & (th["emin"] < start + WIN_MIN)])
    out.append(f"Best SEARCH window: {hhmm(start)}–{hhmm(start + WIN_MIN)} ET | "
               f"{s_search['n']} tr | {s_search['per']:+.3f}R/tr (all-day {base_s['per']:+.3f})")
    out.append(f"HOLDOUT window: {win_h['n']} tr | {win_h['per']:+.3f}R/tr | win {win_h['wr']:.0f}% "
               f"vs all-day {base_h['per']:+.3f}R")
    carried = win_h["per"] > base_h["per"] and win_h["n"] >= wf.MIN_TRADES
    out.append(f"verdict: {'✅ window carried (beats all-day OOS)' if carried else '⚠️ did NOT carry'}")
    return "\n".join(out)


def main():
    t = collect()
    print(f"collected {len(t)} timed trades "
          f"(mr={int((t['strat']=='mr').sum())}, orb={int((t['strat']=='orb').sum())})")
    blocks = []
    for strat, label in [("mr", "MEAN-REVERSION (z=1.5)"), ("orb", "ORB"), (None, "COMBINED")]:
        sub = t if strat is None else t[t["strat"] == strat]
        blocks.append(analyze(sub, label))
    msg = "🕒 Time-of-day backtest (B) — per strategy" + "\n".join(blocks)
    print("\n" + msg)
    tg(msg)


if __name__ == "__main__":
    main()
