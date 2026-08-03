#!/usr/bin/env python3
"""sector_rotation.py -- detects sector rotation (money flowing from cold sectors into a
hot one) using the 11 SPDR sector ETFs vs SPY, entirely on Alpaca free daily bars (no
Databento spend). Feeds a per-ticker "is this name's sector currently hot" gate into
walkforward_search.py so the champion mean-rev/ORB strategy can be tested restricted to
hot-sector names only.

Method (classic Relative Rotation Graph, simplified/transparent version, not the
proprietary JdK normalization):
  rs          = sector_etf_close / SPY_close                      (relative strength ratio)
  rs_zscore   = (rs - rolling_mean(rs, RS_WINDOW)) / rolling_std(rs, RS_WINDOW)
  rs_mom      = rs_zscore - rs_zscore.shift(MOM_WINDOW)            (is the ratio strengthening?)
  quadrant    = Leading    (rs_zscore > 0, rs_mom > 0)  -- already strong, still gaining
                Improving  (rs_zscore <= 0, rs_mom > 0) -- was weak, money rotating IN now
                Weakening  (rs_zscore > 0, rs_mom <= 0) -- still strong, losing steam
                Lagging    (rs_zscore <= 0, rs_mom <= 0) -- weak, money still leaving
"hot" = Leading or Improving (net inflow this period), used as the walkforward gate.

Pieces (each independently runnable, all free/no budget impact):
  --build-map      ticker -> SPDR sector ETF via yfinance .info sector, cached+resumable
                    to data/sector_map.json (only fetches tickers not already mapped).
  --build-etf-bars 2yr daily OHLCV for the 11 ETFs + SPY via Alpaca free daily bars.
  --build-signal   compute rs_zscore/rs_mom/quadrant per ETF per day -> data/sector_rotation.csv
  --all            run all three in order.
  --selftest       pure-math checks on classify_quadrant + rs math ($0, no I/O).
"""
import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
ALPACA_KEY_PATH = ROOT / "credentials" / "alpaca_key.txt"
ALPACA_SECRET_PATH = ROOT / "credentials" / "alpaca_secret.txt"

SECTOR_MAP_PATH = ROOT / "data" / "sector_map.json"
ETF_BARS_PATH = ROOT / "data" / "sector_etf_daily.parquet"
ROTATION_CSV = ROOT / "data" / "sector_rotation.csv"
# STALENESS (2026-07-19, audit Tier-2 #8): the ORB sector gate is LIVE (not informational,
# since 2026-07-02) with zero freshness check on this file -- if the daily 16:20 ET build
# cron silently fails, the gate keeps blocking/allowing entries off a frozen, increasingly
# wrong quadrant read with nothing surfacing it. >3 TRADING days without a refresh is well
# past any plausible single-day cron hiccup and into "something is actually broken."
STALE_TRADING_DAYS_LIMIT = 3

BENCHMARK = "SPY"
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]
ETF_SECTOR_NAME = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication Services",
}
RS_WINDOW = 63     # ~1 trading quarter, for the z-score baseline
MOM_WINDOW = 20    # ~1 trading month, for the momentum leg
HISTORY_DAYS = 760  # ~2yr, matches the Databento backtest window

# yfinance .info['sector'] strings -> SPDR ETF. yfinance uses its own vendor labels, not
# literal GICS names (e.g. "Financial Services" not "Financials") -- mapped explicitly.
YF_SECTOR_TO_ETF = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Health Care": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}


def _creds():
    return ALPACA_KEY_PATH.read_text().strip(), ALPACA_SECRET_PATH.read_text().strip()


def _headers():
    k, s = _creds()
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


# ============================================================= ticker -> sector ETF map
def load_sector_map():
    try:
        return json.loads(SECTOR_MAP_PATH.read_text())
    except Exception:
        return {}


def trading_days_stale(latest_date_str):
    """Business-day count between latest_date_str and today (ET) -- 0 if the file's latest
    date IS today (or later, which shouldn't happen but is harmless). Approximates NYSE
    trading days via business days (no holiday calendar -- same accepted tolerance as this
    codebase's other calendar approximations, e.g. the live ORB gate's own missing NYSE
    half-day handling); good enough to catch a cron that's been silently broken for days,
    not meant to be exact to the day. Never raises -- 0 on any parse error (fail-open, same
    discipline as every other gate/tag in this module)."""
    try:
        latest = pd.Timestamp(latest_date_str)
        today = pd.Timestamp(datetime.now(ET).date())
        if today <= latest:
            return 0
        return max(0, int(len(pd.bdate_range(latest, today)) - 1))
    except Exception:
        return 0


