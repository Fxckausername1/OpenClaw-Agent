#!/usr/bin/env python3
"""Analyze the deep-backtest trades to find what drives losses.

Reads data/deep_trades_rich.csv and breaks the 605 trades down by exit reason,
time of day, day of week, setup stretch (z / RSI), planned R:R, and hold time —
then runs a few principled 'what if we filtered X' experiments. Honest about
overfitting: only a handful of a-priori-sensible cuts, not a data-mine.
"""
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent
CSV = ROOT / "data" / "deep_trades_rich.csv"
DOW = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}


def line(label, rs):
    rs = np.asarray(rs, float)
    if len(rs) == 0:
        return f"  {label:<16} —"
    wins = (rs > 0).sum()
    return (f"  {label:<16} {len(rs):>4} tr | win {wins/len(rs)*100:4.0f}% | "
            f"avg {rs.mean():+.3f}R | total {rs.sum():+7.1f}R")


def by(df, col, label, order=None):
    print(f"\n== by {label} ==")
    keys = order if order else sorted(df[col].dropna().unique())
    for k in keys:
        sub = df[df[col] == k]
        if len(sub):
            print(line(str(k), sub["outcome_r"].values))


def main():
    df = pd.read_csv(CSV)
    df["mins"] = df["entry_time"].str[:2].astype(int) * 60 + df["entry_time"].str[3:5].astype(int)
    df["time_block"] = pd.cut(df["mins"], [569, 630, 720, 840, 960],
                              labels=["open 9:30-10:30", "mid-am 10:30-12", "mid-pm 12-14", "late 14-16"])
    df["abs_z"] = df["z0"].abs()
    df["z_bucket"] = pd.cut(df["abs_z"], [0, 2.25, 2.6, 3.0, 99],
                            labels=["2.0-2.25", "2.25-2.6", "2.6-3.0", "3.0+"])
    df["rr_bucket"] = pd.cut(df["planned_rr"], [0, 2, 3, 5, 999],
                             labels=["1.5-2", "2-3", "3-5", "5+"])
    df["hold_bucket"] = pd.cut(df["hold_bars"], [0, 3, 10, 30, 9999],
                               labels=["1-3 bars", "4-10", "11-30", "30+"])
    df["dow_name"] = df["dow"].map(DOW)
    df["year"] = df["date"].str[:4]

    rs = df["outcome_r"].values
    print("=" * 64)
    print(line("ALL", rs))
    print(line("  LONG", df[df.side == "LONG"]["outcome_r"]))
    print(line("  SHORT", df[df.side == "SHORT"]["outcome_r"]))

    by(df, "exit_reason", "exit reason",
       order=["target", "eod_win", "eod_loss", "stop"])
    by(df, "time_block", "time of day (entry)",
       order=["open 9:30-10:30", "mid-am 10:30-12", "mid-pm 12-14", "late 14-16"])
    by(df, "dow_name", "day of week", order=["Mon", "Tue", "Wed", "Thu", "Fri"])
    by(df, "z_bucket", "stretch depth (|z|)",
       order=["2.0-2.25", "2.25-2.6", "2.6-3.0", "3.0+"])
    by(df, "rr_bucket", "planned R:R", order=["1.5-2", "2-3", "3-5", "5+"])
    by(df, "hold_bucket", "hold time", order=["1-3 bars", "4-10", "11-30", "30+"])

    print("\n" + "=" * 64)
    print("LOSS ANATOMY")
    losers = df[df.outcome_r <= 0]
    print(f"  {len(losers)} losing trades, {losers['outcome_r'].sum():.1f}R lost total")
    print("  loss by exit reason:")
    for k in ["stop", "eod_loss"]:
        sub = losers[losers.exit_reason == k]
        print(f"    {k:<10} {len(sub):>4} trades, {sub['outcome_r'].sum():+.1f}R "
              f"({len(sub)/len(losers)*100:.0f}% of losers)")

    print("\n" + "=" * 64)
    print("FILTER EXPERIMENTS (apply to ALL trades; watch trade-count drop)")

    def show(name, mask):
        sub = df[mask]
        print(line(name, sub["outcome_r"].values))

    show("baseline", df.index == df.index)
    show("drop open 30m", df["mins"] >= 630)
    show("drop late >14:00", df["mins"] < 840)
    show("|z| >= 2.6 only", df["abs_z"] >= 2.6)
    show("planned_rr <= 3", df["planned_rr"] <= 3)
    show("drop Mondays", df["dow"] != 0)
    show("combo: >=10:00 & |z|>=2.6", (df["mins"] >= 630) & (df["abs_z"] >= 2.6))
    show("combo: >=10:00 & rr<=3", (df["mins"] >= 630) & (df["planned_rr"] <= 3))


if __name__ == "__main__":
    main()
