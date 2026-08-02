#!/usr/bin/env python3
"""Premarket ORB candidate scanner -- OBSERVATIONAL, not wired into orb_scanner.py's
execution or portfolio_gate admission. Answers a narrower question than orb_scanner.py:
which names in today's universe show real premarket conviction (gap + relative volume),
*before* the 9:30-9:45 opening range even forms.

2026-07-01 REVERTED to this simple gap+rvol design after the same-day "filter stack"
revision (ATR-normalized gap ceiling, premarket VWAP+SD bands, SPY relative-strength
veto, consolidation-lid check) was found to just narrow the candidate list without
fixing bias-direction misses -- meanwhile THIS simple version's LONG/SHORT call was
correct on 8 of 9 candidates on its first live day. Reverting the SELECTION/SORT logic
back to gap+rvol, but keeping the revision's extra technical readouts (atr14, gap_ratio,
vwap, sd_from_vwap, equity/spy premarket drift, rel_strength, lid_ok) as INFORMATIONAL
fields on every candidate -- computed and shown on the dashboard, but NOT used to filter
or re-rank. That keeps the proven selection behavior while giving heff (and the
dashboard) the fuller technical picture per candidate.

Data reality check done before writing this (see chat): Alpaca's free feed is IEX-only,
and IEX premarket volume is thin -- tens to low-hundreds of shares per 5-min bar vs a
30k+ share opening print. That means premarket volume here can NEVER be used as an
absolute threshold (same lesson as the wide-universe ADV filter, see wide_universe.py's
top-of-file note). Instead this computes relative premarket volume: today's premarket
volume for a symbol vs THAT SAME symbol's own trailing-N-day premarket volume on the
SAME feed -- the feed's constant under-capture cancels out of the ratio even though the
absolute numbers are tiny. A minimum baseline-volume floor guards against near-zero
denominators producing meaningless multiples.

Why this stays observational for now: every other signal change promoted to live
trading in this pipeline (tight-range ORB, the planned_rr ranking change, the 4/2
capacity change) was validated via retroactive replay against stored history first.
There is no stored premarket dataset to replay this against yet. Treat its output as a
candidate watchlist to review manually, not a filter that should silently gate what
orb_scanner.py trades.

Run manually before 9:30 ET: ./venv/bin/python premarket_scanner.py
"""
import json
import argparse
from pathlib import Path
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests
import pandas as pd

import mean_reversion_scanner as mrs
from log_setup import get_logger

log = get_logger("scanner_premarket")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MSG = DATA / "orb_premarket_message_latest.txt"
ET = ZoneInfo("America/New_York")

PM_START = dtime(4, 0)
PM_END = dtime(9, 30)
RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)

# Local defaults -- intentionally NOT added to mean_reversion_scanner.py's shared
# load_live_params() merge logic; this script is new/unvalidated and both live scanners
# already depend on that loader, so keeping this file's config fully separate means a
# bad edit here can't touch mr/orb/portfolio params. Still reads live_params.json's
# "premarket" block if present, so it CAN be tuned live without a code change once
# someone decides values worth keeping.
# gap_min_pct/rvol_min/min_baseline_volume are the only SELECTION filters (proven, kept
# from the pre-revision version). atr_period/index_symbol/lid_* below are used only to
# COMPUTE the informational readouts -- none of them reject a candidate.
_PM_DEFAULTS = {
    "gap_min_pct": 2.0, "rvol_min": 1.5, "lookback_days": 10,
    "min_baseline_volume": 50, "max_candidates": 20,
    "atr_period": 14, "index_symbol": "SPY",
    "lid_bars": 4, "lid_max_range_pct": 0.5, "lid_max_dist_from_extreme_pct": 2.0,
}


def load_pm_params():
    try:
        data = json.loads((DATA / "live_params.json").read_text())
        return {**_PM_DEFAULTS, **data.get("premarket", {})}
    except Exception:
        return dict(_PM_DEFAULTS)


