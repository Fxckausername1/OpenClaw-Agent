#!/usr/bin/env python3
"""swing_sector_rotation_backtest_v2.py -- RESEARCH ONLY, corrected re-run.

v1 (swing_sector_rotation_backtest.py) used the production data/sector_rotation.csv +
data/sector_etf_daily.parquet, both built with Alpaca adjustment=raw. That data has a
REAL, confirmed data bug: State Street executed a 2-for-1 share split on the SPDR sector
ETFs effective 2025-12-05, and unadjusted prices show a fake ~50% one-day drop for every
affected ETF on that date -- which also corrupts the rs_zscore/rs_mom/quadrant computation
for ~1 quarter afterward (RS_WINDOW=63 trading days), right in the middle of the locked
holdout window (2025-12-30 .. 2026-07-09). v1's results are not trustworthy.

This version re-derives everything from data/sector_etf_daily_SPLITADJ_research.parquet
(fetched fresh with adjustment=split, a separate file -- does NOT touch any production
file) and recomputes rs_zscore/rs_mom/quadrant in-memory using the IDENTICAL formulas
from sector_rotation.py (imported, not re-implemented) rather than trusting the
possibly-corrupted data/sector_rotation.csv.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

import sector_rotation as sr

ROOT = Path(__file__).resolve().parent
SPLITADJ_PATH = ROOT / "data" / "sector_etf_daily_SPLITADJ_research.parquet"
SEARCH_FRAC = 0.75
MIN_TRADES = 30
SECTOR_ETFS = sr.SECTOR_ETFS
BENCHMARK = sr.BENCHMARK


def recompute_rotation_inmemory(bars):
    """Same math as sector_rotation.compute_rotation() but no CSV write (in-memory only,
    keeps this research run from touching the production file)."""
    wide = bars.pivot(index="t", columns="symbol", values="Close").sort_index()
    rows = []
    for etf in SECTOR_ETFS:
        rs = wide[etf] / wide[BENCHMARK]
        rs_mean = rs.rolling(sr.RS_WINDOW).mean()
        rs_std = rs.rolling(sr.RS_WINDOW).std()
        rs_z = (rs - rs_mean) / rs_std
        rs_mom = rs_z - rs_z.shift(sr.MOM_WINDOW)
        for dt, z, m in zip(wide.index, rs_z, rs_mom):
            q = sr.classify_quadrant(z, m)
            rows.append((str(dt), etf, z, m, q))
    return pd.DataFrame(rows, columns=["date", "sector_etf", "rs_zscore", "rs_mom", "quadrant"])


def build_price_frames(bars):
    out = {}
    for sym, g in bars.groupby("symbol"):
        g = g.sort_values("t").set_index("t")
        out[sym] = g[["Open", "Close"]]
    return out


def find_entries(rot, etf):
    g = rot[rot["sector_etf"] == etf].sort_values("date").reset_index(drop=True)
    g = g.dropna(subset=["quadrant"])
    entries = []
    prev_q = None
    for row in g.itertuples():
        if row.quadrant == "Improving" and prev_q is not None and prev_q != "Improving":
            entries.append(row.date)
        prev_q = row.quadrant
    return entries, g


def build_trades(rot, price_frames, mode, n=None):
    spy_px = price_frames[BENCHMARK]
    spy_idx = spy_px.index
    trades = []
    for etf in SECTOR_ETFS:
        if etf not in price_frames:
            continue
        px = price_frames[etf]
        idx = px.index
        entries, g = find_entries(rot, etf)
        gq = g.set_index("date")["quadrant"]
        for flip_date in entries:
            try:
                fpos = idx.get_loc(flip_date)
            except KeyError:
                continue
            epos = fpos + 1
            if epos >= len(idx):
                continue
            entry_date = idx[epos]
            entry_px = px["Open"].iloc[epos]

            if mode == "fixed":
                xpos = epos + n
                if xpos >= len(idx):
                    continue
                exit_date = idx[xpos]
                exit_px = px["Close"].iloc[xpos]
            elif mode == "adaptive":
                exit_date = None
                q_dates = list(gq.index)
                try:
                    qpos0 = q_dates.index(entry_date)
                except ValueError:
                    continue
                for qpos in range(qpos0, len(q_dates)):
                    if gq.iloc[qpos] != "Improving":
                        exit_date = q_dates[qpos]
                        break
                if exit_date is None:
                    continue
                if exit_date not in idx:
                    continue
                exit_px = px["Close"].loc[exit_date]
            else:
                raise ValueError(mode)

            if entry_date not in spy_idx or exit_date not in spy_idx:
                continue
            spy_entry = spy_px["Open"].loc[entry_date]
            spy_exit = spy_px["Close"].loc[exit_date]

            ret = (exit_px - entry_px) / entry_px
            spy_ret = (spy_exit - spy_entry) / spy_entry
            trades.append(dict(etf=etf, flip_date=flip_date, entry_date=entry_date,
                                exit_date=exit_date, entry_px=entry_px, exit_px=exit_px,
                                ret=ret, spy_ret=spy_ret, alpha=ret - spy_ret,
                                hold_days=(idx.get_loc(exit_date) - idx.get_loc(entry_date))))
    return pd.DataFrame(trades)


def date_split(all_dates):
    ds = sorted(set(all_dates))
    cut = int(len(ds) * SEARCH_FRAC)
    return set(ds[:cut]), set(ds[cut:])


def score(df, label):
    if df.empty:
        return dict(label=label, n=0)
    n = len(df)
    win_rate = float((df["ret"] > 0).mean())
    mean_ret = float(df["ret"].mean())
    median_ret = float(df["ret"].median())
    mean_alpha = float(df["alpha"].mean())
    median_alpha = float(df["alpha"].median())
    win_rate_vs_spy = float((df["alpha"] > 0).mean())
    daily = df.groupby("entry_date")["ret"].mean()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252 / df["hold_days"].mean())) \
        if daily.std() > 0 and df["hold_days"].mean() > 0 else 0.0
    sorted_by_ret = df.sort_values("ret", ascending=False)
    topn = min(10, n)
    total_net = df["ret"].sum()
    top_sum = sorted_by_ret.head(topn)["ret"].sum()
    top_pct = round(float(top_sum / total_net * 100), 1) if total_net != 0 else None
    return dict(label=label, n=n, win_rate=round(win_rate, 3), mean_ret=round(mean_ret, 4),
                median_ret=round(median_ret, 4), mean_alpha_vs_spy=round(mean_alpha, 4),
                median_alpha_vs_spy=round(median_alpha, 4),
                win_rate_vs_spy=round(win_rate_vs_spy, 3),
                sharpe_approx=round(sharpe, 2),
                top10_pct_of_profit=top_pct,
                total_ret_sum=round(float(df["ret"].sum()), 3))


def verdict(search_s, hold_s):
    if hold_s["n"] < MIN_TRADES:
        return "INSUFFICIENT-DATA (holdout n={} < {} min trades)".format(hold_s["n"], MIN_TRADES)
    if hold_s["mean_ret"] <= 0:
        return "MIRAGE -- holdout mean return <= 0 ({:+.4f})".format(hold_s["mean_ret"])
    if hold_s["median_ret"] < 0:
        return "MIRAGE -- holdout mean positive but MEDIAN trade loses ({:+.4f})".format(hold_s["median_ret"])
    if hold_s["top10_pct_of_profit"] is not None and hold_s["top10_pct_of_profit"] > 50:
        return "MIRAGE -- {:.0f}% of holdout profit from top-10 trades".format(hold_s["top10_pct_of_profit"])
    if hold_s["mean_alpha_vs_spy"] <= 0:
        return "MIRAGE -- does not beat matched-duration SPY buy-and-hold on holdout ({:+.4f} alpha)".format(
            hold_s["mean_alpha_vs_spy"])
    return "CARRIES -- positive holdout, median wins, not top-10 concentrated, beats SPY"


def main():
    bars = pd.read_parquet(SPLITADJ_PATH)
    bars["t"] = pd.to_datetime(bars["t"]).dt.date.astype(str)
    rot = recompute_rotation_inmemory(bars)
    cov = rot.dropna(subset=["quadrant"])
    print("recomputed quadrants on split-adjusted data: {} rows, {} dates covered".format(
        len(rot), cov["date"].nunique()))

    price_frames = build_price_frames(bars)
    all_dates = sorted(price_frames[BENCHMARK].index)
    search_dates, holdout_dates = date_split(all_dates)
    print("total trading days: {}  search: {}  holdout: {}".format(
        len(all_dates), len(search_dates), len(holdout_dates)))
    print("holdout spans: {} .. {}".format(min(holdout_dates), max(holdout_dates)))

    results = {}
    variants = [("fixed_5d", "fixed", 5), ("fixed_10d", "fixed", 10),
                ("fixed_20d", "fixed", 20), ("adaptive_hold_while_improving", "adaptive", None)]

    for name, mode, n in variants:
        trades = build_trades(rot, price_frames, mode, n)
        if trades.empty:
            print("\n=== {} === NO TRADES GENERATED".format(name))
            continue
        s_trades = trades[trades["entry_date"].isin(search_dates)]
        h_trades = trades[trades["entry_date"].isin(holdout_dates)]
        s_score = score(s_trades, "search")
        h_score = score(h_trades, "holdout")
        v = verdict(s_score, h_score)
        results[name] = dict(search=s_score, holdout=h_score, verdict=v, n_total=len(trades))
        print("\n=== {} === (n_total={}, n_search={}, n_holdout={})".format(
            name, len(trades), len(s_trades), len(h_trades)))
        print("  SEARCH : {}".format(s_score))
        print("  HOLDOUT: {}".format(h_score))
        print("  VERDICT: {}".format(v))
        if not h_trades.empty:
            per_etf = h_trades.groupby("etf").size().to_dict()
            print("  holdout events per ETF: {}".format(per_etf))
        # sanity: any remaining suspicious single-day moves in this variant's trades?
        extreme = trades[trades["ret"].abs() > 0.15]
        if len(extreme):
            print("  ** {} trades with |ret|>15% (verify not data artifacts):".format(len(extreme)))
            print(extreme[["etf", "entry_date", "exit_date", "ret"]].to_string(index=False))

    spy = price_frames[BENCHMARK]
    def bh_return(dates):
        ds = sorted(dates)
        if len(ds) < 2:
            return None
        return float((spy["Close"].loc[ds[-1]] - spy["Open"].loc[ds[0]]) / spy["Open"].loc[ds[0]])
    print("\nSPY buy-and-hold, whole search window:  {:+.3f}".format(bh_return(search_dates) or 0))
    print("SPY buy-and-hold, whole holdout window: {:+.3f}".format(bh_return(holdout_dates) or 0))

    (ROOT / "data" / "swing_sector_rotation_result_v2_splitadj.json").write_text(
        json.dumps(results, indent=2, default=str))
    print("\nwrote data/swing_sector_rotation_result_v2_splitadj.json")


if __name__ == "__main__":
    main()
