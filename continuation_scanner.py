#!/usr/bin/env python3
"""Mid-day Wyckoff Re-Accumulation continuation scanner (2026-07-01) -- SCANNER ONLY, no
execution wiring, per heff's explicit ask ("as a scanner only not as an automated
trader"). Complements orb_scanner.py, which only fires ~09:45-11:30 (heff's own
observation: ORB strength decays after that) -- this looks for the mid-day continuation
setup instead: a stock that ran in the morning (Phase A), is now consolidating on
shrinking range/volume (Phase B, optionally with a Phase C low-volume "Spring" liquidity
sweep), and hasn't yet broken out (Phase D). Ranks candidates 0-100 as a manual
watchlist for the eventual breakout -- it does not place or even alert-fire trades.

Only needs RTH data (09:30-16:00), unlike premarket_scanner.py -- so this reuses
wide_universe.fetch_bars_batch() directly rather than writing a new fetch helper.

Built from heff's spec (Wyckoff Re-Accumulation Quantitative Scanner.pdf, 2026-07-01).
Two places this deliberately goes beyond that PDF's illustrative reference code (not
beyond its PROSE spec -- the prose is what's actually being followed here):
  1. Sigma contraction (sigma_PhaseB <= 0.4 * sigma_PhaseA) -- described in the prose,
     absent from the reference code entirely. Implemented as an explicit gate below.
  2. Spring's "<30% of morning max volume" check and the "reclaim within 2 bars"
     temporal allowance -- both described in the prose, both absent from the reference
     code (which only checks a same-bar rejection ratio, no volume-magnitude check and
     no multi-bar reclaim window). Implemented properly in detect_spring() below.
One discrepancy WITHIN the source doc itself, resolved explicitly rather than picked
silently: the prose states Phase B's gate thresholds as a 0.0015 flatness cutoff and a
6-period SMA of the volume Z-score <= -0.75, but the doc's own reference code AND its
scoring-matrix table both use 0.002 and a whole-window mean instead. This
implementation uses 0.002 for the flatness GATE (matching the reference code and the
scoring table's normalization constant, so gate and score stay internally consistent),
but implements the 6-period-SMA<=-0.75 check literally as the prose describes it for
the volume-dry-up GATE, while separately using the scoring table's plain Phase-B mean
Z-score for the 0-100 SCORE component -- two distinct, each explicitly-specified
metrics, used for their own explicitly-specified purpose.

Direction: bullish continuation only, matching the source spec exactly (it is written
entirely in terms of resistance breakouts / Phase D upside triggers). A bearish
re-distribution mirror is a natural follow-up, not built here without being asked.

Phase D (the actual breakout) is tracked as STATUS ONLY, not the point of this tool --
a candidate whose consolidation already resolved into a breakout gets reported
separately (informational: "already broke out, here's when") rather than mixed into
the still-consolidating watchlist, and rather than having its post-breakout bars
silently corrupt the Phase B flatness/volume math for that symbol.

Run manually, any time after ~12:05 ET (Phase B needs >=60min of bars past 11:00):
    ./venv/bin/python continuation_scanner.py
"""
import json
import argparse
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import mean_reversion_scanner as mrs
from log_setup import get_logger

log = get_logger("scanner_continuation")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MSG = DATA / "continuation_message_latest.txt"
ET = ZoneInfo("America/New_York")

RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)
PHASE_A_START = dtime(9, 30)
PHASE_A_END = dtime(11, 0)
S_MIN_CUTOFF = dtime(11, 30)
PHASE_B_START = dtime(11, 5)

# Local defaults -- kept self-contained (not merged into mean_reversion_scanner.py's
# shared load_live_params(), same reasoning as premarket_scanner.py): this script is
# new/unvalidated, and both live scanners already depend on that loader, so an edit
# here can't touch mr/orb/portfolio params. Still reads a "continuation" block from
# live_params.json if present, so thresholds are tunable without a code change.
_CONT_DEFAULTS = {
    "lookback_days_vol": 20, "atr_period": 14, "roc_atr_mult": 0.5, "er_min": 0.60,
    "slope_max": 0.002, "sigma_contraction_max": 0.4, "vol_z_sma_period": 6,
    "vol_z_sma_max": -0.75, "min_phase_b_bars": 12,
    "spring_low_vol_z_max": 0.0, "spring_vol_pct_of_morning_max": 0.30,
    "spring_rejection_ratio_min": 0.60, "spring_reclaim_max_bars": 2,
    "phase_d_vol_z_min": 2.5, "phase_d_rejection_ratio_min": 0.80,
    "max_candidates": 20, "index_symbol": "SPY", "fetch_calendar_days": 40,
}