def fetch_full_bars_batch(tickers, days, batch_size=100):
    """Like wide_universe.fetch_bars_batch but WITHOUT the .between_time('09:30','16:00')
    filter -- this needs premarket bars (04:00-09:30) plus enough regular-session history
    to compute ATR14 and read the prior day's close, so it can't reuse the RTH-only helper."""
    key, secret = mrs.alpaca_creds()
    H = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    start = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    out = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        rows_by_sym = {s: [] for s in batch}
        page_token = None
        while True:
            params = {"symbols": ",".join(batch), "timeframe": "5Min", "start": start,
                      "feed": "iex", "adjustment": "raw", "limit": 10000, "sort": "asc"}
            if page_token:
                params["page_token"] = page_token
            try:
                r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                                  headers=H, params=params, timeout=25)
                if r.status_code != 200:
                    break
                d = r.json()
            except Exception:
                break
            for sym, bars in (d.get("bars") or {}).items():
                rows_by_sym.setdefault(sym, []).extend(bars)
            page_token = d.get("next_page_token")
            if not page_token:
                break
        for sym, bars in rows_by_sym.items():
            if not bars:
                continue
            df = pd.DataFrame(bars)
            df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
            df = (df.set_index("t")
                    .rename(columns={"o": "Open", "h": "High", "l": "Low",
                                     "c": "Close", "v": "Volume"}))
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if not df.empty:
                out[sym] = df
    return out


def daily_rth_ohlc(df):
    """Per-date High/Low/Close aggregated from the RTH (09:30-16:00) rows only. Returns a
    dict {date: (high, low, close)} -- feeds compute_atr14 (informational only, not a filter)."""
    rth = df[(df.index.time >= RTH_START) & (df.index.time <= RTH_END)]
    out = {}
    for d, day_df in rth.groupby(rth.index.date):
        out[d] = (float(day_df["High"].max()), float(day_df["Low"].min()), float(day_df["Close"].iloc[-1]))
    return out


def compute_atr14(daily_ohlc, prev_date, period):
    """Classic ATR: True Range = max(H-L, |H-prevClose|, |L-prevClose|), averaged over the
    `period` most recent COMPLETE trading days ending at prev_date. Needs period+1 distinct
    daily bars. Returns None if there isn't enough history yet. Informational only (feeds
    gap_ratio for display) -- not used to filter candidates."""
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


def premarket_vwap_bands(pm_df):
    """Volume-weighted VWAP + SD bands, expanding from the first premarket bar (~04:00).
    Informational only (feeds vwap/sd_from_vwap for display) -- not used to filter."""
    typ = (pm_df["High"] + pm_df["Low"] + pm_df["Close"]) / 3.0
    vol = pm_df["Volume"].astype(float)
    cum_vol = vol.cumsum()
    vwap = (typ * vol).cumsum() / cum_vol
    var = ((vol * (typ - vwap) ** 2).cumsum() / cum_vol)
    std = np.sqrt(var)
    return vwap, std


def premarket_drift_pct(pm_df):
    """(last premarket price - first premarket price) / first premarket price * 100 --
    the session's own drift from ~04:00 to now, NOT vs. yesterday's close. Used for the
    SPY relative-strength readout. Informational only."""
    first = float(pm_df["Open"].iloc[0])
    last = float(pm_df["Close"].iloc[-1])
    if first <= 0:
        return None
    return (last - first) / first * 100.0


def check_lid(pm_df, bias, pm_high, pm_low, params):
    """Tight-consolidation check on the trailing `lid_bars` premarket bars. Informational
    only (feeds lid_ok for display) -- not used to filter candidates."""
    tail = pm_df.tail(params["lid_bars"])
    if len(tail) < min(3, params["lid_bars"]):
        return False
    closes = tail["Close"]
    last_price = float(closes.iloc[-1])
    if last_price <= 0:
        return False
    rng_pct = (float(closes.max()) - float(closes.min())) / last_price * 100.0
    if rng_pct > params["lid_max_range_pct"]:
        return False
    extreme = pm_high if bias == "LONG" else pm_low
    if extreme <= 0:
        return False
    dist_pct = abs(extreme - float(closes.mean())) / extreme * 100.0
    return dist_pct <= params["lid_max_dist_from_extreme_pct"]


