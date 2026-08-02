#!/usr/bin/env python3
"""swing_sector_rotation_backtest.py -- RESEARCH ONLY, not wired into any live cron/promotion.

Tests sector rotation (the RRG quadrant computed by sector_rotation.py) as its OWN
standalone multi-day SWING strategy on the 11 SPDR sector ETFs, rather than as a same-day
gate feeding the intraday MR/ORB scanners. Uses the same locked 75/25 date-split +
concentration-diagnostics discipline as the rest of this codebase (see
_wf_probe_common.py / today's volume-profile mirage finding: win rate, MEDIAN trade
return, top-10 profit concentration).

Signal (from data/sector_rotation.csv, already computed by sector_rotation.py):
  quadrant per (date, sector_etf) -- Leading / Improving / Weakening / Lagging.

Entry: the trading day the quadrant TRANSITIONS INTO "Improving" (money rotating in
       from a weak base -- textbook RRG entry). Executes at the NEXT trading day's
       Open (signal only knowable after the flip day's close -- no lookahead).
Exit:  (a) fixed holding periods -- 5 / 10 / 20 trading days, Close-to-Close.
       (b) adaptive -- hold while quadrant stays "Improving"; exit at the Close of
           the first day it's no longer Improving. Trades still open at the end of
           the data (right-censored) are DROPPED, not guessed.

Benchmark: SPY return over the IDENTICAL entry/exit window per trade (apples-to-apples
       matched-duration comparison), not just "SPY over the whole backtest period".

No R-multiples: there's no defined stop-loss for a multi-day ETF swing hold, so returns
are raw % change entry->exit (documented simplification -- "simplest version first" per
the research ask). Diagnostics (win rate, median, top-10 concentration) applied exactly
the same way regardless of units.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
ROTATION_CSV = ROOT / "data" / "sector_rotation.csv"
ETF_BARS_PATH = ROOT / "data" / "sector_etf_daily.parquet"
SEARCH_FRAC = 0.75   # identical split convention to walkforward_search.py
MIN_TRADES = 30      # same quality guard used elsewhere in this codebase

SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]
BENCHMARK = "SPY"
HOLD_PERIODS = [5, 10, 20]


def load_data():
    rot = pd.read_csv(ROTATION_CSV, dtype={"date": str})
    bars = pd.read_parquet(ETF_BARS_PATH)
    bars["t"] = pd.to_datetime(bars["t"]).dt.date.astype(str)
    return rot, bars


def build_price_frames(bars):
    """symbol -> DataFrame indexed by date string, sorted, with Open/Close."""
    out = {}
    for sym, g in bars.groupby("symbol"):
        g = g.sort_values("t").set_index("t")
        out[sym] = g[["Open", "Close"]]
    return out


def find_entries(rot, etf):
    """Dates where quadrant transitions INTO 'Improving' for this etf."""
    g = rot[rot["sector_etf"] == etf].sort_values("date").reset_index(drop=True)
    g = g.dropna(subset=["quadrant"])
    entries = []
    prev_q = None
    for row in g.itertuples():
        if row.quadrant == "Improving" and prev_q is not None and prev_q != "Improving":
            entries.append(row.date)
        prev_q = row.quadrant
    return entries, g


def trading_days_after(price_index, date, n):
    """Index position n trading days after `date` in a sorted date-string index. None if OOB."""
    try:
        pos = price_index.get_loc(date)
    except KeyError:
        # date not itself a trading day for this symbol (shouldn't happen, ETF bars are dense)
        later = [i for i, d in enumerate(price_index) if d > date]
        if not later:
            return None
        pos = later[0] - 1
    tgt = pos + n
    if tgt >= len(price_index):
        return None
    return price_index[tgt]


def build_trades(rot, price_frames, mode, n=None):
    """mode: 'fixed' (n trading days) or 'adaptive' (hold while quadrant stays Improving).
    Entry executes at NEXT trading day's Open after the flip-day close. Returns a list of
    dict(etf, entry_date, exit_date, entry_px, exit_px, ret, spy_ret, alpha)."""
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
            # entry = next trading day's Open in this ETF's own price series
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
                # walk forward from entry_date (inclusive) in the QUADRANT series (not
                # necessarily identical dates to px, but sector_rotation.csv is built from
                # the same Alpaca bars so they should line up) until quadrant != Improving
                exit_date = None
                q_dates = list(gq.index)
                try:
                    qpos0 = q_dates.index(entry_date)
                except ValueError:
                    # entry_date not in quadrant index (e.g. trailing partial day) -- skip
                    continue
                for qpos in range(qpos0, len(q_dates)):
                    if gq.iloc[qpos] != "Improving":
                        exit_date = q_dates[qpos]
                        break
                if exit_date is None:
                    continue  # right-censored (still Improving at end of data) -- drop
                if exit_date not in idx:
                    continue
                exit_px = px["Close"].loc[exit_date]
            else:
                raise ValueError(mode)

            if entry_date not in spy_idx or exit_date not in spy_idx:
                continue
            spy_entry = spy_px["Open"].loc[entry_date] if mode == "fixed" or True else None
            spy_exit = spy_px["Close"].loc[exit_date]
            spy_entry = spy_px["Open"].loc[entry_date]

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
    total_net = df["ret"].sum()  # matches _wf_probe_common.py's convention: denominator is
                                  # the FULL net sum (all trades), not just winners' sum
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
    rot, bars = load_data()
    price_frames = build_price_frames(bars)
    all_dates = sorted(price_frames[BENCHMARK].index)
    search_dates, holdout_dates = date_split(all_dates)
    print("total trading days: {}  search: {} ({})  holdout: {} ({})".format(
        len(all_dates), len(search_dates), min(search_dates) if search_dates else None,
        len(holdout_dates), min(holdout_dates) if holdout_dates else None))
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
        # per-etf event counts in holdout, honesty check on sample thinness
        if not h_trades.empty:
            per_etf = h_trades.groupby("etf").size().to_dict()
            print("  holdout events per ETF: {}".format(per_etf))

    # whole-period SPY buy-and-hold context (search vs holdout windows)
    spy = price_frames[BENCHMARK]
    def bh_return(dates):
        ds = sorted(dates)
        if len(ds) < 2:
            return None
        return float((spy["Close"].loc[ds[-1]] - spy["Open"].loc[ds[0]]) / spy["Open"].loc[ds[0]])
    print("\nSPY buy-and-hold, whole search window:  {:+.3f}".format(bh_return(search_dates) or 0))
    print("SPY buy-and-hold, whole holdout window: {:+.3f}".format(bh_return(holdout_dates) or 0))

    out = {name: r for name, r in results.items()}
    (ROOT / "data" / "swing_sector_rotation_result.json").write_text(json.dumps(out, indent=2, default=str))
    print("\nwrote data/swing_sector_rotation_result.json")


if __name__ == "__main__":
    main()