def load_cont_params():
    try:
        data = json.loads((DATA / "live_params.json").read_text())
        return {**_CONT_DEFAULTS, **data.get("continuation", {})}
    except Exception:
        return dict(_CONT_DEFAULTS)


def daily_rth_ohlc(df):
    """Per-date (High, Low, Close) from the RTH rows -- Close is the last RTH bar's
    close, used as the day's settle price for ATR's true-range calc."""
    rth = df[(df.index.time >= RTH_START) & (df.index.time <= RTH_END)]
    out = {}
    for d, day_df in rth.groupby(rth.index.date):
        out[d] = (float(day_df["High"].max()), float(day_df["Low"].min()), float(day_df["Close"].iloc[-1]))
    return out


def compute_atr14(daily_ohlc, prev_date, period):
    """Classic ATR over the `period` most recent complete trading days ending at
    prev_date. Needs period+1 distinct daily bars (one extra day back for the first
    TR's prevClose). None if there isn't enough history yet."""
    days = sorted(d for d in daily_ohlc if d <= prev_date)
    if len(days) < period + 1:
        return None
    window = days[-(period + 1):]
    trs = []
    for i in range(1, len(window)):
        h, l, _ = daily_ohlc[window[i]]
        _, _, prev_c = daily_ohlc[window[i - 1]]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs) if trs else None


def compute_tod_vol_zscore(df, lookback_days, min_periods=5):
    """Time-of-day-adjusted volume Z-score for every bar: each bar is compared against
    THAT SAME clock-minute's own trailing `lookback_days` occurrences (e.g. every prior
    12:00-12:05 bar), shifted by 1 so a day never leaks into its own baseline. This is
    the only way to compare mid-day volume across time without the deterministic
    U-shaped intraday volume curve making every mid-day bar look artificially "dried
    up" relative to the morning. NaN until min_periods prior occurrences exist."""
    time_str = pd.Series(df.index.strftime("%H:%M"), index=df.index)
    grp = df["Volume"].groupby(time_str)
    hist_mean = grp.transform(lambda x: x.shift(1).rolling(lookback_days, min_periods=min_periods).mean())
    hist_std = grp.transform(lambda x: x.shift(1).rolling(lookback_days, min_periods=min_periods).std())
    return (df["Volume"] - hist_mean) / hist_std.replace(0, np.nan)


def kaufman_er(closes):
    """Net directional change / sum of absolute bar-to-bar changes -- 1.0 for a
    perfectly straight-line move, near 0 for pure chop."""
    if len(closes) < 2:
        return None
    net_change = abs(float(closes.iloc[-1]) - float(closes.iloc[0]))
    sum_abs_change = float(closes.diff().abs().sum())
    return net_change / sum_abs_change if sum_abs_change > 0 else None


def normalized_slope(y):
    """OLS slope of y vs. bar sequence, normalized by mean(y) into %-per-bar so it's
    comparable across assets of different absolute price levels."""
    n = len(y)
    if n < 2:
        return None
    x = np.arange(n, dtype=float)
    xbar, ybar = x.mean(), y.mean()
    denom = float(((x - xbar) ** 2).sum())
    if denom == 0:
        return None
    slope = float(((x - xbar) * (y - ybar)).sum()) / denom
    mean_y = float(y.mean())
    return slope / mean_y if mean_y != 0 else None


