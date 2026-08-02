#!/usr/bin/env python3
"""market_regime.py — classifies the prevailing SPY regime as TRENDING or MEAN_REVERTING,
for any future strategy/generator that wants to condition on it (the same idea behind heff's
own discretionary 50/200-MA workflow, see the REJECTED "Scanner #3" memory note, and the
regime->strategy map in GAMMA_PLAYBOOK.md: POS-gamma -> mean-reversion, NEG-gamma -> ORB/
momentum).

METHODOLOGY (Phase 3, write-only, 2026-06-29 — NOT yet wired into any live scanner):
  1. Efficiency ratio (Kaufman's ER): |net displacement| / (sum of |daily moves|) over the
     lookback window. ER -> 1 means price moved efficiently in one direction (trending);
     ER -> 0 means lots of back-and-forth with little net progress (mean-reverting/choppy).
     This is the actual trend-vs-chop axis — realized vol alone only measures MAGNITUDE, not
     character (a quiet trend and a violent chop can have the same vol).
  2. Realized volatility: rolling annualized stdev of SPY daily log returns. Logged for
     context, not currently part of the classification rule.
  3. VIX level (reuses fetch_vix_vvix from options_orchestrator.py rather than reinventing
     the yfinance pull) — informational only, same reason.

CLASSIFICATION: ER >= ER_THRESHOLD -> TRENDING, else MEAN_REVERTING. Deliberately a single,
inspectable rule rather than a fitted model. ER_THRESHOLD is a PLACEHOLDER, not backtest/
holdout-validated — per heff's explicit Phase 3 instruction this is write-only, the real
screen has not been run against market data, only --selftest (synthetic series) has.

NOT wired into mean_reversion_scanner.py / orb_scanner.py / continuous_search.py — standalone
classifier only, ready to be plugged in once heff decides where and the threshold is tested.
"""
import sys
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walkforward_search import load_daily_symbol
from options_orchestrator import fetch_vix_vvix

from log_setup import get_logger
log = get_logger("market_regime")

ER_THRESHOLD = 0.30  # placeholder, NOT backtest-validated — see module docstring
LOOKBACK_DAYS = 20


def efficiency_ratio(closes):
    """Kaufman's Efficiency Ratio over a closes series: |net move| / (sum of |moves|).
    1.0 = perfectly efficient trend, ~0 = pure chop/mean-reversion. NaN-safe: returns None if
    fewer than 2 points or the path length is zero (flat-line input)."""
    closes = pd.Series(closes).dropna()
    if len(closes) < 2:
        return None
    net = abs(closes.iloc[-1] - closes.iloc[0])
    path = closes.diff().abs().sum()
    if path == 0:
        return None
    return float(net / path)


def realized_vol(closes, annualize=True):
    """Annualized stdev of daily log returns. None if fewer than 2 returns."""
    closes = pd.Series(closes).dropna()
    if len(closes) < 3:
        return None
    ret = np.log(closes / closes.shift(1)).dropna()
    if ret.empty:
        return None
    vol = float(ret.std())
    return vol * np.sqrt(252) if annualize else vol


def classify_regime(lookback_days=LOOKBACK_DAYS, er_threshold=ER_THRESHOLD, ticker="SPY"):
    """Pulls `ticker`'s cached/fetched daily closes (Alpaca, via walkforward_search's
    load_daily_symbol — builds+caches to data/wf_daily_cache/ on first call), computes the
    efficiency ratio over the trailing `lookback_days`, and classifies TRENDING vs
    MEAN_REVERTING. Also fetches VIX (informational only) for context in the log line.
    Returns a dict — any field can be None if its data source failed; never fabricates a
    regime when data is missing, returns UNKNOWN instead."""
    df = load_daily_symbol(ticker)
    if df is None or len(df) < lookback_days + 1:
        log.warning(f"classify_regime({ticker}): insufficient daily data, returning UNKNOWN")
        return {"regime": "UNKNOWN", "efficiency_ratio": None, "realized_vol": None,
                "vix": None, "ticker": ticker, "lookback_days": lookback_days}
    window = df["Close"].iloc[-lookback_days:]
    er = efficiency_ratio(window)
    vol = realized_vol(window)
    vix, _, _, _ = fetch_vix_vvix()
    regime = "UNKNOWN" if er is None else ("TRENDING" if er >= er_threshold else "MEAN_REVERTING")
    log.info(f"classify_regime({ticker}): ER={er} vol={vol} vix={vix} -> {regime}")
    return {"regime": regime, "efficiency_ratio": er, "realized_vol": vol, "vix": vix,
            "ticker": ticker, "lookback_days": lookback_days}


def _selftest():
    """Synthetic series only — no network, no real market data. A straight ramp must read as
    TRENDING (ER~1); a pure oscillation around a flat mean must read as MEAN_REVERTING (ER~0)."""
    trend = pd.Series(np.linspace(100, 130, 40))
    er_trend = efficiency_ratio(trend)
    assert er_trend is not None and er_trend > 0.9, f"trend ER should be ~1, got {er_trend}"

    chop = pd.Series(100 + 5 * np.sin(np.linspace(0, 12 * np.pi, 80)))
    er_chop = efficiency_ratio(chop)
    assert er_chop is not None and er_chop < 0.15, f"chop ER should be ~0, got {er_chop}"

    flat = pd.Series([100.0] * 10)
    assert efficiency_ratio(flat) is None, "flat series should return None (zero path length)"
    assert efficiency_ratio([100.0]) is None, "single point should return None"

    vol = realized_vol(trend)
    assert vol is not None and vol >= 0

    print(f"selftest OK: trend ER={er_trend:.3f}, chop ER={er_chop:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    result = classify_regime(lookback_days=a.lookback_days, ticker=a.ticker)
    print(result)


if __name__ == "__main__":
    main()