def load_latest_quadrants():
    """Most recent date's row per sector ETF from sector_rotation.csv ->
    {etf: {"quadrant":.., "rs_zscore":.., "rs_mom":.., "date":..}}. {} on any error/missing
    file (live callers must treat that as fail-open, same as every other gate in this repo)."""
    if not ROTATION_CSV.exists():
        return {}
    try:
        df = pd.read_csv(ROTATION_CSV, dtype={"date": str})
    except Exception:
        return {}
    if df.empty:
        return {}
    latest_date = df["date"].max()
    latest = df[df["date"] == latest_date]
    out = {}
    for row in latest.itertuples():
        q = row.quadrant
        if not isinstance(q, str) or not q:
            continue
        out[row.sector_etf] = {"quadrant": q, "rs_zscore": row.rs_zscore,
                                "rs_mom": row.rs_mom, "date": row.date}
    return out


def ticker_sector_tag(ticker, sector_map=None, quadrants=None):
    """Live per-ticker tag for the dashboard/scanners: {"sector_etf", "sector_quadrant",
    "sector_hot"} or None if the ticker is unmapped or there's no rotation data yet
    (fail-open -- callers must never treat None as "block", same discipline as every other
    tag/gate here). Pass pre-loaded sector_map/quadrants when tagging many tickers in one
    run to avoid re-reading the same two small files per ticker."""
    sector_map = sector_map if sector_map is not None else load_sector_map()
    quadrants = quadrants if quadrants is not None else load_latest_quadrants()
    etf = sector_map.get(ticker)
    if not etf:
        return None
    q = quadrants.get(etf)
    if not q:
        return None
    return {"sector_etf": etf, "sector_quadrant": q["quadrant"],
            "sector_hot": q["quadrant"] in ("Leading", "Improving"),
            "sector_data_date": q["date"],
            "sector_stale_days": trading_days_stale(q["date"])}


def build_sector_map(tickers, force=False, pause_s=0.15):
    """Resumable: skips tickers already mapped (or already probed and found unmapped,
    tagged None) unless force=True. yfinance is free/no budget impact."""
    import yfinance as yf
    existing = load_sector_map()
    todo = tickers if force else [t for t in tickers if t not in existing]
    print(f"sector map: {len(existing)} cached, {len(todo)} to fetch")
    for i, t in enumerate(todo):
        try:
            info = yf.Ticker(t).info
            yf_sector = info.get("sector")
            etf = YF_SECTOR_TO_ETF.get(yf_sector)
            existing[t] = etf  # None if unmapped (ETF/ADR/unknown) -- still cached, don't refetch
            print(f"  [{i+1}/{len(todo)}] {t}: {yf_sector} -> {etf}")
        except Exception as e:
            print(f"  [{i+1}/{len(todo)}] {t}: FAILED ({e}) -- will retry next run")
            continue
        time.sleep(pause_s)
        if (i + 1) % 20 == 0:  # checkpoint periodically, a 180-name pull is a few minutes
            SECTOR_MAP_PATH.write_text(json.dumps(existing, indent=1))
    SECTOR_MAP_PATH.write_text(json.dumps(existing, indent=1))
    mapped = sum(1 for v in existing.values() if v)
    print(f"sector map: {mapped}/{len(existing)} tickers mapped -> {SECTOR_MAP_PATH}")
    return existing


# ============================================================= ETF daily bars (Alpaca free)
def fetch_daily_bars(symbols, days=HISTORY_DAYS):
    """Full daily OHLCV for `symbols` via Alpaca free daily bars. Small symbol count (12)
    so no batching/pagination complexity needed beyond what a single multi-symbol request
    handles; still paginates defensively via next_page_token."""
    H = _headers()
    start = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    rows_by_sym = {s: [] for s in symbols}
    page_token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                  "feed": "iex", "adjustment": "raw", "limit": 10000, "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                          headers=H, params=params, timeout=25)
        r.raise_for_status()
        d = r.json()
        for sym, bars in (d.get("bars") or {}).items():
            rows_by_sym.setdefault(sym, []).extend(bars)
        page_token = d.get("next_page_token")
        if not page_token:
            break
    frames = []
    for sym, bars in rows_by_sym.items():
        if not bars:
            print(f"  WARNING: no daily bars for {sym}")
            continue
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET).dt.date
        df["symbol"] = sym
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        frames.append(df[["symbol", "t", "Open", "High", "Low", "Close", "Volume"]])
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return out


def build_etf_bars():
    symbols = SECTOR_ETFS + [BENCHMARK]
    print(f"fetching {HISTORY_DAYS}d daily bars for {symbols} via Alpaca...")
    df = fetch_daily_bars(symbols)
    df.to_parquet(ETF_BARS_PATH)
    print(f"  {len(df)} rows, {df['symbol'].nunique()} symbols -> {ETF_BARS_PATH}")
    return df