def detect_spring(phase_b, s_min, morning_max_vol, params):
    """Phase C: a low-volume liquidity sweep below s_min that reclaims the range.
    Real multi-bar reclaim window + the <30%-of-morning-max volume check -- both in
    the source PDF's prose, both missing from its own reference code (see module
    docstring). Returns (has_spring, spring_timestamp_or_None)."""
    lows = phase_b["Low"].values
    closes = phase_b["Close"].values
    highs = phase_b["High"].values
    vols = phase_b["Volume"].values
    vol_z = phase_b["Vol_Z"].values
    idx = phase_b.index
    n = len(phase_b)
    for i in range(n):
        if not (lows[i] < s_min):
            continue
        if not (vol_z[i] < params["spring_low_vol_z_max"]):
            continue
        if morning_max_vol > 0 and not (vols[i] < params["spring_vol_pct_of_morning_max"] * morning_max_vol):
            continue
        rng = highs[i] - lows[i]
        if rng <= 0:
            continue
        rejection = (closes[i] - lows[i]) / rng
        if rejection < params["spring_rejection_ratio_min"]:
            continue
        reclaimed = closes[i] > s_min
        j = i
        while not reclaimed and j < min(i + params["spring_reclaim_max_bars"], n - 1):
            j += 1
            reclaimed = closes[j] > s_min
        if reclaimed:
            return True, idx[i]
    return False, None


def detect_phase_d(post_morning, r_max, params):
    """First bar (if any) where price closes above r_max with a violent volume
    expansion AND velocity into the close -- the actual breakout trigger. Scanned for
    STATUS only, so a candidate whose setup already resolved isn't miscounted as still
    consolidating; this scanner does not act on it. Returns (timestamp, price) or
    (None, None)."""
    for ts, b in post_morning.iterrows():
        if b["Close"] <= r_max:
            continue
        if pd.isna(b["Vol_Z"]) or b["Vol_Z"] < params["phase_d_vol_z_min"]:
            continue
        rng = b["High"] - b["Low"]
        if rng <= 0:
            continue
        rejection = (b["Close"] - b["Low"]) / rng
        if rejection < params["phase_d_rejection_ratio_min"]:
            continue
        return ts, float(b["Close"])
    return None, None