def analyze_symbol(df, today, daily_ohlc, params, spy_drift_pct):
    """Returns a candidate dict (core gap+rvol fields, the ONLY ones the selection filter
    uses, plus atr14/gap_ratio/vwap/sd_from_vwap/rel_strength/lid_ok as informational
    extras) or None if there isn't enough data to compute the core fields at all."""
    todays = df[df.index.date == today]
    pm_today = todays[(todays.index.time >= PM_START) & (todays.index.time < PM_END)]
    if pm_today.empty:
        return None

    prior_dates = sorted({d for d in df.index.date if d < today})
    if not prior_dates:
        return None
    prev_date = prior_dates[-1]
    prev_day = df[df.index.date == prev_date]
    prev_rth = prev_day[prev_day.index.time <= RTH_END]
    if prev_rth.empty:
        return None
    prev_close = float(prev_rth["Close"].iloc[-1])
    if prev_close <= 0:
        return None

    pm_last = float(pm_today["Close"].iloc[-1])
    pm_high = float(pm_today["High"].max())
    pm_low = float(pm_today["Low"].min())
    pm_vol_today = float(pm_today["Volume"].sum())
    gap_pct = (pm_last - prev_close) / prev_close * 100.0
    bias = "LONG" if gap_pct > 0 else "SHORT"

    baseline_vols = []
    for d in prior_dates:
        day_df = df[df.index.date == d]
        pm_day = day_df[(day_df.index.time >= PM_START) & (day_df.index.time < PM_END)]
        if not pm_day.empty:
            baseline_vols.append(float(pm_day["Volume"].sum()))
    if not baseline_vols:
        return None
    baseline = sum(baseline_vols) / len(baseline_vols)
    pm_rvol = (pm_vol_today / baseline) if baseline > 0 else None

    # --- informational-only extras below (computed for display, never used to filter) ---
    atr14 = compute_atr14(daily_ohlc, prev_date, params["atr_period"])
    gap_ratio = (abs(pm_last - prev_close) / atr14) if atr14 else None

    vwap_s, std_s = premarket_vwap_bands(pm_today)
    vwap_last = float(vwap_s.iloc[-1])
    std_last = float(std_s.iloc[-1]) if not np.isnan(std_s.iloc[-1]) else 0.0
    sd_from_vwap = ((pm_last - vwap_last) / std_last) if std_last > 0 else None

    equity_drift = premarket_drift_pct(pm_today)
    rel_strength = (equity_drift - spy_drift_pct) if (equity_drift is not None and spy_drift_pct is not None) else None

    lid_ok = check_lid(pm_today, bias, pm_high, pm_low, params)

    return {
        "prev_close": round(prev_close, 4), "pm_last": round(pm_last, 4),
        "pm_high": round(pm_high, 4), "pm_low": round(pm_low, 4),
        "gap_pct": round(gap_pct, 2), "bias": bias,
        "pm_volume_today": pm_vol_today, "pm_volume_baseline": round(baseline, 1),
        "pm_rvol": round(pm_rvol, 2) if pm_rvol is not None else None,
        "baseline_days": len(baseline_vols),
        "atr14": round(atr14, 4) if atr14 is not None else None,
        "gap_ratio": round(gap_ratio, 2) if gap_ratio is not None else None,
        "vwap": round(vwap_last, 4), "sd_from_vwap": round(sd_from_vwap, 2) if sd_from_vwap is not None else None,
        "equity_pm_drift_pct": round(equity_drift, 2) if equity_drift is not None else None,
        "spy_pm_drift_pct": round(spy_drift_pct, 2) if spy_drift_pct is not None else None,
        "rel_strength": round(rel_strength, 2) if rel_strength is not None else None,
        "lid_ok": lid_ok,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=6000)
    args = ap.parse_args()

    now = datetime.now(ET)
    today = now.date()
    if now.time() >= PM_END:
        log.info(f"Past {PM_END.strftime('%H:%M')} ET -- premarket window closed for today; "
                  f"running anyway for review, but gap/rvol will reflect a stale premarket read.")

    params = load_pm_params()
    blk = mrs.earnings_blacklist(today)
    core99 = mrs.core99_set()
    try:
        import wide_universe as wu
        tickers = wu.load_universe_for_scanning(rebuild_if_stale=True) or list(core99)
    except Exception:
        tickers = list(core99)
    tickers = [t for t in tickers if t not in blk][:args.max_tickers]

    # ATR14 (informational) needs ~15 trading days of RTH history, more than the RVOL
    # baseline lookback -- fetch window covers whichever is larger, plus a calendar buffer.
    fetch_days = max(params["lookback_days"], params["atr_period"] + 5) + 15
    fetch_list = list(dict.fromkeys([params["index_symbol"]] + tickers))
    log.info(f"Fetching {len(fetch_list)} tickers (incl. {params['index_symbol']}), "
             f"{fetch_days} calendar days, IEX feed...")
    bars = fetch_full_bars_batch(fetch_list, days=fetch_days)

    spy_df = bars.pop(params["index_symbol"], None)
    spy_drift_pct = None
    if spy_df is not None:
        spy_pm = spy_df[(spy_df.index.date == today) & (spy_df.index.time >= PM_START) &
                         (spy_df.index.time < PM_END)]
        if not spy_pm.empty:
            spy_drift_pct = premarket_drift_pct(spy_pm)
    if spy_drift_pct is None:
        log.warning(f"No {params['index_symbol']} premarket data -- rel_strength will be "
                    f"null on every candidate (informational field only, doesn't block selection).")

    # sector-rotation TAG (2026-07-01), informational only -- see orb_scanner.py/
    # mean_reversion_scanner.py for the same additive pattern.
    import sector_rotation as secrot
    sector_map = secrot.load_sector_map()
    sector_quadrants = secrot.load_latest_quadrants()

    candidates = []
    for tk, df in bars.items():
        try:
            daily_ohlc = daily_rth_ohlc(df)
            r = analyze_symbol(df, today, daily_ohlc, params, spy_drift_pct)
        except Exception:
            continue
        if r is None or r["pm_rvol"] is None:
            continue
        # ONLY these three gate selection -- the proven pre-revision behavior. atr14/
        # gap_ratio/vwap/sd_from_vwap/rel_strength/lid_ok ride along as display fields.
        if r["pm_volume_baseline"] < params["min_baseline_volume"]:
            continue
        if abs(r["gap_pct"]) < params["gap_min_pct"]:
            continue
        if r["pm_rvol"] < params["rvol_min"]:
            continue
        r["ticker"] = tk
        r["universe"] = "core99" if tk in core99 else "wide500k"
        sec_tag = secrot.ticker_sector_tag(tk, sector_map, sector_quadrants)
        r["sector_etf"] = sec_tag["sector_etf"] if sec_tag else None
        r["sector_quadrant"] = sec_tag["sector_quadrant"] if sec_tag else None
        r["sector_hot"] = sec_tag["sector_hot"] if sec_tag else None
        candidates.append(r)

    # Sort by GAP size (the proven pre-revision behavior -- 8/9 correct direction calls),
    # NOT by RVOL (that was the revision's change, reverted along with its filter gates).
    candidates.sort(key=lambda c: abs(c["gap_pct"]), reverse=True)
    candidates = candidates[:params["max_candidates"]]

    out_path = DATA / f"orb_premarket_{today.isoformat()}.json"
    out_path.write_text(json.dumps(candidates, indent=2))

    if candidates:
        lines = [f"• {c['ticker']} {c['bias']} | gap {c['gap_pct']:+.1f}% | rvol {c['pm_rvol']:.1f}x "
                 f"(vs {c['baseline_days']}d avg) | pm range {c['pm_low']:.2f}-{c['pm_high']:.2f} "
                 f"| prev close {c['prev_close']:.2f} | last {c['pm_last']:.2f} [{c['universe']}]"
                 for c in candidates]
        msg = (f"\U0001F4CA Premarket ORB candidates -- {now.strftime('%b %d %H:%M %Z')} "
               f"(observational, not auto-traded):\n" + "\n".join(lines))
        MSG.write_text(msg)
        log.info(msg)
    else:
        log.info(f"No premarket candidates met gap>={params['gap_min_pct']}% "
                 f"and rvol>={params['rvol_min']}x at {now.strftime('%H:%M %Z')}.")
        if MSG.exists():
            MSG.unlink()


if __name__ == "__main__":
    main()