# ============================================================= rotation signal
def classify_quadrant(rs_zscore, rs_mom):
    """Pure. None if either input is NaN/None (insufficient history -> unknown, never guess)."""
    if rs_zscore is None or rs_mom is None or (isinstance(rs_zscore, float) and np.isnan(rs_zscore)) \
       or (isinstance(rs_mom, float) and np.isnan(rs_mom)):
        return None
    if rs_zscore > 0:
        return "Leading" if rs_mom > 0 else "Weakening"
    else:
        return "Improving" if rs_mom > 0 else "Lagging"


def compute_rotation(df=None):
    """df: long-format [symbol, t, Close, ...] as returned by fetch_daily_bars/build_etf_bars.
    Returns long-format [date, sector_etf, rs_zscore, rs_mom, quadrant]."""
    if df is None:
        df = pd.read_parquet(ETF_BARS_PATH)
    wide = df.pivot(index="t", columns="symbol", values="Close").sort_index()
    if BENCHMARK not in wide.columns:
        raise SystemExit(f"benchmark {BENCHMARK} missing from ETF bars -- rerun --build-etf-bars")
    rows = []
    for etf in SECTOR_ETFS:
        if etf not in wide.columns:
            print(f"  WARNING: {etf} missing from ETF bars, skipped")
            continue
        rs = wide[etf] / wide[BENCHMARK]
        rs_mean = rs.rolling(RS_WINDOW).mean()
        rs_std = rs.rolling(RS_WINDOW).std()
        rs_z = (rs - rs_mean) / rs_std
        rs_mom = rs_z - rs_z.shift(MOM_WINDOW)
        for dt, z, m in zip(wide.index, rs_z, rs_mom):
            q = classify_quadrant(z, m)
            rows.append((str(dt), etf, z, m, q))
    out = pd.DataFrame(rows, columns=["date", "sector_etf", "rs_zscore", "rs_mom", "quadrant"])
    out.to_csv(ROTATION_CSV, index=False)
    covered = out.dropna(subset=["quadrant"])
    print(f"  {len(out)} rows ({covered['date'].nunique()} dates w/ a quadrant) -> {ROTATION_CSV}")
    if len(covered):
        print("  latest quadrants:\n" + covered[covered["date"] == covered["date"].max()]
              .sort_values("sector_etf").to_string(index=False))
    return out


def _selftest():
    ok = True

    def chk(n, g):
        nonlocal ok; ok &= bool(g); print(f"  [{'OK' if g else 'FAIL'}] {n}")
    chk("Leading: rs_z>0, rs_mom>0", classify_quadrant(1.0, 0.5) == "Leading")
    chk("Weakening: rs_z>0, rs_mom<=0", classify_quadrant(1.0, -0.1) == "Weakening")
    chk("Improving: rs_z<=0, rs_mom>0", classify_quadrant(-0.5, 0.3) == "Improving")
    chk("Lagging: rs_z<=0, rs_mom<=0", classify_quadrant(-0.5, -0.2) == "Lagging")
    chk("boundary rs_z==0 -> non-Leading bucket", classify_quadrant(0.0, 0.3) == "Improving")
    chk("boundary rs_mom==0 -> non-Leading/Improving bucket", classify_quadrant(1.0, 0.0) == "Weakening")
    chk("NaN rs_z -> None (unknown, no guess)", classify_quadrant(float("nan"), 0.3) is None)
    chk("None rs_mom -> None", classify_quadrant(1.0, None) is None)
    chk("yfinance sector map covers all 11 canonical GICS-ish names",
        len({v for v in YF_SECTOR_TO_ETF.values()}) == 11)
    chk("SECTOR_ETFS has exactly 11 unique tickers", len(set(SECTOR_ETFS)) == 11)
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-map", action="store_true")
    ap.add_argument("--symbols", default=None, help="comma list for --build-map; default = wide_universe")
    ap.add_argument("--force-map", action="store_true", help="refetch even already-mapped tickers")
    ap.add_argument("--build-etf-bars", action="store_true")
    ap.add_argument("--build-signal", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        import sys; sys.exit(0 if _selftest() else 1)

    if a.build_map or a.all:
        if a.symbols:
            tickers = [s.strip().upper() for s in a.symbols.split(",")]
        else:
            import wide_universe as wu
            import mean_reversion_scanner as mr
            tickers = wu.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
        build_sector_map(tickers, force=a.force_map)

    if a.build_etf_bars or a.all:
        build_etf_bars()

    if a.build_signal or a.all:
        compute_rotation()


if __name__ == "__main__":
    main()