def analyze_symbol(df, spy_df, today, scan_time, params):
    daily_ohlc = daily_rth_ohlc(df)
    prior_dates = sorted(d for d in daily_ohlc if d < today)
    if not prior_dates:
        return None
    prev_date = prior_dates[-1]
    prev_close = daily_ohlc[prev_date][2]
    if prev_close <= 0:
        return None

    atr14 = compute_atr14(daily_ohlc, prev_date, params["atr_period"])
    if atr14 is None:
        return None

    work = df.copy()
    work["Vol_Z"] = compute_tod_vol_zscore(work, params["lookback_days_vol"])
    work["Returns"] = np.log(work["Close"] / work["Close"].shift(1))

    todays = work[work.index.date == today]
    if todays.empty:
        return None

    # --- Phase A: morning momentum (09:30-11:00) ---
    morning = todays[(todays.index.time >= PHASE_A_START) & (todays.index.time <= PHASE_A_END)]
    if len(morning) < 18:
        return None
    morn_open = float(morning["Open"].iloc[0])
    if morn_open <= 0:
        return None
    r_max = float(morning["High"].max())
    peak_pos = int(np.argmax(morning["High"].values))
    roc_morn = (r_max - morn_open) / morn_open
    atr_pct = atr14 / prev_close
    if roc_morn < params["roc_atr_mult"] * atr_pct:
        return None
    er = kaufman_er(morning["Close"])
    if er is None or er < params["er_min"]:
        return None

    # S_min: lowest low from the R_max bar through the 11:30 cutoff -- the prose spec's
    # wider window (not just the 09:30-11:00 morning slice the reference code uses),
    # giving the Automatic Reaction room to actually happen.
    s_min_window = todays[(todays.index >= morning.index[peak_pos]) & (todays.index.time <= S_MIN_CUTOFF)]
    if s_min_window.empty:
        return None
    s_min = float(s_min_window["Low"].min())
    if s_min <= 0 or s_min >= r_max:
        return None

    # --- Phase D check across the full post-morning window up to scan_time, so an
    # already-fired breakout truncates Phase B instead of contaminating it ---
    post_morning = todays[(todays.index.time > PHASE_A_END) & (todays.index.time <= scan_time)]
    breakout_ts, breakout_px = detect_phase_d(post_morning, r_max, params)

    # --- Phase B: consolidation, 11:05 up to scan_time OR up to the breakout, whichever's first ---
    if breakout_ts is not None:
        phase_b = todays[(todays.index.time >= PHASE_B_START) & (todays.index.time < breakout_ts.time())]
    else:
        phase_b = todays[(todays.index.time >= PHASE_B_START) & (todays.index.time <= scan_time)]
    if len(phase_b) < params["min_phase_b_bars"]:
        return None

    slope = normalized_slope(phase_b["Close"].values)
    if slope is None:
        return None
    sigma_a = float(morning["Close"].std())
    sigma_b = float(phase_b["Close"].std())
    if sigma_a <= 0:
        return None

    vol_z_mean = float(phase_b["Vol_Z"].mean())
    vol_z_sma_last = float(phase_b["Vol_Z"].rolling(params["vol_z_sma_period"]).mean().iloc[-1])

    # --- Relative strength vs. SPY over the Phase B window (residual alpha, scored only, not gated) ---
    residual_alpha = None
    if spy_df is not None:
        spy_today = spy_df[spy_df.index.date == today]
        spy_phase_b = spy_today.reindex(phase_b.index)
        spy_ret = np.log(spy_phase_b["Close"] / spy_phase_b["Close"].shift(1))
        asset_ret = phase_b["Returns"]
        valid = asset_ret.notna() & spy_ret.notna()
        if valid.sum() >= 5:  # floor against a noisy few-point regression; not in the spec, a sane safeguard
            var_s = float(spy_ret[valid].var())
            if var_s and var_s > 0:
                beta = float(asset_ret[valid].cov(spy_ret[valid])) / var_s
                residual_alpha = float((asset_ret[valid] - beta * spy_ret[valid]).sum())

    # --- Gates ---
    if abs(slope) > params["slope_max"]:
        return None
    if sigma_b > params["sigma_contraction_max"] * sigma_a:
        return None
    if np.isnan(vol_z_sma_last) or vol_z_sma_last > params["vol_z_sma_max"]:
        return None

    # --- Phase C: spring (bonus, non-gating) ---
    morning_max_vol = float(morning["Volume"].max())
    has_spring, spring_ts = detect_spring(phase_b, s_min, morning_max_vol, params)

    # --- Score (0-100), five equally-weighted 0-20 components per the spec's scoring matrix ---
    score_er = 20 * min(1, max(0, (er - 0.5) / 0.3))
    score_flat = 20 * (1 - min(1, abs(slope) / params["slope_max"]))
    score_vol = min(20, max(0, abs(vol_z_mean) * 15))
    score_rs = min(20, max(0, (residual_alpha or 0) * 1000))
    score_spring = 20 if has_spring else 0
    total_score = score_er + score_flat + score_vol + score_rs + score_spring

    return {
        "prev_close": round(prev_close, 4), "atr14": round(atr14, 4),
        "roc_morn_pct": round(roc_morn * 100, 2), "er": round(er, 2),
        "r_max": round(r_max, 4), "s_min": round(s_min, 4),
        "slope_norm": round(slope, 5), "sigma_a": round(sigma_a, 4), "sigma_b": round(sigma_b, 4),
        "vol_z_mean": round(vol_z_mean, 2), "vol_z_sma_last": round(vol_z_sma_last, 2),
        "residual_alpha": round(residual_alpha, 4) if residual_alpha is not None else None,
        "has_spring": has_spring,
        "spring_time": spring_ts.strftime("%H:%M") if spring_ts is not None else None,
        "phase_b_bars": len(phase_b),
        "score": round(total_score, 1),
        "phase_d_fired": breakout_ts is not None,
        "phase_d_time": breakout_ts.strftime("%H:%M") if breakout_ts is not None else None,
        "phase_d_price": round(breakout_px, 4) if breakout_px is not None else None,
        "last_price": round(float(todays["Close"].iloc[-1]), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=6000)
    ap.add_argument("--scan-time", type=str, default=None, help="HH:MM ET; default = now")
    args = ap.parse_args()

    now = datetime.now(ET)
    today = now.date()
    params = load_cont_params()
    if args.scan_time:
        hh, mm = map(int, args.scan_time.split(":"))
        scan_time = dtime(hh, mm)
    else:
        scan_time = now.time()

    if scan_time < dtime(12, 5):
        log.warning(f"Scan time {scan_time.strftime('%H:%M')} is before Phase B can even reach "
                    f"{params['min_phase_b_bars']} bars (needs 11:05 + ~60min) -- running anyway, "
                    f"expect an empty result.")

    blk = mrs.earnings_blacklist(today)
    core99 = mrs.core99_set()
    import wide_universe as wu
    try:
        tickers = wu.load_universe_for_scanning(rebuild_if_stale=True) or list(core99)
    except Exception:
        tickers = list(core99)
    tickers = [t for t in tickers if t not in blk][:args.max_tickers]

    fetch_list = list(dict.fromkeys([params["index_symbol"]] + tickers))
    log.info(f"Fetching {len(fetch_list)} tickers (incl. {params['index_symbol']}), "
             f"{params['fetch_calendar_days']} calendar days RTH, IEX feed...")
    bars = wu.fetch_bars_batch(fetch_list, days=params["fetch_calendar_days"])

    spy_df = bars.pop(params["index_symbol"], None)
    if spy_df is None:
        log.warning(f"No {params['index_symbol']} data -- relative-strength scoring "
                    f"component will be 0 for every candidate.")

    # sector-rotation TAG (2026-07-01), informational only -- see orb_scanner.py/
    # mean_reversion_scanner.py for the same additive pattern.
    import sector_rotation as secrot
    sector_map = secrot.load_sector_map()
    sector_quadrants = secrot.load_latest_quadrants()

    watching, fired = [], []
    for tk, df in bars.items():
        try:
            r = analyze_symbol(df, spy_df, today, scan_time, params)
        except Exception:
            continue
        if r is None:
            continue
        r["ticker"] = tk
        r["universe"] = "core99" if tk in core99 else "wide500k"
        sec_tag = secrot.ticker_sector_tag(tk, sector_map, sector_quadrants)
        r["sector_etf"] = sec_tag["sector_etf"] if sec_tag else None
        r["sector_quadrant"] = sec_tag["sector_quadrant"] if sec_tag else None
        r["sector_hot"] = sec_tag["sector_hot"] if sec_tag else None
        (fired if r["phase_d_fired"] else watching).append(r)

    watching.sort(key=lambda c: c["score"], reverse=True)
    fired.sort(key=lambda c: c["score"], reverse=True)
    watching = watching[:params["max_candidates"]]
    fired = fired[:params["max_candidates"]]

    out_path = DATA / f"continuation_{today.isoformat()}.json"
    out_path.write_text(json.dumps({"watching": watching, "fired": fired}, indent=2))

    lines = []
    if watching:
        lines.append("\U0001F440 STILL CONSOLIDATING (watch for the breakout):")
        for c in watching:
            spring = f" | SPRING@{c['spring_time']}" if c["has_spring"] else ""
            lines.append(f"• {c['ticker']} score {c['score']:.0f} | R_max {c['r_max']:.2f} "
                         f"S_min {c['s_min']:.2f} | last {c['last_price']:.2f} | "
                         f"ER {c['er']:.2f} slope {c['slope_norm']:+.4f} volZ {c['vol_z_mean']:+.1f} "
                         f"relS {c['residual_alpha']}{spring} [{c['universe']}]")
    if fired:
        lines.append("\n✅ ALREADY BROKE OUT (informational -- setup already resolved):")
        for c in fired:
            lines.append(f"• {c['ticker']} score {c['score']:.0f} | fired {c['phase_d_time']} "
                         f"@ {c['phase_d_price']:.2f} (R_max was {c['r_max']:.2f}) [{c['universe']}]")

    if lines:
        msg = (f"\U0001F4C8 Mid-day continuation scan -- {now.strftime('%b %d %H:%M %Z')} "
               f"(scanner only, no auto-trading):\n" + "\n".join(lines))
        MSG.write_text(msg)
        log.info(msg)
    else:
        log.info(f"No continuation candidates (watching or fired) at {now.strftime('%H:%M %Z')}.")
        if MSG.exists():
            MSG.unlink()


if __name__ == "__main__":
    main()
