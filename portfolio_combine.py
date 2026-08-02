#!/usr/bin/env python3
"""Combine mean-reversion + sharpened ORB into one portfolio and measure the
diversification benefit (the whole point of the strategy lab).

Builds each strategy's daily summed-R series, computes a risk-adjusted score
(annualized Sharpe on daily R) and max drawdown on the cumulative-R equity
curve, then blends them 50/50 at EQUAL RISK (each scaled to unit daily vol) and
shows the combined Sharpe + drawdown vs the standalones. Negative correlation
means the blend should beat either alone on risk-adjusted terms.
"""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ANN = np.sqrt(252)


def daily_series(csv, rcol):
    df = pd.read_csv(csv)
    return df.groupby("date")[rcol].sum()


def sharpe(s):
    return (s.mean() / s.std() * ANN) if s.std() > 0 else 0.0


def max_dd(s):
    eq = s.cumsum()
    return float((eq - eq.cummax()).min())


def describe(name, s):
    return (f"{name:<22} Sharpe {sharpe(s):+5.2f} | totalR {s.sum():+8.1f} "
            f"| daily {s.mean():+.3f}±{s.std():.3f}R | maxDD {max_dd(s):+7.1f}R "
            f"| {len(s[s != 0])} active days")


def main():
    mr = daily_series(DATA / "deep_trades_rich.csv", "outcome_r")

    osharp = pd.read_csv(DATA / "orb_sharp_trades.csv")
    osharp = osharp[(osharp["vwap"]) & (osharp["vol"])]
    orb = osharp.groupby("date")["r"].sum()

    days = sorted(set(mr.index) | set(orb.index))
    mr = mr.reindex(days, fill_value=0.0)
    orb = orb.reindex(days, fill_value=0.0)

    corr = np.corrcoef(mr.values, orb.values)[0, 1]

    # equal-risk blend: scale each to unit daily vol, 50/50
    mr_u = mr / mr.std()
    orb_u = orb / orb.std()
    blend = 0.5 * (mr_u + orb_u)

    print("=" * 78)
    print("PORTFOLIO: mean reversion + sharpened ORB (VWAP+VOL)")
    print(f"{len(days)} trading days | daily-return correlation: {corr:+.3f}")
    print("-" * 78)
    print("STANDALONE (raw daily R):")
    print("  " + describe("mean reversion", mr))
    print("  " + describe("sharpened ORB", orb))
    print("-" * 78)
    print("EQUAL-RISK 50/50 BLEND (each scaled to unit daily vol):")
    print(f"  mean reversion alone   Sharpe {sharpe(mr_u):+5.2f} | maxDD {max_dd(mr_u):+6.2f} (vol units)")
    print(f"  sharpened ORB alone    Sharpe {sharpe(orb_u):+5.2f} | maxDD {max_dd(orb_u):+6.2f}")
    print(f"  >> COMBINED 50/50      Sharpe {sharpe(blend):+5.2f} | maxDD {max_dd(blend):+6.2f}")
    best = max(sharpe(mr_u), sharpe(orb_u))
    lift = (sharpe(blend) / best - 1) * 100 if best > 0 else 0
    print("-" * 78)
    print(f"Diversification result: combined Sharpe {sharpe(blend):.2f} vs best standalone "
          f"{best:.2f}  ->  {lift:+.0f}%")
    print(f"Drawdown: combined maxDD {max_dd(blend):.2f} vs mean-rev {max_dd(mr_u):.2f} / "
          f"ORB {max_dd(orb_u):.2f} (vol units)")


if __name__ == "__main__":
    main()
