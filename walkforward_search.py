#!/usr/bin/env python3
"""walkforward_search.py — ratcheting strategy-search with out-of-sample honesty.

Goal (per user): keep trying strategy *portfolios*, each a little better than the
last, ratcheting the running champion upward until its net-of-cost total R is
>= 15% above the FIRST (baseline) backtest. Candidates that come back dead
(~0.00R net or negative-expectancy) are discarded.

Why this is built the careful way (skeptical-quant rules):
  * A greedy "run until +15%" search on one data slice is a false-positive
    machine — try enough variants and one clears the bar on noise. So:
      - The ratchet/acceptance decisions are made ONLY on the SEARCH region
        (first SEARCH_FRAC of the calendar).
      - The last (1-SEARCH_FRAC) is a LOCKED HOLDOUT, never touched until the
        champion *claims* +15%. Then baseline vs champion are both scored on it
        once. If the champion's edge does NOT carry to the holdout, we caught a
        mirage and say so.
  * "Total R" alone rewards trade frequency, not edge quality. So a candidate is
    a PORTFOLIO (set of component strategies); baseline = current combo
    (mean-rev + sharpened ORB). Each candidate swaps a component or adds a 3rd
    uncorrelated edge. Per-trade R, daily Sharpe, and pairwise correlation are
    reported beside every result so a frequency-driven "win" is visible.
  * Net-of-cost: per-trade cost = COST_BPS/risk_frac (RH commission-free, so
    slippage+spread). Sub-MIN_RISK_FRAC stops are skipped as untradeable (guards
    against a ZTS-type 12c-stop fluke inflating R).

Components are generated ONCE each (the expensive pass), cached, then the
portfolio compositions are scored cheaply by concatenation.

Run:
  ./venv/bin/python walkforward_search.py --build-cache        # one-time prep (~10m)
  ./venv/bin/python walkforward_search.py --symbols AAPL,NKE   # quick smoke test
  nohup ./venv/bin/python walkforward_search.py > logs/wf_search.log 2>&1 &
"""
import sys
import json
import time
import hashlib
import argparse
import subprocess
from pathlib import Path
from datetime import time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.dataset as pads
from scipy.signal import find_peaks
from scipy.ndimage import gaussian_filter1d

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mean_reversion_scanner as mr
import wide_universe

from log_setup import get_logger
log = get_logger("discovery")

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "data" / "databento"
CACHE_DIR = ROOT / "data" / "wf_cache"
COMP_DIR = ROOT / "data" / "wf_comp"      # per-component trade cache (generate once, reuse)
LEDGER = ROOT / "data" / "wf_ledger.csv"
CHAMP_OUT = ROOT / "data" / "wf_champion.json"
ET = ZoneInfo("America/New_York")
TG_TARGET = "7590346809"

# --- search / honesty knobs ---
SEARCH_FRAC = 0.75      # first 75% of dates = search; last 25% = locked holdout
COST_BPS = 6            # round-trip slippage+spread (realistic large-cap)
MIN_RISK_FRAC = 0.001   # skip trades with stop < 0.1% of price (untradeable / fluke)
TARGET_MULT = 1.15      # champion must reach 1.15x baseline net total R (search region)
MIN_TRADES = 30         # quality guard: a portfolio needs >= this many trades to count
EARN_CACHE = ROOT / "data" / "bt_earnings.json"


# ----------------------------------------------------------------------------
# data loading + indicator prep (mirrors backtest_deep / orb_sharp exactly)
# ----------------------------------------------------------------------------
def rsi(s, p=14):
    d = s.diff(); g = d.clip(lower=0); l = -d.clip(upper=0)
    ag = g.rolling(p).mean(); al = l.rolling(p).mean()
    out = 100 - (100 / (1 + ag / al))
    return out.where(al != 0, 100.0)


def load_symbol(ds, db_sym):
    t = ds.to_table(filter=(pads.field("symbol") == db_sym),
                    columns=["ts_event", "open", "high", "low", "close", "volume", "symbol"])
    df = t.to_pandas()
    if df.empty:
        return None
    if "ts_event" in df.columns:
        df = df.set_index("ts_event")
    df = df.sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.between_time("09:30", "15:59")
    o = df.resample("5min", label="left", closed="left").agg(
        Open=("open", "first"), High=("high", "max"), Low=("low", "min"),
        Close=("close", "last"), Volume=("volume", "sum")).dropna(subset=["Open"])
    o = o.between_time("09:30", "15:55")
    o.index = o.index.tz_localize(None)
    return o


def prep(df):
    df = df.copy()
    c = df["Close"]
    d = df.index.date
    df["sma20"] = c.rolling(20).mean()
    df["std20"] = c.rolling(20).std()
    df["rsi"] = rsi(c, 14)
    df["z"] = (c - df["sma20"]) / df["std20"]
    typ = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"]
    df["vwap"] = (typ * vol).groupby(d).cumsum() / vol.groupby(d).cumsum()
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"]
    df["avgvol"] = vol.groupby(d).transform(lambda s: s.expanding().mean())

    # Downcast for storage/RAM (2026-06-28): all the rolling/cumsum math above runs in
    # float64 for precision, THEN we shrink to float32 here -- roughly halves both the
    # on-disk parquet size and the in-memory footprint of the 99-symbol cache (~320MB
    # on disk currently, on a 1.9GB box that already OOM'd once). float32's exact-integer
    # range is +/-16,777,216; the highest single 5-min-bar Volume seen across the whole
    # cache is ~3.8M, comfortably inside that, so Volume downcasts to uint32 (exact, no
    # float rounding at all) rather than float32.
    float_cols = ["Open", "High", "Low", "Close", "sma20", "std20", "rsi", "z",
                  "vwap", "vwap_dev", "avgvol"]
    for col in float_cols:
        if col in df.columns:
            df[col] = df[col].astype("float32")
    if "Volume" in df.columns:
        df["Volume"] = df["Volume"].astype("uint32")
    return df


def build_cache(symbols, quiet=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    chunks = sorted(DB_DIR.glob("chunk_*.parquet"))
    if not chunks:
        raise SystemExit("no databento chunks found")
    ds = pads.dataset([str(p) for p in chunks])
    built = 0
    for n, sym in enumerate(symbols, 1):
        out = CACHE_DIR / f"{sym}.parquet"
        try:
            df = load_symbol(ds, sym.replace("-", "."))
        except Exception as e:
            log.error(f"[{n}] {sym} load err {e}"); continue
        if df is None or len(df) < 50:
            continue
        prep(df).to_parquet(out)
        built += 1
        if not quiet:
            log.debug(f"[{n}/{len(symbols)}] cached {sym}")
        del df
    return built


def build_cache_alpaca(symbols, days=730, batch_size=15, max_req_per_min=180, quiet=False):
    """Alpaca-sourced alternative to build_cache() for symbols NOT covered by the purchased
    Databento chunks (confirmed 2026-06-28: the wide500k universe has near-zero overlap with
    the curated-99 chunk files build_cache() reads from -- it would silently cache almost
    nothing if pointed at that data). Pulls 2yr of 5-min bars directly from Alpaca's free IEX
    feed via the same multi-symbol/paginated pattern as wide_universe.fetch_bars_batch, but:
      - small batch_size (15, not 100) to keep peak in-memory raw-bar-dict size modest on this
        1-vCPU/1.9GB box -- a 2yr/5min pull is ~150x more data per symbol than the 5-day live
        pulls that pattern was originally sized for.
      - writes + drops each symbol to disk IMMEDIATELY after its batch is fetched, never
        holding more than one batch's raw bars in memory at once.
      - explicit request throttle (max_req_per_min, default 180 = 10% under Alpaca's free-tier
        200/min cap) -- a 2yr/5min pull needs deep pagination (10k bars/page), so request count
        per batch is high even though batch_size is small.
    Final per-symbol output is run through prep() -- the EXACT same float32/uint32-downcasting,
    indicator-computing function build_cache() uses -- so the resulting parquet files are
    schema-identical and gen_mean_rev/gen_orb/generate_component can't tell the difference.
    """
    key, secret = mr.alpaca_creds()
    if not key or not secret:
        raise SystemExit("no Alpaca credentials (credentials/alpaca_key.txt/alpaca_secret.txt)")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    start = (pd.Timestamp.now(tz=ET).date() - pd.Timedelta(days=days)).isoformat()
    req_times = []

    def throttled_get(params):
        now = time.time()
        while req_times and now - req_times[0] > 60:
            req_times.pop(0)
        if len(req_times) >= max_req_per_min:
            time.sleep(max(0.0, 60 - (now - req_times[0]) + 0.1))
        r = mr.requests.get("https://data.alpaca.markets/v2/stocks/bars",
                             headers=headers, params=params, timeout=30)
        req_times.append(time.time())
        return r

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    built = 0
    n_batches = (len(symbols) - 1) // batch_size + 1
    for bi in range(0, len(symbols), batch_size):
        batch = symbols[bi:bi + batch_size]
        rows_by_sym = {s: [] for s in batch}
        page_token = None
        pages = 0
        while True:
            params = {"symbols": ",".join(batch), "timeframe": "5Min", "start": start,
                      "feed": "iex", "adjustment": "raw", "limit": 10000, "sort": "asc"}
            if page_token:
                params["page_token"] = page_token
            try:
                r = throttled_get(params)
                if r.status_code != 200:
                    log.warning(f"batch {bi // batch_size + 1}/{n_batches}: HTTP {r.status_code}, "
                                f"stopping pagination early")
                    break
                d = r.json()
            except Exception as e:
                log.error(f"batch {bi // batch_size + 1}/{n_batches}: request failed: {e}")
                break
            for sym, bars in (d.get("bars") or {}).items():
                rows_by_sym.setdefault(sym, []).extend(bars)
            pages += 1
            page_token = d.get("next_page_token")
            if not page_token:
                break

        batch_built = 0
        for sym, bars in rows_by_sym.items():
            if not bars:
                continue
            df = pd.DataFrame(bars)
            df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
            df = (df.set_index("t").sort_index()
                    .rename(columns={"o": "Open", "h": "High", "l": "Low",
                                      "c": "Close", "v": "Volume"}))
            df = df[["Open", "High", "Low", "Close", "Volume"]].between_time("09:30", "15:55")
            df.index = df.index.tz_localize(None)
            df = df.dropna()
            if len(df) < 50:
                continue
            prep(df).to_parquet(CACHE_DIR / f"{sym}.parquet")
            built += 1
            batch_built += 1
            del df
        if not quiet:
            log.info(f"batch {bi // batch_size + 1}/{n_batches}: {batch_built}/{len(batch)} "
                     f"symbols cached ({pages} pages)")
    return built


def load_cached(symbols):
    out = []
    for sym in symbols:
        p = CACHE_DIR / f"{sym}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            out.append((sym, df))
    return out


# ----------------------------------------------------------------------------
# DAILY-bar data path (separate from the 5-min intraday cache above) -- the
# MA-engagement / volume-profile strategy trades off 50/200-DAY moving averages,
# which the intraday wf_cache can't provide. Source = Alpaca daily bars (free,
# same creds as the 5-min feed), adjustment=split so multi-year MAs aren't
# distorted by raw split gaps (NVDA 10:1 in 2024, etc).
# ----------------------------------------------------------------------------
CACHE_DIR_DAILY = ROOT / "data" / "wf_daily_cache"


def fetch_1d_alpaca(ticker, days=1100):
    """Daily OHLCV bars from Alpaca (free IEX feed), split-adjusted. Returns a df
    with a naive date index + Open/High/Low/Close/Volume, or None on any failure."""
    key, secret = mr.alpaca_creds()
    if not key or not secret:
        return None
    sym = ticker.replace("-", ".")
    start = (pd.Timestamp.now(tz=ET).date() - pd.Timedelta(days=days)).isoformat()
    try:
        r = mr.requests.get(
            mr.ALPACA_BARS_URL.format(sym=sym),
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            params={"timeframe": "1Day", "start": start, "feed": "iex",
                    "adjustment": "split", "limit": 10000, "sort": "asc"},
            timeout=20)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars") or []
        if not bars:
            return None
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET).dt.normalize()
        df = (df.set_index("t")
                .rename(columns={"o": "Open", "h": "High", "l": "Low",
                                  "c": "Close", "v": "Volume"}))
        df.index = df.index.tz_localize(None)
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception:
        return None


def load_daily_symbol(ticker, refresh=False, days=1100):
    """Cache-or-fetch a single symbol's daily bars to data/wf_daily_cache/. `days` only takes
    effect on a fresh fetch (refresh=True or no cache yet) -- an already-cached file is used
    as-is regardless of `days`, so bump refresh=True when requesting a longer window than
    whatever's on disk (added 2026-07-02: free Alpaca IEX daily bars actually go back to
    2020-07-27, ~6yr, well past the original 1100-day/~3yr default)."""
    CACHE_DIR_DAILY.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR_DAILY / f"{ticker}.parquet"
    if out.exists() and not refresh:
        return pd.read_parquet(out)
    df = fetch_1d_alpaca(ticker, days=days)
    if df is None or len(df) < 60:
        return None
    df.to_parquet(out)
    return df


def load_daily_cached(symbols, refresh=False, days=1100):
    out = []
    for sym in symbols:
        df = load_daily_symbol(sym, refresh=refresh, days=days)
        if df is not None:
            out.append((sym, df))
    return out


# ----------------------------------------------------------------------------
# shared forward simulator
# ----------------------------------------------------------------------------
def sim_forward(side, entry, stop, target, H, L, C):
    """Return outcome R (risk-normalized) or None. target=None -> stop/EOD only."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    for k in range(len(H)):
        if side == "LONG":
            if L[k] <= stop:
                return -1.0
            if target is not None and H[k] >= target:
                return (target - entry) / risk
        else:
            if H[k] >= stop:
                return -1.0
            if target is not None and L[k] <= target:
                return (entry - target) / risk
    if len(C) == 0:
        return None
    c = C[-1]
    return (c - entry) / risk if side == "LONG" else (entry - c) / risk


# ----------------------------------------------------------------------------
# strategy generators -> list of (date_str, side, r_gross, risk_frac)
# ----------------------------------------------------------------------------
def gen_mean_rev(df, p, collect_time=False):
    """Two-stage mean reversion (ports backtest_deep.backtest_symbol, parametrized).
    collect_time=True appends a 5th tuple field = entry minute-of-day (for Backtest B);
    default False preserves the 4-tuple contract the component cache + scoring rely on."""
    z_t = p["z"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]; vdev = p["vdev"]
    min_rr = p["min_rr"]; buf = p["stop_buf"]; max_price = p["max_price"]; max_watch = p["max_watch"]
    rows = []

    def emit(day, side, out, rf, emin):
        rows.append((str(day), side, out, rf, emin) if collect_time else (str(day), side, out, rf))

    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        sampled = [i for i, ts in enumerate(idx) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            emin = idx[i].hour * 60 + idx[i].minute
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            sma = float(r["sma20"]); std = float(r["std20"]); rs = float(r["rsi"])
            zz = float(r["z"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, sma, std, rs, zz, vw, dev)):
                continue
            upper = sma + z_t * std
            lower = sma - z_t * std
            setup_long = (zz <= -z_t and close < lower and rs < rsi_os and dev <= -vdev)
            setup_short = (zz >= z_t and close > upper and rs > rsi_ob and dev >= vdev)
            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and close <= max_price:
                        state[key] = {"stage": "watch", "sl": low, "sh": high,
                                      "eh": high, "el": low, "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], high)
                    if close > upper:
                        st["sl"] = low; st["ck"] += 1
                    elif low < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + buf)
                        risk = stop - entry
                        rr = (entry - vw) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                emit(day, "SHORT", out, risk / entry, emin)
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], low)
                    if close < lower:
                        st["sh"] = high; st["ck"] += 1
                    elif high > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - buf)
                        risk = entry - stop
                        rr = (vw - entry) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                emit(day, "LONG", out, risk / entry, emin)
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if state.get(key, {}).get("stage") == "watch" and state[key]["ck"] > max_watch:
                    state[key]["stage"] = "expired"
    return rows


VIX_REGIME_CSV = ROOT / "data" / "vix_regime.csv"


def load_vix_regime():
    """date_str -> term-structure ratio (VIX3M-VIX)/VIX. ratio < 0 = backwardation."""
    out = {}
    try:
        import csv as _csv
        with open(VIX_REGIME_CSV) as f:
            for row in _csv.DictReader(f):
                out[row["date"][:10]] = float(row["ratio"])
    except Exception:
        pass
    return out


VIX_RATIO = load_vix_regime()


# --- intraday market-proxy (SPY-stand-in built from the universe itself) ----------
# The wf_cache holds the 99 S&P-100 single names but NO SPY/VIX intraday series, and
# yfinance only serves ~60d of 5-min — so a "SPY trending down hard" gate can't be
# backtested off external data. Instead we build a market drift proxy FROM the cache:
# at each 5-min ts, the cross-sectional mean of each name's return-since-the-day's-open.
# S&P-100 equal-weight ≈ SPY's mega-cap core; on a risk-off day all names sink together
# so the proxy goes deeply negative across the session — exactly the 2026-06-17 signature.
# Built once by --build-proxy (writes mkt_proxy.csv); loaded here at import like VIX_RATIO.
MKT_PROXY_CSV = ROOT / "data" / "mkt_proxy.csv"


def load_mkt_proxy():
    """ts-string (naive ET 5-min, matches str(Timestamp)) -> cross-sectional mean
    intraday return-since-open across the universe. Empty {} or a missing ts -> the
    gate looks up 0.0 ('flat') and NEVER blocks, so an absent/short proxy file can't
    silently suppress every long (fail-open, same discipline as the VIX gate)."""
    out = {}
    try:
        import csv as _csv
        with open(MKT_PROXY_CSV) as f:
            for row in _csv.DictReader(f):
                out[row["ts"]] = float(row["ret"])
    except Exception:
        pass
    return out


MKT_PROXY = load_mkt_proxy()

# --- SECTOR ROTATION gate (2026-07-01): restrict the champion strategy to names whose
# SPDR sector ETF is currently in a "hot" (Leading/Improving) Relative-Rotation-Graph
# quadrant vs SPY -- built entirely on free Alpaca daily bars + a yfinance-derived static
# ticker->ETF map (sector_rotation.py), $0 spend. Same fail-open discipline as MKT_PROXY/
# VIX_RATIO: an unmapped ticker or missing rotation data never blocks a trade.
SECTOR_MAP_JSON = ROOT / "data" / "sector_map.json"
SECTOR_ROTATION_CSV = ROOT / "data" / "sector_rotation.csv"


def load_sector_map():
    try:
        with open(SECTOR_MAP_JSON) as f:
            return json.load(f)
    except Exception:
        return {}


def load_sector_rotation():
    """date-string -> {sector_etf: quadrant}. Empty {} on any error (fail-open)."""
    if not SECTOR_ROTATION_CSV.exists():
        return {}, []
    try:
        rot = pd.read_csv(SECTOR_ROTATION_CSV, dtype={"date": str})
    except Exception:
        return {}, []
    idx = {d: dict(zip(dd["sector_etf"], dd["quadrant"])) for d, dd in rot.groupby("date")}
    return idx, sorted(idx)


SECTOR_MAP = load_sector_map()
SECTOR_ROTATION, SECTOR_DATES = load_sector_rotation()


def sector_quadrant_on(date_str, ticker):
    """RRG quadrant string (Leading/Improving/Weakening/Lagging) for `ticker`'s sector ETF on
    the most recent CLOSED trading day strictly before date_str (T-1, no look-ahead). None if
    the ticker is unmapped or no prior rotation data exists yet -- shared lookup underlying
    both sector_hot_on (LONG gate) and gen_volprofile_sector's cold-side (SHORT gate) check."""
    etf = SECTOR_MAP.get(ticker)
    if not etf or not SECTOR_DATES:
        return None
    prior = [d for d in SECTOR_DATES if d < date_str]
    if not prior:
        return None
    return SECTOR_ROTATION.get(prior[-1], {}).get(etf)


def sector_hot_on(date_str, ticker):
    """True/False if `ticker`'s sector was Leading/Improving on the most recent CLOSED
    trading day strictly before date_str (T-1, no look-ahead -- mirrors gen_mean_rev_regime's
    shift(1) discipline). None (fail-open -> caller must not block) if the ticker is
    unmapped or no prior rotation data exists yet."""
    quad = sector_quadrant_on(date_str, ticker)
    if quad is None:
        return None
    return quad in ("Leading", "Improving")


def build_proxy(cached):
    """Cross-sectional mean intraday return-since-open per 5-min ts, from the prepped
    cache. ret(sym, ts) = Close(ts)/Open(first bar of that day) - 1; proxy(ts) = mean
    over all symbols present at ts. Equal-weight, written to mkt_proxy.csv."""
    acc = {}  # ts-string -> [sum_ret, count]
    for sym, df in cached:
        o0 = df["Open"].groupby(df.index.normalize()).transform("first")
        ret = df["Close"] / o0 - 1.0
        for ts, rv in ret.items():
            if pd.isna(rv):
                continue
            a = acc.setdefault(str(ts), [0.0, 0])
            a[0] += float(rv); a[1] += 1
    rows = [{"ts": ts, "ret": s / n} for ts, (s, n) in acc.items() if n > 0]
    out = pd.DataFrame(rows).sort_values("ts")
    out.to_csv(MKT_PROXY_CSV, index=False)
    return len(out)


def gen_mean_rev_vix(df, p):
    """Mean reversion + VIX term-structure gate: skip LONG fades on days in
    BACKWARDATION (ratio < vix_veto_long_below). When VIX > VIX3M the market is in
    panic / forced liquidation and a -1.5sigma drop is a repricing, not a reversion.
    Shorts unaffected. Orthogonal to price (market-wide macro). Fail-open if a day
    has no VIX data (ratio defaults to 999 -> not blocked)."""
    z_t = p["z"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]; vdev = p["vdev"]
    min_rr = p["min_rr"]; buf = p["stop_buf"]; max_price = p["max_price"]; max_watch = p["max_watch"]
    veto_below = p["vix_veto_long_below"]
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        long_blocked = VIX_RATIO.get(str(day), 999.0) < veto_below
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        sampled = [i for i, ts in enumerate(idx) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            sma = float(r["sma20"]); std = float(r["std20"]); rs = float(r["rsi"])
            zz = float(r["z"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, sma, std, rs, zz, vw, dev)):
                continue
            upper = sma + z_t * std
            lower = sma - z_t * std
            setup_long = (zz <= -z_t and close < lower and rs < rsi_os and dev <= -vdev) and not long_blocked
            setup_short = (zz >= z_t and close > upper and rs > rsi_ob and dev >= vdev)
            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and close <= max_price:
                        state[key] = {"stage": "watch", "sl": low, "sh": high,
                                      "eh": high, "el": low, "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], high)
                    if close > upper:
                        st["sl"] = low; st["ck"] += 1
                    elif low < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + buf)
                        risk = stop - entry
                        rr = (entry - vw) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "SHORT", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], low)
                    if close < lower:
                        st["sh"] = high; st["ck"] += 1
                    elif high > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - buf)
                        risk = entry - stop
                        rr = (vw - entry) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "LONG", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if state.get(key, {}).get("stage") == "watch" and state[key]["ck"] > max_watch:
                    state[key]["stage"] = "expired"
    return rows


def gen_mean_rev_riskoff(df, p):
    """Mean reversion + an INTRADAY risk-off gate. Identical two-stage MR as
    gen_mean_rev, but a LONG fade is blocked when the market is selling off hard
    intraday: MKT_PROXY[ts] (cross-sectional return-since-open) <= riskoff_long_below.
    Optionally a SHORT is blocked when the market is ripping up (>= riskoff_short_above;
    default off). Attacks MR's catastrophic failure mode — fading a broad DIRECTIONAL
    selloff (the 2026-06-17 rate-hike day: 27 LONG fires, 14 straight stops). Faster,
    price-based counterpart to the daily VIX term-structure gate (which came back
    NO-ADOPT, event-starved). Fail-open: ts absent from MKT_PROXY -> 0.0 -> never blocks."""
    z_t = p["z"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]; vdev = p["vdev"]
    min_rr = p["min_rr"]; buf = p["stop_buf"]; max_price = p["max_price"]; max_watch = p["max_watch"]
    long_below = p["riskoff_long_below"]
    short_above = p.get("riskoff_short_above", 1e9)   # default: shorts never blocked
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        sampled = [i for i, ts in enumerate(idx) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            sma = float(r["sma20"]); std = float(r["std20"]); rs = float(r["rsi"])
            zz = float(r["z"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, sma, std, rs, zz, vw, dev)):
                continue
            mkt = MKT_PROXY.get(str(idx[i]), 0.0)   # market drift since open at this bar
            block_long = mkt <= long_below
            block_short = mkt >= short_above
            upper = sma + z_t * std
            lower = sma - z_t * std
            setup_long = (zz <= -z_t and close < lower and rs < rsi_os and dev <= -vdev) and not block_long
            setup_short = (zz >= z_t and close > upper and rs > rsi_ob and dev >= vdev) and not block_short
            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and close <= max_price:
                        state[key] = {"stage": "watch", "sl": low, "sh": high,
                                      "eh": high, "el": low, "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], high)
                    if close > upper:
                        st["sl"] = low; st["ck"] += 1
                    elif low < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + buf)
                        risk = stop - entry
                        rr = (entry - vw) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "SHORT", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], low)
                    if close < lower:
                        st["sh"] = high; st["ck"] += 1
                    elif high > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - buf)
                        risk = entry - stop
                        rr = (vw - entry) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "LONG", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if state.get(key, {}).get("stage") == "watch" and state[key]["ck"] > max_watch:
                    state[key]["stage"] = "expired"
    return rows


def gen_orb(df, p, collect_time=False):
    """Opening-range breakout w/ optional VWAP + volume filters (ports orb_sharp).
    collect_time=True appends a 5th field = entry minute-of-day (Backtest B); default
    False preserves the 4-tuple contract the cache + scoring rely on."""
    or_end = p["or_end"]; vol_mult = p["vol_mult"]; use_vwap = p["use_vwap"]
    use_vol = p["use_vol"]; max_price = p["max_price"]
    max_range_frac = p.get("max_range_frac")   # None = no cap (baseline ORB); else skip WIDE opening ranges
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        times = np.array([t.time() for t in dd.index])
        orb = dd[times < or_end]; post = dd[times >= or_end]
        if len(orb) < 1 or len(post) < 2:
            continue
        orh = float(orb["High"].max()); orl = float(orb["Low"].min())
        rng = orh - orl
        if rng <= 0 or orh > max_price:
            continue
        pH = post["High"].to_numpy(); pL = post["Low"].to_numpy(); pC = post["Close"].to_numpy()
        pO = post["Open"].to_numpy(); pV = post["vwap"].to_numpy(); pA = post["avgvol"].to_numpy()
        pVol = post["Volume"].to_numpy(); pidx = post.index
        for j in range(len(post)):
            up = pH[j] >= orh; dn = pL[j] <= orl
            if not (up or dn):
                continue
            if up and dn:
                side = "LONG" if pC[j] >= pO[j] else "SHORT"
            else:
                side = "LONG" if up else "SHORT"
            entry = orh if side == "LONG" else orl
            stop = orl if side == "LONG" else orh
            if entry <= 0 or rng / entry < MIN_RISK_FRAC:
                break  # first break untradeable (sub-0.1% stop); no ORB trade this day
            if max_range_frac is not None and rng / entry > max_range_frac:
                break  # opening range too WIDE -> skip; tight-range = the holdout-carried ORB edge (2026-06-24)
            relvol = pVol[j] / pA[j] if pA[j] > 0 else 0
            take_vwap = (pC[j] > pV[j]) if side == "LONG" else (pC[j] < pV[j])
            take_vol = relvol >= vol_mult
            if use_vwap and not take_vwap:
                break  # first break failed filter -> no ORB trade this day
            if use_vol and not take_vol:
                break
            out = sim_forward(side, entry, stop, None, pH[j:], pL[j:], pC[j:])
            if out is not None:
                emin = pidx[j].hour * 60 + pidx[j].minute
                rows.append((str(day), side, out, rng / entry, emin) if collect_time
                            else (str(day), side, out, rng / entry))
            break  # ORB acts only on the first break of the day
    return rows


def gen_orb_regime(df, p):
    """Backtest A — sharpened ORB + a higher-timeframe (1h EMA) trend gate. Only takes the
    first break whose side is ALIGNED with the 1h trend (fast EMA vs slow EMA, prior
    completed hour -> no look-ahead): in a 1h downtrend only SHORT breakouts are eligible,
    in an uptrend only LONGS. Misaligned breaks are skipped (we keep scanning for the first
    aligned one). Halves signals + stops the book fighting the dominant tape — the live read
    (ORB shorts +20.5R, longs dead). Same VWAP+volume filters as gen_orb."""
    or_end = p["or_end"]; vol_mult = p["vol_mult"]; use_vwap = p["use_vwap"]
    use_vol = p["use_vol"]; max_price = p["max_price"]
    ema_fast = p.get("ema_fast", 20); ema_slow = p.get("ema_slow", 50)

    h1 = df["Close"].resample("1h").last().dropna()      # 1h trend, no intra-hour peeking
    if len(h1) < ema_slow + 2:
        trend = None
    else:
        ef = h1.ewm(span=ema_fast, adjust=False).mean()
        es = h1.ewm(span=ema_slow, adjust=False).mean()
        up = (ef > es).shift(1)                           # prior completed hour
        trend = up.reindex(df.index, method="ffill")

    def trend_up(ts):
        if trend is None:
            return None
        v = trend.get(ts)
        return None if v is None else bool(v)

    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        times = np.array([t.time() for t in dd.index])
        orb = dd[times < or_end]; post = dd[times >= or_end]
        if len(orb) < 1 or len(post) < 2:
            continue
        orh = float(orb["High"].max()); orl = float(orb["Low"].min())
        rng = orh - orl
        if rng <= 0 or orh > max_price:
            continue
        pH = post["High"].to_numpy(); pL = post["Low"].to_numpy(); pC = post["Close"].to_numpy()
        pO = post["Open"].to_numpy(); pV = post["vwap"].to_numpy(); pA = post["avgvol"].to_numpy()
        pVol = post["Volume"].to_numpy(); pidx = post.index
        for j in range(len(post)):
            up = pH[j] >= orh; dn = pL[j] <= orl
            if not (up or dn):
                continue
            if up and dn:
                side = "LONG" if pC[j] >= pO[j] else "SHORT"
            else:
                side = "LONG" if up else "SHORT"
            tu = trend_up(pidx[j])
            if tu is not None and ((side == "LONG" and not tu) or (side == "SHORT" and tu)):
                break  # first break is AGAINST the 1h trend -> no ORB this day (strict subset
                       # of plain ORB: we filter the misaligned signal, we don't hunt a new one)
            entry = orh if side == "LONG" else orl
            stop = orl if side == "LONG" else orh
            if entry <= 0 or rng / entry < MIN_RISK_FRAC:
                break
            relvol = pVol[j] / pA[j] if pA[j] > 0 else 0
            take_vwap = (pC[j] > pV[j]) if side == "LONG" else (pC[j] < pV[j])
            take_vol = relvol >= vol_mult
            if use_vwap and not take_vwap:
                break
            if use_vol and not take_vol:
                break
            out = sim_forward(side, entry, stop, None, pH[j:], pL[j:], pC[j:])
            if out is not None:
                rows.append((str(day), side, out, rng / entry))
            break  # first ALIGNED, filter-passing break only
    return rows


def gen_mean_rev_sector(df, p):
    """Sector-rotation-gated mean-rev (2026-07-01): identical two-stage MR mechanic as
    gen_mean_rev, but a setup is only allowed to arm on days where p["_ticker"]'s SPDR
    sector ETF is in a "hot" (Leading/Improving) Relative-Rotation-Graph quadrant vs SPY
    as of the PRIOR closed trading day (sector_hot_on, no look-ahead). Unmapped ticker or
    no rotation data -> None -> fail-open (never blocks), same discipline as the VIX/
    risk-off gates. Tests "restrict the champion strategy to hot-sector names.\""""
    z_t = p["z"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]; vdev = p["vdev"]
    min_rr = p["min_rr"]; buf = p["stop_buf"]; max_price = p["max_price"]; max_watch = p["max_watch"]
    ticker = p.get("_ticker")

    hot_cache = {}

    def hot_on(day):
        ds = str(day)
        if ds not in hot_cache:
            v = sector_hot_on(ds, ticker)
            hot_cache[ds] = True if v is None else v   # fail-open: unknown -> allowed
        return hot_cache[ds]

    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        if not hot_on(day):
            continue
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        sampled = [i for i, ts in enumerate(idx) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            sma = float(r["sma20"]); std = float(r["std20"]); rs = float(r["rsi"])
            zz = float(r["z"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, sma, std, rs, zz, vw, dev)):
                continue
            upper = sma + z_t * std
            lower = sma - z_t * std
            setup_long = (zz <= -z_t and close < lower and rs < rsi_os and dev <= -vdev)
            setup_short = (zz >= z_t and close > upper and rs > rsi_ob and dev >= vdev)
            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and close <= max_price:
                        state[key] = {"stage": "watch", "sl": low, "sh": high,
                                      "eh": high, "el": low, "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], high)
                    if close > upper:
                        st["sl"] = low; st["ck"] += 1
                    elif low < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + buf)
                        risk = stop - entry
                        rr = (entry - vw) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "SHORT", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], low)
                    if close < lower:
                        st["sh"] = high; st["ck"] += 1
                    elif high > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - buf)
                        risk = entry - stop
                        rr = (vw - entry) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "LONG", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if state.get(key, {}).get("stage") == "watch" and state[key]["ck"] > max_watch:
                    state[key]["stage"] = "expired"
    return rows


def gen_orb_sector(df, p):
    """Sector-rotation-gated ORB (2026-07-01): identical to gen_orb, but a symbol's ORB
    setups are only eligible on days its sector is "hot" (see gen_mean_rev_sector). Same
    fail-open discipline; whole trading DAYS are skipped rather than gating direction, since
    ORB (unlike mean-rev) has no natural pairing to sector inflow/outflow direction."""
    or_end = p["or_end"]; vol_mult = p["vol_mult"]; use_vwap = p["use_vwap"]
    use_vol = p["use_vol"]; max_price = p["max_price"]
    max_range_frac = p.get("max_range_frac")
    ticker = p.get("_ticker")

    hot_cache = {}

    def hot_on(day):
        ds = str(day)
        if ds not in hot_cache:
            v = sector_hot_on(ds, ticker)
            hot_cache[ds] = True if v is None else v
        return hot_cache[ds]

    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        if not hot_on(day):
            continue
        times = np.array([t.time() for t in dd.index])
        orb = dd[times < or_end]; post = dd[times >= or_end]
        if len(orb) < 1 or len(post) < 2:
            continue
        orh = float(orb["High"].max()); orl = float(orb["Low"].min())
        rng = orh - orl
        if rng <= 0 or orh > max_price:
            continue
        pH = post["High"].to_numpy(); pL = post["Low"].to_numpy(); pC = post["Close"].to_numpy()
        pO = post["Open"].to_numpy(); pV = post["vwap"].to_numpy(); pA = post["avgvol"].to_numpy()
        pVol = post["Volume"].to_numpy()
        for j in range(len(post)):
            up = pH[j] >= orh; dn = pL[j] <= orl
            if not (up or dn):
                continue
            if up and dn:
                side = "LONG" if pC[j] >= pO[j] else "SHORT"
            else:
                side = "LONG" if up else "SHORT"
            entry = orh if side == "LONG" else orl
            stop = orl if side == "LONG" else orh
            if entry <= 0 or rng / entry < MIN_RISK_FRAC:
                break
            if max_range_frac is not None and rng / entry > max_range_frac:
                break
            relvol = pVol[j] / pA[j] if pA[j] > 0 else 0
            take_vwap = (pC[j] > pV[j]) if side == "LONG" else (pC[j] < pV[j])
            take_vol = relvol >= vol_mult
            if use_vwap and not take_vwap:
                break
            if use_vol and not take_vol:
                break
            out = sim_forward(side, entry, stop, None, pH[j:], pL[j:], pC[j:])
            if out is not None:
                rows.append((str(day), side, out, rng / entry))
            break
    return rows


def gen_vwap_rev(df, p):
    """New edge: simple VWAP-deviation fade (no Bollinger/break double-confirm).

    When price is stretched from session VWAP and RSI is exhausted, fade back to
    VWAP. Distinct, lower-confirmation signal -> different trade days than mean-rev.
    """
    vdev = p["vdev"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]
    buf = p["stop_buf"]; max_price = p["max_price"]; min_rr = p.get("min_rr", 1.0)
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["rsi", "vwap"])
        if dd.empty:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        fired = False
        for i, ts in enumerate(idx):
            if ts.minute % 15 != 0 or fired:
                continue
            r = dd.iloc[i]
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            rs = float(r["rsi"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, rs, vw, dev)) or close > max_price:
                continue
            if dev <= -vdev and rs < rsi_os:
                entry = close; stop = low * (1 - buf); risk = entry - stop
                if risk <= 0 or entry <= 0 or risk / entry < MIN_RISK_FRAC:
                    continue
                if (vw - entry) / risk < min_rr:
                    continue
                out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                if out is not None:
                    rows.append((str(day), "LONG", out, risk / entry)); fired = True
            elif dev >= vdev and rs > rsi_ob:
                entry = close; stop = high * (1 + buf); risk = stop - entry
                if risk <= 0 or entry <= 0 or risk / entry < MIN_RISK_FRAC:
                    continue
                if (entry - vw) / risk < min_rr:
                    continue
                out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                if out is not None:
                    rows.append((str(day), "SHORT", out, risk / entry)); fired = True
    return rows


def gen_close_drift(df, p):
    """Bet A — closing-hour / MOC-flow edge.

    Driver = market-on-close imbalances + index-rebalance flows in the last hour,
    structurally orthogonal to the opening-range (ORB) and Bollinger-stretch
    (mean-rev) edges -> aims for low correlation to both. At `decide_time` we
    classify the day by its position vs session VWAP:
      mode='mom' : ride a trending day into the close (LONG if above VWAP).
      mode='rev' : fade a stretched day (SHORT if above VWAP).
    Entry = decision-bar close; protective stop = swing extreme of the last
    `stop_bars` bars; exit = the session's last bar (EOD). One trade/symbol/day.
    """
    decide = p["decide_time"]; mode = p["mode"]; thresh = p["thresh"]
    stop_bars = p["stop_bars"]; buf = p["stop_buf"]; max_price = p["max_price"]
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["vwap"])
        if len(dd) < stop_bars + 2:
            continue
        idx = dd.index
        pos = None
        for i, ts in enumerate(idx):
            if ts.time() >= decide:
                pos = i; break
        if pos is None or pos < stop_bars or pos >= len(dd) - 1:
            continue
        r = dd.iloc[pos]
        close = float(r["Close"]); vw = float(r["vwap"])
        if np.isnan(close) or np.isnan(vw) or vw <= 0 or close > max_price:
            continue
        strength = (close - vw) / vw
        if abs(strength) < thresh:
            continue
        up = strength > 0
        side = ("LONG" if up else "SHORT") if mode == "mom" else ("SHORT" if up else "LONG")
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        entry = close
        if side == "LONG":
            stop = float(L[pos - stop_bars:pos + 1].min()) * (1 - buf); risk = entry - stop
        else:
            stop = float(H[pos - stop_bars:pos + 1].max()) * (1 + buf); risk = stop - entry
        if risk <= 0 or entry <= 0 or risk / entry < MIN_RISK_FRAC:
            continue
        out = sim_forward(side, entry, stop, None, H[pos + 1:], L[pos + 1:], C[pos + 1:])
        if out is not None:
            rows.append((str(day), side, out, risk / entry))
    return rows


def gen_vol_reversion(df, p):
    """Bet B1 — volatility reversion. Fade large gaps at market open.

    If prev-close-to-open gap > vol_thresh * intraday sigma, take opposite side.
    Entry at 09:45 (gap absorbed by algos), exit EOD. One trade per symbol per day.
    """
    vol_thresh = p["vol_thresh"]; hold_until = p["hold_until"]
    max_price = p["max_price"]; stop_buf = p["stop_buf"]
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["Close"])
        if len(dd) < 2:
            continue
        idx = dd.index
        ent_idx = None
        for i, ts in enumerate(idx):
            if ts.time() >= hold_until:
                ent_idx = i
                break
        if ent_idx is None or ent_idx >= len(dd) - 1:
            continue
        open_px = float(dd.iloc[0]["Open"]); prev_close = float(dd.iloc[0]["Close"])
        gap = (open_px - prev_close) / prev_close if prev_close > 0 else 0
        std = float(dd["Close"].std())
        if std <= 0 or prev_close <= 0:
            continue
        gap_z = gap * prev_close / std
        if abs(gap_z) < vol_thresh:
            continue
        entry = float(dd.iloc[ent_idx]["Close"])
        if entry > max_price or entry <= 0:
            continue
        side = "SHORT" if gap > 0 else "LONG"
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        if side == "LONG":
            stop = L[max(0, ent_idx - 2):ent_idx + 1].min() * (1 - stop_buf)
            risk = entry - stop
        else:
            stop = H[max(0, ent_idx - 2):ent_idx + 1].max() * (1 + stop_buf)
            risk = stop - entry
        if risk <= 0 or risk / entry < MIN_RISK_FRAC:
            continue
        out = sim_forward(side, entry, stop, None, H[ent_idx + 1:], L[ent_idx + 1:], C[ent_idx + 1:])
        if out is not None:
            rows.append((str(day), side, out, risk / entry))
    return rows


def gen_earnings_reversion(df, p):
    """Bet B2 — post-earnings gap reversions. Fade gaps on earnings-announcement days.

    Load earnings cache; if trade date is in earnings window (within 2 days of event),
    fade intraday reversions. Entry at specific time, exit EOD.
    """
    earn_dates = set(p["earn_dates"]); hold_until = p["hold_until"]
    max_price = p["max_price"]; stop_buf = p["stop_buf"]; min_rr = p["min_rr"]
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        if str(day) not in earn_dates:
            continue
        dd = dd.dropna(subset=["vwap"])
        if len(dd) < 3:
            continue
        idx = dd.index
        ent_idx = None
        for i, ts in enumerate(idx):
            if ts.time() >= hold_until:
                ent_idx = i
                break
        if ent_idx is None or ent_idx >= len(dd) - 1:
            continue
        open_px = float(dd.iloc[0]["Open"]); vw = float(dd.iloc[ent_idx]["vwap"])
        entry = float(dd.iloc[ent_idx]["Close"])
        if entry > max_price or entry <= 0 or vw <= 0:
            continue
        gap = (open_px - vw) / vw
        if abs(gap) < 0.01:
            continue
        side = "SHORT" if gap > 0 else "LONG"
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        if side == "LONG":
            stop = L[max(0, ent_idx - 3):ent_idx + 1].min() * (1 - stop_buf)
            risk = entry - stop
        else:
            stop = H[max(0, ent_idx - 3):ent_idx + 1].max() * (1 + stop_buf)
            risk = stop - entry
        if risk <= 0 or risk / entry < MIN_RISK_FRAC:
            continue
        rr = (vw - entry) / risk if side == "LONG" else (entry - vw) / risk
        if rr < min_rr:
            continue
        out = sim_forward(side, entry, stop, vw, H[ent_idx + 1:], L[ent_idx + 1:], C[ent_idx + 1:])
        if out is not None:
            rows.append((str(day), side, out, risk / entry))
    return rows


def gen_overnight_gap(df, p):
    """Bet B3 — capture overnight gap reversions.

    On days with large open-to-VWAP gaps, enter and hold intraday for reversion.
    Early entry (9:35), exit EOD. One trade per symbol per day.
    """
    gap_thresh = p["gap_thresh"]; hold_until = p["hold_until"]
    max_price = p["max_price"]; stop_buf = p["stop_buf"]
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["vwap"])
        if len(dd) < 2:
            continue
        idx = dd.index
        open_px = float(dd.iloc[0]["Open"]); first_vwap = float(dd.iloc[0]["vwap"])
        if first_vwap <= 0 or open_px <= 0:
            continue
        gap = (open_px - first_vwap) / first_vwap
        if abs(gap) < gap_thresh:
            continue
        ent_idx = None
        for i, ts in enumerate(idx):
            if ts.time() >= hold_until:
                ent_idx = i
                break
        if ent_idx is None or ent_idx >= len(dd) - 1:
            continue
        entry = float(dd.iloc[ent_idx]["Close"])
        if entry > max_price or entry <= 0:
            continue
        side = "SHORT" if gap > 0 else "LONG"
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        if side == "LONG":
            stop = open_px * (1 - stop_buf)
            risk = entry - stop
        else:
            stop = open_px * (1 + stop_buf)
            risk = stop - entry
        if risk <= 0 or risk / entry < MIN_RISK_FRAC:
            continue
        out = sim_forward(side, entry, stop, None, H[ent_idx + 1:], L[ent_idx + 1:], C[ent_idx + 1:])
        if out is not None:
            rows.append((str(day), side, out, risk / entry))
    return rows


def gen_microstructure(df, p):
    """Bet B4 — microstructure / vol-spike reversion. Fade single-bar extremes.

    When a 5-min bar range > vol_spike_thresh * rolling std, enter to fade the move.
    Entry = bar close, stop = bar extreme, target = back to session VWAP or midpoint.
    """
    vol_spike_thresh = p["vol_spike_thresh"]; min_rr = p.get("min_rr", 1.0)
    stop_buf = p["stop_buf"]; max_price = p["max_price"]
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["vwap"])
        if len(dd) < 5:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        O = dd["Open"].to_numpy(); vwap_arr = dd["vwap"].to_numpy()
        rolled_std = pd.Series(H - L).rolling(10).mean()
        fired = False
        for i in range(1, len(dd)):
            if fired:
                break
            bar_range = H[i] - L[i]
            roll_std = float(rolled_std.iloc[i]) if i < len(rolled_std) else 0
            if roll_std <= 0 or bar_range < vol_spike_thresh * roll_std:
                continue
            entry = float(C[i])
            vw = float(vwap_arr[i])
            if entry > max_price or entry <= 0 or vw <= 0:
                continue
            if entry > vw:
                side = "SHORT"
                stop = H[i] * (1 + stop_buf)
                target = vw
            else:
                side = "LONG"
                stop = L[i] * (1 - stop_buf)
                target = vw
            risk = abs(entry - stop)
            if risk <= 0 or risk / entry < MIN_RISK_FRAC:
                continue
            rr = abs(target - entry) / risk
            if rr < min_rr:
                continue
            out = sim_forward(side, entry, stop, target, H[i + 1:], L[i + 1:], C[i + 1:])
            if out is not None:
                rows.append((str(day), side, out, risk / entry)); fired = True
    return rows


def gen_mean_rev_regime(df, p):
    """B — mean reversion + a 1h EMA20/50 trend gate. Same two-stage MR as
    gen_mean_rev, but a setup is only allowed in a ROTATIONAL regime (when the
    EMAs are stacked, |ema20-ema50|/ema50 > regime_thresh, the market is trending
    -> skip the fade). Attacks MR's known failure: fast stop-outs on trending days."""
    z_t = p["z"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]; vdev = p["vdev"]
    min_rr = p["min_rr"]; buf = p["stop_buf"]; max_price = p["max_price"]; max_watch = p["max_watch"]
    regime_thresh = p["regime_thresh"]
    ema_fast = p.get("ema_fast", 20); ema_slow = p.get("ema_slow", 50)

    h1 = df["Close"].resample("1h").last().dropna()      # 1h EMA regime, no look-ahead
    if len(h1) < ema_slow + 2:
        trending = None
    else:
        ef = h1.ewm(span=ema_fast, adjust=False).mean()
        es = h1.ewm(span=ema_slow, adjust=False).mean()
        stacked = ((ef - es).abs() / es).shift(1)        # use the PRIOR completed hour
        trending = stacked.reindex(df.index, method="ffill") > regime_thresh

    def is_trending(ts):
        if trending is None:
            return False
        v = trending.get(ts)
        return bool(v) if v is not None else False

    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        sampled = [i for i, ts in enumerate(idx) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            sma = float(r["sma20"]); std = float(r["std20"]); rs = float(r["rsi"])
            zz = float(r["z"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, sma, std, rs, zz, vw, dev)):
                continue
            upper = sma + z_t * std
            lower = sma - z_t * std
            setup_long = (zz <= -z_t and close < lower and rs < rsi_os and dev <= -vdev)
            setup_short = (zz >= z_t and close > upper and rs > rsi_ob and dev >= vdev)
            blocked = is_trending(idx[i])
            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and close <= max_price and not blocked:
                        state[key] = {"stage": "watch", "sl": low, "sh": high,
                                      "eh": high, "el": low, "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], high)
                    if close > upper:
                        st["sl"] = low; st["ck"] += 1
                    elif low < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + buf)
                        risk = stop - entry
                        rr = (entry - vw) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "SHORT", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], low)
                    if close < lower:
                        st["sh"] = high; st["ck"] += 1
                    elif high > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - buf)
                        risk = entry - stop
                        rr = (vw - entry) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "LONG", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if state.get(key, {}).get("stage") == "watch" and state[key]["ck"] > max_watch:
                    state[key]["stage"] = "expired"
    return rows


def gen_orb_fib(df, p):
    """A — Fibonacci-pullback ORB. Detect the 15-min OR break (signal only, same
    VWAP+volume filter as gen_orb), let the thrust peak, then enter on a pullback to
    the fib retracement (resting-limit style -> immune to manual-approval latency).
    Stop past the deeper fib; target = swing extreme (+ optional extension)."""
    or_end = p["or_end"]; thrust = p["thrust_bars"]; fib_e = p["fib_entry"]
    fib_s = p["fib_stop"]; tgt_ext = p.get("target_ext", 0.0); ent_win = p["entry_window"]
    use_vwap = p["use_vwap"]; use_vol = p["use_vol"]; vol_mult = p["vol_mult"]; max_price = p["max_price"]
    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        times = np.array([t.time() for t in dd.index])
        orb = dd[times < or_end]; post = dd[times >= or_end]
        if len(orb) < 1 or len(post) < thrust + 3:
            continue
        orh = float(orb["High"].max()); orl = float(orb["Low"].min())
        if orh - orl <= 0 or orh > max_price:
            continue
        pH = post["High"].to_numpy(); pL = post["Low"].to_numpy(); pC = post["Close"].to_numpy()
        pV = post["vwap"].to_numpy(); pA = post["avgvol"].to_numpy(); pVol = post["Volume"].to_numpy()
        n = len(post)

        bj = None; side = None
        for j in range(n):
            up = pC[j] > orh; dn = pC[j] < orl
            if not (up or dn):
                continue
            side = "LONG" if up else "SHORT"
            relvol = pVol[j] / pA[j] if pA[j] > 0 else 0
            take_vwap = (pC[j] > pV[j]) if side == "LONG" else (pC[j] < pV[j])
            if use_vwap and not take_vwap:
                break
            if use_vol and relvol < vol_mult:
                break
            bj = j
            break
        if bj is None:
            continue

        end = min(bj + thrust, n - 1)
        if side == "LONG":
            sp = bj + int(np.argmax(pH[bj:end + 1]))
            swing = float(pH[sp]); rng = swing - orl
            if rng <= 0:
                continue
            entry = swing - fib_e * rng; stop = swing - fib_s * rng; target = swing + tgt_ext * rng
            risk = entry - stop
            if risk <= 0 or entry <= 0 or risk / entry < MIN_RISK_FRAC:
                continue
            fill = None
            for k in range(sp + 1, min(sp + 1 + ent_win, n)):
                if pL[k] <= entry:
                    fill = k; break
            if fill is None:
                continue
            out = sim_forward("LONG", entry, stop, target, pH[fill:], pL[fill:], pC[fill:])
        else:
            sp = bj + int(np.argmin(pL[bj:end + 1]))
            swing = float(pL[sp]); rng = orh - swing
            if rng <= 0:
                continue
            entry = swing + fib_e * rng; stop = swing + fib_s * rng; target = swing - tgt_ext * rng
            risk = stop - entry
            if risk <= 0 or entry <= 0 or risk / entry < MIN_RISK_FRAC:
                continue
            fill = None
            for k in range(sp + 1, min(sp + 1 + ent_win, n)):
                if pH[k] >= entry:
                    fill = k; break
            if fill is None:
                continue
            out = sim_forward("SHORT", entry, stop, target, pH[fill:], pL[fill:], pC[fill:])

        if out is not None:
            rows.append((str(day), side, out, risk / entry))
    return rows


# ----------------------------------------------------------------------------
# Volume Profile (HVN/LVN) helpers for gen_volprofile, DAILY-bar only.
# ----------------------------------------------------------------------------
def compute_volume_profile(window_df, bins=50):
    """Volume-by-price histogram over window_df (needs High/Low/Volume). Each bar's
    volume is split evenly across every bucket its High-Low range touched (not just
    its Close), so wide-range bars correctly contribute liquidity across the levels
    they traded through. Returns (bin_centers, bin_volume), both low->high."""
    if window_df.empty:
        return np.array([]), np.array([])
    lo = float(window_df["Low"].min()); hi = float(window_df["High"].max())
    if hi <= lo:
        return np.array([]), np.array([])
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    H = window_df["High"].to_numpy(); L = window_df["Low"].to_numpy(); V = window_df["Volume"].to_numpy()
    for h, l, v in zip(H, L, V):
        if h <= l or v <= 0:
            continue
        b0 = max(0, min(int(np.searchsorted(edges, l, side="right")) - 1, bins - 1))
        b1 = max(0, min(int(np.searchsorted(edges, h, side="right")) - 1, bins - 1))
        n = b1 - b0 + 1
        vol[b0:b1 + 1] += v / n
    centers = (edges[:-1] + edges[1:]) / 2
    return centers, vol


def find_hvn_lvn(centers, vol, sep_pct=0.025, smooth_sigma=1.5, prominence_frac=0.10):
    """Local-extrema HVN/LVN detection (replaces the old global-quantile cutoff, which
    just flagged the broad upper third of bins -- i.e. wherever price recently sat --
    instead of distinct nodes; that bug was killing R:R on the volprofile strategy by
    handing back a 'nearest HVN' essentially adjacent to entry).

    1. Gaussian-smooth the volume profile first (kills tick/single-bin noise so one
       heavy print doesn't register as a fake peak).
    2. HVN = scipy.find_peaks on the smoothed profile, gated by PROMINENCE (a peak must
       stand out from its immediate surroundings, not just be locally high) and
       DISTANCE (minimum price separation between nodes, converted from sep_pct into
       bin units) so nodes are distinct, tradable zones rather than a stacked cluster.
    3. LVN = the identical find_peaks call on the INVERTED smoothed profile -- peaks of
       -vol are troughs of vol.
    Returns (hvn_prices, lvn_prices), sorted ascending."""
    if len(centers) < 5 or vol.sum() <= 0:
        return np.array([]), np.array([])
    smoothed = gaussian_filter1d(vol, sigma=smooth_sigma)
    bin_width = float(centers[1] - centers[0]) if len(centers) > 1 else 1.0
    price_ref = float(np.median(centers))
    distance = max(1, int(round((sep_pct * price_ref) / bin_width)))
    peak = float(smoothed.max())
    if peak <= 0:
        return np.array([]), np.array([])
    prominence = prominence_frac * peak

    hvn_idx, _ = find_peaks(smoothed, prominence=prominence, distance=distance)
    lvn_idx, _ = find_peaks(-smoothed, prominence=prominence, distance=distance)
    return np.sort(centers[hvn_idx]), np.sort(centers[lvn_idx])


def gen_volprofile(df, p):
    """MA-engagement strategy (heff's primary chart workflow, 2026-06-28): watch for
    DAILY price to reach the 50- or 200-day MA, trade the BOUNCE (wick rejection) or
    CONTINUATION (clean close-through with an expanded body) off it. TP routes to the
    next major MA or the nearest HVN in the trade's direction (whichever is closer); SL
    sits just beyond the entry MA, pushed past the nearest LVN if one sits close behind
    (so a standard MA wick doesn't stop you out inside dead air).

    STRICT T-1 vs T (no repaint): every MA, ATR, and volume-profile level used to judge
    day T's bar is computed ONLY from data through T-1 (.shift(1) on the MAs/ATR; the
    volume-profile window ends at t-1, excluding today's own bar). Day T's own OHLC is
    only used to RESOLVE the bounce/continuation/outcome against that frozen level --
    it never feeds the level it's being judged against. df = DAILY OHLCV (not the 5-min
    intraday cache the other generators use).
    """
    ma_fast = p.get("ma_fast", 50); ma_slow = p.get("ma_slow", 200)
    prox_mode = p.get("prox_mode", "pct")            # "pct" or "atr"
    prox_pct = p.get("prox_pct", 0.002)
    atr_period = p.get("atr_period", 14)
    atr_mult = p.get("atr_mult", 0.5)
    in_play_mult = p.get("in_play_mult", 3.0)         # MA "in play" if within in_play_mult x band
    body_mult = p.get("body_mult", 1.5)
    body_lookback = p.get("body_lookback", 20)
    vp_lookback = p.get("vp_lookback", 60)            # trailing window for HVN/LVN levels
    vp_bins = p.get("vp_bins", 50)
    vp_sep_pct = p.get("vp_sep_pct", 0.025)           # min price separation between nodes
    vp_smooth_sigma = p.get("vp_smooth_sigma", 1.5)
    vp_prominence_frac = p.get("vp_prominence_frac", 0.10)
    stop_buf_pct = p.get("stop_buf", 0.003)
    min_stop_pct = p.get("min_stop_pct", 0.015)       # stop-distance floor: max(this, min_stop_atr_mult x ATR)
    min_stop_atr_mult = p.get("min_stop_atr_mult", 1.0)
    min_rr = p.get("min_rr", 1.5)
    max_hold_days = p.get("max_hold_days", 40)        # time-stop: bound the forward walk to a
    # realistic swing-trade horizon. Without this, a trade that never hits stop/target gets
    # marked-to-market against whatever the LAST bar of the whole multi-year fetch happens to
    # be (the intraday generators avoid this for free -- their arrays are sliced per trading
    # day, so they always bottom out at EOD; this daily-bar generator has no such boundary).
    max_price = p.get("max_price", 10_000.0)
    max_target_frac = p.get("max_target_frac", 0.25)  # cap target search distance from entry
    sig_mode = p.get("signature_mode", "both")        # "both" (default, unchanged behavior) |
    # "bounce" (wick rejection only) | "continuation" (clean break only) -- added 2026-07-02 to
    # test whether blending two philosophically opposite bets (fade the MA vs ride the break)
    # was diluting/hiding a real edge in one with noise from the other.

    need = max(ma_slow, vp_lookback, body_lookback, atr_period) + 1
    if len(df) < need + 2:
        return []

    close = df["Close"]; high = df["High"]; low = df["Low"]; open_ = df["Open"]
    ma_f = close.rolling(ma_fast).mean().shift(1)
    ma_s = close.rolling(ma_slow).mean().shift(1)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean().shift(1)
    body = (close - open_).abs()
    body_avg = body.rolling(body_lookback).mean().shift(1)

    H = high.to_numpy(); L = low.to_numpy(); C = close.to_numpy()
    PC = prev_close.to_numpy(); MAF = ma_f.to_numpy(); MAS = ma_s.to_numpy()
    ATR = atr.to_numpy(); BODY = body.to_numpy(); BAVG = body_avg.to_numpy()
    idx = df.index

    rows = []
    for t in range(need, len(df) - 1):   # -1: sim_forward needs t+1.. to walk forward
        if np.isnan(MAF[t]) or np.isnan(MAS[t]) or np.isnan(ATR[t]) or np.isnan(BAVG[t]):
            continue
        price_ref = PC[t]   # yesterday's close: all we "know" walking into day t's session
        if price_ref <= 0 or price_ref > max_price:
            continue

        cands = []  # (which_ma, ma_level, touch_band, other_ma_level)
        for which, ma_lvl, other in (("fast", MAF[t], MAS[t]), ("slow", MAS[t], MAF[t])):
            band = (prox_pct * ma_lvl) if prox_mode == "pct" else (atr_mult * ATR[t])
            if abs(price_ref - ma_lvl) <= band * in_play_mult:
                cands.append((which, ma_lvl, band, other))
        if not cands:
            continue

        # volume profile from the trailing window ENDING at t-1 (today's bar excluded)
        window = df.iloc[max(0, t - vp_lookback):t]
        centers, vol = compute_volume_profile(window, bins=vp_bins)
        hvn, lvn = find_hvn_lvn(centers, vol, vp_sep_pct, vp_smooth_sigma, vp_prominence_frac)

        side = None; ma_lvl = None; other_ma = None
        for which, lvl, band, other in cands:
            touched = (L[t] <= lvl + band) and (H[t] >= lvl - band)
            if not touched:
                continue
            approach_from_below = price_ref < lvl
            pierced_above = H[t] > lvl + band
            pierced_below = L[t] < lvl - band
            closed_above = C[t] > lvl
            closed_below = C[t] < lvl
            big_body = BODY[t] >= body_mult * BAVG[t]

            cand_side = None
            if approach_from_below:
                if sig_mode in ("both", "bounce") and pierced_above and closed_below:
                    cand_side = "SHORT"        # tagged resistance from below, rejected back down
                elif sig_mode in ("both", "continuation") and closed_above and big_body:
                    cand_side = "LONG"         # broke clean through, continuation
            else:
                if sig_mode in ("both", "bounce") and pierced_below and closed_above:
                    cand_side = "LONG"         # tagged support from above, rejected back up
                elif sig_mode in ("both", "continuation") and closed_below and big_body:
                    cand_side = "SHORT"        # broke clean through, continuation
            if cand_side is not None:
                side, ma_lvl, other_ma = cand_side, lvl, other
                break   # first in-play MA that resolves wins; fast checked before slow
        if side is None:
            continue

        entry = float(C[t])   # decision made at today's (fully-formed) close, acted on next bar

        # ---- Stop: just beyond the entry MA, pushed past the nearest LVN if one sits close behind ----
        buf = stop_buf_pct * ma_lvl
        if side == "LONG":
            raw_stop = ma_lvl - buf
            behind = lvn[lvn <= raw_stop]
            stop = float(behind.max()) - buf if len(behind) else raw_stop
            stop = min(stop, entry - entry * MIN_RISK_FRAC)
        else:
            raw_stop = ma_lvl + buf
            behind = lvn[lvn >= raw_stop]
            stop = float(behind.min()) + buf if len(behind) else raw_stop
            stop = max(stop, entry + entry * MIN_RISK_FRAC)

        # ---- Stop-distance FLOOR: never tighter than max(min_stop_pct x entry, min_stop_atr_mult x ATR).
        # An MA/LVN-derived stop can land freakishly close on a quiet day (the NVDA 2025-09-03 case,
        # risk=0.57% -> RR=13.9) -- a thin-stop fluke, not a real edge. Push the stop OUT to the floor
        # (never tighter-in) before the R:R gate sees it, so the discovery sweep can't get pulled into
        # optimizing for micro-stop outliers.
        floor = max(min_stop_pct * entry, min_stop_atr_mult * ATR[t])
        if side == "LONG":
            stop = min(stop, entry - floor)
        else:
            stop = max(stop, entry + floor)

        # ---- Target: nearer of (the OTHER major MA) vs (nearest HVN), in the trade direction ----
        cap = entry * (1 + max_target_frac) if side == "LONG" else entry * (1 - max_target_frac)
        ma_target = other_ma if ((side == "LONG" and other_ma > entry) or
                                  (side == "SHORT" and other_ma < entry)) else None
        if side == "LONG":
            ahead = hvn[(hvn > entry) & (hvn <= cap)]
            hvn_target = float(ahead.min()) if len(ahead) else None
        else:
            ahead = hvn[(hvn < entry) & (hvn >= cap)]
            hvn_target = float(ahead.max()) if len(ahead) else None
        targets = [x for x in (ma_target, hvn_target) if x is not None]
        if not targets:
            continue
        target = min(targets) if side == "LONG" else max(targets)

        risk = abs(entry - stop); reward = abs(target - entry)
        if risk <= 0 or entry <= 0 or risk / entry < MIN_RISK_FRAC or reward / risk < min_rr:
            continue

        end = t + 1 + max_hold_days
        out = sim_forward(side, entry, stop, target, H[t + 1:end], L[t + 1:end], C[t + 1:end])
        if out is not None:
            rows.append((str(idx[t].date()), side, out, risk / entry))
    return rows


def gen_volprofile_sector(df, p):
    """Sector-rotation-gated MA-engagement/volume-profile swing strategy (2026-07-02, Pillar 3
    of the swing_scanner build). A THIN POST-FILTER over gen_volprofile's own output -- NOT a
    duplicated copy of its ~100-line body. This is safe because gen_volprofile's decision at
    day t depends only on data through t (T-1 levels + day t's own close), never on state
    carried from other days, so filtering the OUTPUT rows by date is exactly equivalent to
    gating inside the loop (same trade set either way) -- much less error-prone than
    maintaining two parallel copies of the core logic in sync (the pattern gen_mean_rev_sector/
    gen_orb_sector use instead, acceptable there since those generators are much shorter).

    LONG signals require the ticker's sector to be HOT (Leading/Improving) on the prior closed
    day; SHORT signals require it to be COLD (Lagging/Weakening). Unmapped ticker or no prior
    rotation data -> fail-open (kept), same discipline as every other sector gate in this file."""
    rows = gen_volprofile(df, p)
    ticker = p.get("_ticker")
    out = []
    for date_str, side, outcome, risk_frac in rows:
        quad = sector_quadrant_on(date_str, ticker)
        if quad is None:
            out.append((date_str, side, outcome, risk_frac))          # fail-open: unmapped/no data
            continue
        if side == "LONG" and quad in ("Leading", "Improving"):
            out.append((date_str, side, outcome, risk_frac))
        elif side == "SHORT" and quad in ("Lagging", "Weakening"):
            out.append((date_str, side, outcome, risk_frac))
        # else: gated out (sector doesn't confirm this side's direction)
    return out


def gen_orb_vwapdev(df, p):
    """VWAP-deviation-gated ORB (2026-07-08 research, thread 3): identical mechanic to
    gen_orb_sector (sector gate) / gen_orb (base breakout), but the additional gate here is
    on how far price has ALREADY moved from session VWAP at the moment of breakout -- the
    idea being a breakout accompanied by a strong existing VWAP separation is a stronger
    "the crowd is already leaning this way" signal than a breakout with price still hugging
    VWAP. min_vwapdev_frac=None disables the gate (falls back to plain gen_orb/gen_orb_sector
    behavior). Composable with max_range_frac (tight-range) and sector gate via the separate
    comp_orb_vwapdev / comp_orb_vwapdev_sector wrappers -- this function itself only adds the
    VWAP-deviation gate on top of the existing base-ORB filter stack (use_vwap/use_vol/
    max_range_frac), so it is directly comparable to gen_orb with those same params."""
    or_end = p["or_end"]; vol_mult = p["vol_mult"]; use_vwap = p["use_vwap"]
    use_vol = p["use_vol"]; max_price = p["max_price"]
    max_range_frac = p.get("max_range_frac")
    min_vwapdev_frac = p.get("min_vwapdev_frac")   # None = no gate; else require |vwap_dev| >= this at entry bar
    ticker = p.get("_ticker")
    sector_gate = p.get("sector_gate", False)

    hot_cache = {}

    def hot_on(day):
        if not sector_gate:
            return True
        ds = str(day)
        if ds not in hot_cache:
            v = sector_hot_on(ds, ticker)
            hot_cache[ds] = True if v is None else v
        return hot_cache[ds]

    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        if not hot_on(day):
            continue
        times = np.array([t.time() for t in dd.index])
        orb = dd[times < or_end]; post = dd[times >= or_end]
        if len(orb) < 1 or len(post) < 2:
            continue
        orh = float(orb["High"].max()); orl = float(orb["Low"].min())
        rng = orh - orl
        if rng <= 0 or orh > max_price:
            continue
        pH = post["High"].to_numpy(); pL = post["Low"].to_numpy(); pC = post["Close"].to_numpy()
        pO = post["Open"].to_numpy(); pV = post["vwap"].to_numpy(); pA = post["avgvol"].to_numpy()
        pVol = post["Volume"].to_numpy(); pDev = post["vwap_dev"].to_numpy()
        for j in range(len(post)):
            up = pH[j] >= orh; dn = pL[j] <= orl
            if not (up or dn):
                continue
            if up and dn:
                side = "LONG" if pC[j] >= pO[j] else "SHORT"
            else:
                side = "LONG" if up else "SHORT"
            entry = orh if side == "LONG" else orl
            stop = orl if side == "LONG" else orh
            if entry <= 0 or rng / entry < MIN_RISK_FRAC:
                break
            if max_range_frac is not None and rng / entry > max_range_frac:
                break
            relvol = pVol[j] / pA[j] if pA[j] > 0 else 0
            take_vwap = (pC[j] > pV[j]) if side == "LONG" else (pC[j] < pV[j])
            take_vol = relvol >= vol_mult
            if use_vwap and not take_vwap:
                break
            if use_vol and not take_vol:
                break
            if min_vwapdev_frac is not None:
                dev = pDev[j]
                if np.isnan(dev) or abs(dev) < min_vwapdev_frac:
                    break
            out = sim_forward(side, entry, stop, None, pH[j:], pL[j:], pC[j:])
            if out is not None:
                rows.append((str(day), side, out, rng / entry))
            break
    return rows


def gen_mr_volband(df, p):
    """Volatility-band-gated mean reversion (2026-07-08 research, thread 3): identical
    two-stage MR mechanic as gen_mean_rev, but adds a "not too quiet, not too wild" gate
    using std20/sma20 (relative 20-bar realized vol, an ATR-like proxy already available
    from the existing prep() columns -- no new intraday column needed) compared to that
    SAME ratio's own trailing rolling average as of the prior bar (no look-ahead). A setup
    is only allowed to arm if the current relvol ratio sits within
    [vol_band_lo, vol_band_hi] x its own rolling mean. vol_band_lo/hi = (0, inf) disables
    the gate (falls back to plain gen_mean_rev behavior)."""
    z_t = p["z"]; rsi_os = p["rsi_os"]; rsi_ob = p["rsi_ob"]; vdev = p["vdev"]
    min_rr = p["min_rr"]; buf = p["stop_buf"]; max_price = p["max_price"]; max_watch = p["max_watch"]
    vol_band_lo = p.get("vol_band_lo", 0.0)
    vol_band_hi = p.get("vol_band_hi", float("inf"))
    vol_avg_window = p.get("vol_avg_window", 78)   # ~1 trading day of 5-min bars

    relvol_series = (df["std20"] / df["sma20"]).replace([np.inf, -np.inf], np.nan)
    relvol_avg = relvol_series.rolling(vol_avg_window, min_periods=20).mean().shift(1)
    ratio_series = (relvol_series / relvol_avg)

    rows = []
    for day, dd in df.groupby(df.index.date, sort=True):
        dd = dd.dropna(subset=["sma20", "std20", "rsi", "vwap"])
        if dd.empty:
            continue
        H = dd["High"].to_numpy(); L = dd["Low"].to_numpy(); C = dd["Close"].to_numpy()
        idx = dd.index
        ratio_local = ratio_series.reindex(idx)
        sampled = [i for i, ts in enumerate(idx) if ts.minute % 15 == 0]
        state = {}
        for i in sampled:
            r = dd.iloc[i]
            close = float(r["Close"]); high = float(r["High"]); low = float(r["Low"])
            sma = float(r["sma20"]); std = float(r["std20"]); rs = float(r["rsi"])
            zz = float(r["z"]); vw = float(r["vwap"]); dev = float(r["vwap_dev"])
            if any(np.isnan(v) for v in (close, sma, std, rs, zz, vw, dev)):
                continue
            ratio = ratio_local.iloc[i]
            vol_ok = (not np.isnan(ratio)) and (vol_band_lo <= ratio <= vol_band_hi)
            upper = sma + z_t * std
            lower = sma - z_t * std
            setup_long = (zz <= -z_t and close < lower and rs < rsi_os and dev <= -vdev and vol_ok)
            setup_short = (zz >= z_t and close > upper and rs > rsi_ob and dev >= vdev and vol_ok)
            for side, setup, key in (("SHORT", setup_short, "SHORT"), ("LONG", setup_long, "LONG")):
                st = state.get(key)
                if st is None:
                    if setup and close <= max_price:
                        state[key] = {"stage": "watch", "sl": low, "sh": high,
                                      "eh": high, "el": low, "ck": 0}
                    continue
                if st["stage"] != "watch":
                    continue
                if side == "SHORT":
                    st["eh"] = max(st["eh"], high)
                    if close > upper:
                        st["sl"] = low; st["ck"] += 1
                    elif low < st["sl"]:
                        entry = st["sl"]; stop = st["eh"] * (1 + buf)
                        risk = stop - entry
                        rr = (entry - vw) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("SHORT", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "SHORT", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                else:
                    st["el"] = min(st["el"], low)
                    if close < lower:
                        st["sh"] = high; st["ck"] += 1
                    elif high > st["sh"]:
                        entry = st["sh"]; stop = st["el"] * (1 - buf)
                        risk = entry - stop
                        rr = (vw - entry) / risk if risk > 0 else 0
                        if rr >= min_rr and entry > 0 and risk / entry >= MIN_RISK_FRAC:
                            st["stage"] = "triggered"
                            out = sim_forward("LONG", entry, stop, vw, H[i + 1:], L[i + 1:], C[i + 1:])
                            if out is not None:
                                rows.append((str(day), "LONG", out, risk / entry))
                        else:
                            st["stage"] = "expired"
                    else:
                        st["ck"] += 1
                if state.get(key, {}).get("stage") == "watch" and state[key]["ck"] > max_watch:
                    state[key]["stage"] = "expired"
    return rows


GEN = {"mean_rev": gen_mean_rev, "orb": gen_orb, "vwap_rev": gen_vwap_rev,
       "close_drift": gen_close_drift, "vol_reversion": gen_vol_reversion,
       "earnings_reversion": gen_earnings_reversion, "overnight_gap": gen_overnight_gap,
       "microstructure": gen_microstructure,
       "mean_rev_regime": gen_mean_rev_regime, "orb_fib": gen_orb_fib,
       "mean_rev_vix": gen_mean_rev_vix, "mean_rev_riskoff": gen_mean_rev_riskoff,
       "orb_regime": gen_orb_regime, "volprofile": gen_volprofile,
       "mean_rev_sector": gen_mean_rev_sector, "orb_sector": gen_orb_sector,
       "volprofile_sector": gen_volprofile_sector,
       "orb_vwapdev": gen_orb_vwapdev, "mr_volband": gen_mr_volband}


# ----------------------------------------------------------------------------
# component generation (each unique config produced once) + portfolio scoring
# ----------------------------------------------------------------------------
def component_key(comp):
    return comp["base"] + ":" + json.dumps(comp["p"], sort_keys=True, default=str)


def generate_component(comp, cached):
    # disk cache: an identical component (same base+params) is generated only once,
    # so iterating on new edges never re-pays for the expensive ORB/mean-rev passes.
    COMP_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(component_key(comp).encode()).hexdigest()[:12]
    cf = COMP_DIR / f"{comp['base']}_{h}.parquet"
    if cf.exists():
        return pd.read_parquet(cf)
    gen = GEN[comp["base"]]
    rows = []
    for sym, df in cached:
        # per-symbol ticker is stamped into a SHALLOW COPY of p (comp["p"] itself, which the
        # cache key hashes, is untouched) so sector-aware generators can look up SECTOR_MAP
        # without changing the calling convention for every other gen_ function.
        pp = dict(comp["p"], _ticker=sym)
        rows.extend(gen(df, pp))
    out = pd.DataFrame(rows, columns=["date", "side", "r_gross", "risk_frac"])
    if not out.empty:
        out["net_r"] = out["r_gross"] - (COST_BPS / 10000.0) / out["risk_frac"].clip(lower=MIN_RISK_FRAC)
    else:
        out["net_r"] = []
    out.to_parquet(cf)
    return out


def date_split(all_dates):
    ds = sorted(set(all_dates))
    cut = int(len(ds) * SEARCH_FRAC)
    return set(ds[:cut]), set(ds[cut:])


def score_portfolio(comp_keys, comp_trades, region):
    """Net total R + quality metrics for a portfolio over a date-set `region`."""
    frames = []
    for k in comp_keys:
        t = comp_trades[k]
        if not t.empty:
            frames.append(t[t["date"].isin(region)])
    if not frames:
        return dict(total_r=0.0, n=0, per_trade=0.0, sharpe=0.0, corr=None)
    allt = pd.concat(frames, ignore_index=True)
    n = len(allt)
    total = float(allt["net_r"].sum())
    per = total / n if n else 0.0
    daily = allt.groupby("date")["net_r"].sum()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    corr = None
    if len(frames) == 2:
        a = frames[0].groupby("date")["net_r"].sum()
        b = frames[1].groupby("date")["net_r"].sum()
        days = sorted(set(a.index) | set(b.index))
        if len(days) > 2:
            corr = float(np.corrcoef(a.reindex(days, fill_value=0),
                                     b.reindex(days, fill_value=0))[0, 1])
    return dict(total_r=total, n=n, per_trade=per, sharpe=sharpe, corr=corr)


# ----------------------------------------------------------------------------
# candidate definitions
# ----------------------------------------------------------------------------
def comp_mr(name, **over):
    p = dict(z=2.0, rsi_os=30, rsi_ob=70, vdev=0.015, min_rr=1.5,
             stop_buf=0.0015, max_price=250.0, max_watch=12)
    p.update(over)
    return {"base": "mean_rev", "name": name, "p": p}


def comp_orb(name, **over):
    p = dict(or_end=dtime(9, 45), vol_mult=1.5, use_vwap=True, use_vol=True, max_price=250.0)
    p.update(over)
    return {"base": "orb", "name": name, "p": p}


def comp_vwr(name, **over):
    p = dict(vdev=0.015, rsi_os=30, rsi_ob=70, stop_buf=0.0015, max_price=250.0, min_rr=1.0)
    p.update(over)
    return {"base": "vwap_rev", "name": name, "p": p}


def comp_cld(name, **over):
    p = dict(decide_time=dtime(15, 0), mode="mom", thresh=0.003, stop_bars=6,
             stop_buf=0.0015, max_price=250.0)
    p.update(over)
    return {"base": "close_drift", "name": name, "p": p}


def comp_volr(name, **over):
    p = dict(vol_thresh=2.0, hold_until=dtime(9, 45), max_price=250.0, stop_buf=0.0015)
    p.update(over)
    return {"base": "vol_reversion", "name": name, "p": p}


def load_earnings_cache():
    """Load earnings dates from JSON; return set of date strings for earnings days."""
    try:
        with open(EARN_CACHE) as f:
            data = json.load(f)
        all_dates = set()
        for ticker, dates in data.get("tickers", {}).items():
            all_dates.update(dates)
        return all_dates
    except Exception:
        return set()


def comp_earn(name, earn_dates, **over):
    p = dict(earn_dates=tuple(sorted(earn_dates)), hold_until=dtime(9, 45), max_price=250.0,
             stop_buf=0.0015, min_rr=1.0)
    p.update(over)
    return {"base": "earnings_reversion", "name": name, "p": p}


def comp_ovn(name, **over):
    p = dict(gap_thresh=0.015, hold_until=dtime(9, 35), max_price=250.0, stop_buf=0.0015)
    p.update(over)
    return {"base": "overnight_gap", "name": name, "p": p}


def comp_micro(name, **over):
    p = dict(vol_spike_thresh=2.5, stop_buf=0.0015, max_price=250.0, min_rr=1.0)
    p.update(over)
    return {"base": "microstructure", "name": name, "p": p}


def comp_mrr(name, **over):
    # B: regime-filtered mean-rev. Defaults match MR_CAP (z=1.5, max_price=250) so
    # the ONLY difference vs baseline is the 1h-EMA regime gate.
    p = dict(z=1.5, rsi_os=30, rsi_ob=70, vdev=0.015, min_rr=1.5, stop_buf=0.0015,
             max_price=250.0, max_watch=12, regime_thresh=0.004, ema_fast=20, ema_slow=50)
    p.update(over)
    return {"base": "mean_rev_regime", "name": name, "p": p}


def comp_orbf(name, **over):
    # A: fib-pullback ORB. Defaults match ORB_CAP's filters so the ONLY difference
    # vs baseline ORB is the entry mechanic (fib pullback instead of breakout chase).
    p = dict(or_end=dtime(9, 45), thrust_bars=6, fib_entry=0.5, fib_stop=0.786,
             target_ext=0.0, entry_window=12, use_vwap=True, use_vol=True,
             vol_mult=1.5, max_price=250.0)
    p.update(over)
    return {"base": "orb_fib", "name": name, "p": p}


def comp_mrv(name, **over):
    # VIX-filtered mean-rev. Defaults match MR_CAP (z=1.5, max_price=250) so the ONLY
    # difference vs baseline is the VIX backwardation long-veto.
    p = dict(z=1.5, rsi_os=30, rsi_ob=70, vdev=0.015, min_rr=1.5, stop_buf=0.0015,
             max_price=250.0, max_watch=12, vix_veto_long_below=0.0)
    p.update(over)
    return {"base": "mean_rev_vix", "name": name, "p": p}


def comp_mro(name, **over):
    # Intraday risk-off-gated mean-rev. Defaults match MR_CAP (z=1.5, max_price=250) so
    # the ONLY difference vs baseline is the market-proxy LONG-veto (block longs when the
    # universe is down >= |riskoff_long_below| since the open). Reuses the MR cache shape.
    p = dict(z=1.5, rsi_os=30, rsi_ob=70, vdev=0.015, min_rr=1.5, stop_buf=0.0015,
             max_price=250.0, max_watch=12, riskoff_long_below=-0.0075)
    p.update(over)
    return {"base": "mean_rev_riskoff", "name": name, "p": p}


def comp_orbreg(name, **over):
    # Backtest A: 1h-EMA regime-gated ORB. Defaults match ORB_CAP's filters so the ONLY
    # difference vs baseline ORB is the higher-timeframe trend alignment gate.
    p = dict(or_end=dtime(9, 45), vol_mult=1.5, use_vwap=True, use_vol=True,
             max_price=250.0, ema_fast=20, ema_slow=50)
    p.update(over)
    return {"base": "orb_regime", "name": name, "p": p}


def comp_mrsec(name, **over):
    # Sector-rotation-gated mean-rev (2026-07-01). Defaults match MR_CAP (z=1.5, max_price=250)
    # so the ONLY difference vs baseline is the hot-sector day gate (sector_hot_on).
    p = dict(z=1.5, rsi_os=30, rsi_ob=70, vdev=0.015, min_rr=1.5, stop_buf=0.0015,
             max_price=250.0, max_watch=12)
    p.update(over)
    return {"base": "mean_rev_sector", "name": name, "p": p}


def comp_orbsec(name, **over):
    # Sector-rotation-gated ORB (2026-07-01). Defaults match ORB_CAP so the ONLY difference
    # vs baseline is the hot-sector day gate.
    p = dict(or_end=dtime(9, 45), vol_mult=1.5, use_vwap=True, use_vol=True, max_price=250.0)
    p.update(over)
    return {"base": "orb_sector", "name": name, "p": p}


def comp_orb_vwapdev(name, **over):
    # VWAP-deviation-gated ORB (2026-07-08 research, thread 3). Defaults match the LIVE
    # champion ORB leg (vol_mult=1.5, max_range_frac=0.0066, use_vwap/use_vol=True) so the
    # ONLY difference vs the deployed champion is the added min_vwapdev_frac gate (None =
    # gate off, reduces exactly to the live champion for a sanity-check baseline row).
    p = dict(or_end=dtime(9, 45), vol_mult=1.5, use_vwap=True, use_vol=True, max_price=250.0,
              max_range_frac=0.0066, min_vwapdev_frac=None, sector_gate=True)
    p.update(over)
    return {"base": "orb_vwapdev", "name": name, "p": p}


def comp_mr_volband(name, **over):
    # Volatility-band-gated mean-rev (2026-07-08 research, thread 3). Defaults match the
    # LIVE champion MR leg (z=1.5) so the ONLY difference vs the deployed champion is the
    # added vol_band_lo/hi gate (0.0/inf = gate off, reduces exactly to the live champion
    # for a sanity-check baseline row).
    p = dict(z=1.5, rsi_os=30, rsi_ob=70, vdev=0.015, min_rr=1.5, stop_buf=0.0015,
              max_price=250.0, max_watch=12, vol_band_lo=0.0, vol_band_hi=float("inf"),
              vol_avg_window=78)
    p.update(over)
    return {"base": "mr_volband", "name": name, "p": p}


def comp_vp(name, **over):
    p = dict(ma_fast=50, ma_slow=200, prox_mode="pct", prox_pct=0.002,
              atr_period=14, atr_mult=0.5, in_play_mult=3.0,
              body_mult=1.5, body_lookback=20,
              vp_lookback=60, vp_bins=50, vp_sep_pct=0.025, vp_smooth_sigma=1.5, vp_prominence_frac=0.10,
              stop_buf=0.003, min_stop_pct=0.015, min_stop_atr_mult=1.0, max_hold_days=40,
              min_rr=1.5, max_price=10_000.0, max_target_frac=0.25)
    p.update(over)
    return {"base": "volprofile", "name": name, "p": p}


def comp_vpsec(name, **over):
    # Sector-rotation-gated volume-profile swing strategy (2026-07-02). Same defaults as
    # comp_vp so the ONLY difference vs the base candidate is the hot/cold sector day gate.
    p = dict(ma_fast=50, ma_slow=200, prox_mode="pct", prox_pct=0.002,
              atr_period=14, atr_mult=0.5, in_play_mult=3.0,
              body_mult=1.5, body_lookback=20,
              vp_lookback=60, vp_bins=50, vp_sep_pct=0.025, vp_smooth_sigma=1.5, vp_prominence_frac=0.10,
              stop_buf=0.003, min_stop_pct=0.015, min_stop_atr_mult=1.0, max_hold_days=40,
              min_rr=1.5, max_price=10_000.0, max_target_frac=0.25)
    p.update(over)
    return {"base": "volprofile_sector", "name": name, "p": p}


MR_BASE = comp_mr("mr_base")
ORB_BASE = comp_orb("orb_base")
EARN_DATES = load_earnings_cache()

# Each candidate is a PORTFOLIO (list of components). #0 is the baseline combo.
# MAX_PRICE TEST: fractional shares remove the affordability reason for the $250
# cap, but high-priced names may mean-revert differently -> backtest before going
# live. Compare capped baseline vs no-cap, and isolate each leg.
# Capped components reuse the existing wf_comp cache; only the no-cap pair regen.
MR_CAP = comp_mr("mr_z1p5_cap", z=1.5, max_price=250.0)        # == cached winner
MR_NOCAP = comp_mr("mr_z1p5_nocap", z=1.5, max_price=1e9)
ORB_CAP = comp_orb("orb_cap", max_price=250.0)                 # == cached orb_base
ORB_NOCAP = comp_orb("orb_nocap", max_price=1e9)

# --- INTRADAY RISK-OFF test (2026-06-18): does vetoing mean-rev LONGS when the market
# is selling off hard intraday help? Targets the 2026-06-17 failure (27 LONG fades into
# a rate-hike selloff -> ~-5.6R / -$4.93). Baseline = current validated config
# (mr_z1.5 + orb, both $250-cached -> instant). Risk-off MR variants are a fast mean-rev
# pass; ORB_CAP reused from cache. NOTE: requires `--build-proxy` to have run first.
# (Risk-off MR long-veto was tested 2026-06-18 -> MIRAGE, did NOT carry on holdout; shelved.)

# (Regime-ORB tested 2026-06-23 -> no-improve, shelved. Time-of-day 2026-06-24 -> no-adopt.)

# --- TIGHT-RANGE ORB (2026-06-24): the ORB edge analysis found the smallest-tercile opening
# range carried the holdout (+0.121 vs +0.040R/tr, ~3x). Test a max-range-frac cap on the ORB
# leg as ONE clean hypothesis (no time gate). Search terciles: q33=0.658%, q50=0.825%.
# Baseline MR leg unchanged (cached); ORB_CAP cached; only the orb_t* components regen.
ORB_T060 = comp_orb("orb_t0.60", max_range_frac=0.0060)   # ~q27 (tighter than the winning tercile)
ORB_T066 = comp_orb("orb_t0.66", max_range_frac=0.0066)   # ~q33 (the tercile that carried)
ORB_T080 = comp_orb("orb_t0.80", max_range_frac=0.0080)   # ~q48 (looser, ~half the trades)
ORB_T100 = comp_orb("orb_t1.00", max_range_frac=0.0100)   # ~q65

CANDIDATES = [
    {"name": "baseline (mr_z1.5 + orb)",   "comps": [MR_CAP, ORB_CAP]},
    {"name": "mr + tight-ORB <=0.60%",     "comps": [MR_CAP, ORB_T060]},
    {"name": "mr + tight-ORB <=0.66%",     "comps": [MR_CAP, ORB_T066]},
    {"name": "mr + tight-ORB <=0.80%",     "comps": [MR_CAP, ORB_T080]},
    {"name": "mr + tight-ORB <=1.00%",     "comps": [MR_CAP, ORB_T100]},
]

# --- SECTOR ROTATION test (2026-07-01): does restricting the champion strategy to
# names whose sector is currently "hot" (Leading/Improving RRG quadrant vs SPY) improve
# quality? Isolates each leg (MR-only, ORB-only gated) plus both together, against the
# SAME baseline. Requires data/sector_map.json + data/sector_rotation.csv (sector_rotation.py
# --all) to have been built first; sector_hot_on() fails open (never blocks) if either is
# missing, so an un-built signal just reduces to the baseline (safe default, not a crash).
MR_SECTOR = comp_mrsec("mr_sector")
ORB_SECTOR = comp_orbsec("orb_sector")

SECTOR_CANDIDATES = [
    {"name": "baseline (mr_z1.5 + orb)",        "comps": [MR_CAP, ORB_CAP]},
    {"name": "sector-gated MR + baseline ORB",  "comps": [MR_SECTOR, ORB_CAP]},
    {"name": "baseline MR + sector-gated ORB",  "comps": [MR_CAP, ORB_SECTOR]},
    {"name": "sector-gated MR + sector-gated ORB", "comps": [MR_SECTOR, ORB_SECTOR]},
]


def tg(msg):
    try:
        subprocess.run(["openclaw", "message", "send", "--channel", "telegram",
                        "--target", TG_TARGET, "--message", msg],
                       timeout=30, check=False)
    except Exception:
        pass


def fmt(s):
    c = f" corr {s['corr']:+.2f}" if s.get("corr") is not None else ""
    return (f"net {s['total_r']:+7.1f}R | {s['n']:>4} tr | {s['per_trade']:+.3f}R/tr "
            f"| Sharpe {s['sharpe']:.2f}{c}")


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="comma list; also forces a fresh cache build on this subset")
    ap.add_argument("--build-cache", action="store_true", help="(re)build the prepped-bar cache then exit")
    ap.add_argument("--build-cache-alpaca", action="store_true",
                    help="(re)build the prepped-bar cache from Alpaca (not Databento) -- for "
                         "symbols not covered by the purchased Databento chunks; use with --symbols")
    ap.add_argument("--build-proxy", action="store_true",
                    help="(re)build data/mkt_proxy.csv (intraday market-drift gate input) then exit")
    ap.add_argument("--sector-test", action="store_true",
                    help="score SECTOR_CANDIDATES (hot-sector gate on MR/ORB) instead of CANDIDATES")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    global CANDIDATES
    if a.sector_test:
        CANDIDATES = SECTOR_CANDIDATES

    if a.build_cache_alpaca:
        if not a.symbols:
            raise SystemExit("--build-cache-alpaca requires --symbols (comma list)")
        target = [s.strip().upper() for s in a.symbols.split(",")]
        log.info(f"building Alpaca-sourced prepped-bar cache for {len(target)} symbols...")
        n = build_cache_alpaca(target, quiet=a.quiet)
        log.info(f"cached {n}/{len(target)} symbols -> {CACHE_DIR}")
        return

    syms = wide_universe.load_universe(rebuild_if_stale=False) or mr.fetch_sp100()
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",")]

    # build cache if asked, if missing, or if a custom symbol subset was given
    need_build = a.build_cache or a.symbols or not any(CACHE_DIR.glob("*.parquet"))
    if need_build:
        log.info(f"building prepped-bar cache for {len(syms)} symbols...")
        n = build_cache(syms, quiet=a.quiet)
        log.info(f"cached {n} symbols -> {CACHE_DIR}")
        if a.build_cache:
            return

    cached = load_cached(syms)
    if not cached:
        raise SystemExit("no cached symbols; run with --build-cache first")
    log.info(f"loaded {len(cached)} cached symbols")

    if a.build_proxy:
        n = build_proxy(cached)
        log.info(f"built market proxy: {n} 5-min bars -> {MKT_PROXY_CSV}")
        log.info("re-run without --build-proxy to score the risk-off candidates against it.")
        return

    # 1) generate every UNIQUE component once
    uniq = {}
    for cand in CANDIDATES:
        for c in cand["comps"]:
            uniq.setdefault(component_key(c), c)
    comp_trades = {}
    t0 = time.time()
    mr_ref = None  # daily net-R series of the first mean_rev component, for corr
    log.info("STANDALONE component diagnostics (full-sample, in-sample read):")
    for i, (k, c) in enumerate(uniq.items(), 1):
        t = generate_component(c, cached)
        comp_trades[k] = t
        n = len(t)
        tot = float(t["net_r"].sum()) if n else 0.0
        per = tot / n if n else 0.0
        daily = t.groupby("date")["net_r"].sum() if n else pd.Series(dtype=float)
        if c["base"] == "mean_rev" and mr_ref is None:
            mr_ref = daily
        cc = ""
        if mr_ref is not None and n and c["base"] != "mean_rev":
            days = sorted(set(daily.index) | set(mr_ref.index))
            if len(days) > 2:
                corr = np.corrcoef(daily.reindex(days, fill_value=0),
                                   mr_ref.reindex(days, fill_value=0))[0, 1]
                cc = f" | corr→mr {corr:+.2f}"
        line = (f"  [{i}/{len(uniq)}] {c['name']:<14} {n:>5} tr | "
                f"net {tot:+7.1f}R | {per:+.3f}R/tr{cc} ({time.time()-t0:.0f}s)")
        log.info(line)
        # surface the close-edge reads to Telegram as they land
        if c["base"] == "close_drift":
            tg(f"🕒 {c['name']}: net {tot:+.1f}R, {per:+.3f}R/tr, {n} tr{cc}")

    all_dates = []
    for t in comp_trades.values():
        all_dates += t["date"].tolist()
    search, holdout = date_split(all_dates)

    # 2) ratchet over portfolio candidates, scoring on the SEARCH region only
    led = []
    keys = lambda cand: [component_key(c) for c in cand["comps"]]
    base = CANDIDATES[0]
    base_s = score_portfolio(keys(base), comp_trades, search)
    R0 = base_s["total_r"]
    target = R0 * TARGET_MULT
    champ = base; champ_s = base_s
    line0 = f"BASELINE {base['name']}: {fmt(base_s)}  -> target {target:+.1f}R (+15%)"
    log.info("=" * 92); log.info(line0)
    tg(f"🔬 WF search started.\n{line0}")
    led.append(dict(step=0, name=base["name"], **{k: base_s.get(k) for k in
                    ("total_r", "n", "per_trade", "sharpe", "corr")}, decision="baseline"))

    hit = False
    for step, cand in enumerate(CANDIDATES[1:], 1):
        s = score_portfolio(keys(cand), comp_trades, search)
        if s["n"] < MIN_TRADES or s["per_trade"] <= 0:
            decision = "DEAD"
        elif s["total_r"] > champ_s["total_r"]:
            decision = "RATCHET-UP"; champ = cand; champ_s = s
        else:
            decision = "no-improve"
        flag = ""
        if decision == "RATCHET-UP" and s["per_trade"] < champ_s["per_trade"] * 0.9 and False:
            flag = "  ⚠frequency?"
        msg = f"[{step:>2}] {cand['name']:<26} {fmt(s)}  -> {decision}{flag}"
        log.info(msg)
        led.append(dict(step=step, name=cand["name"], **{k: s.get(k) for k in
                        ("total_r", "n", "per_trade", "sharpe", "corr")}, decision=decision))
        if decision == "RATCHET-UP":
            tg(f"⬆️ ratchet: {cand['name']} {fmt(s)} (champ now {champ_s['total_r']:+.1f}R / target {target:+.1f}R)")
        if R0 > 0 and champ is not base and champ_s["total_r"] >= target:
            hit = True
            log.info(f">>> TARGET HIT on search region: champion '{champ['name']}' "
                     f"{champ_s['total_r']:+.1f}R >= {target:+.1f}R")
            break

    # 3) LOCKED HOLDOUT — the one honest test. Score baseline vs champion once.
    log.info("=" * 92)
    log.info("LOCKED HOLDOUT (untouched until now):")
    base_h = score_portfolio(keys(base), comp_trades, holdout)
    champ_h = score_portfolio(keys(champ), comp_trades, holdout)
    log.info(f"  baseline {base['name']:<22} {fmt(base_h)}")
    log.info(f"  champion {champ['name']:<22} {fmt(champ_h)}")
    if champ is base:
        real = "champion == baseline (no candidate beat it) — nothing to confirm"
    else:
        # sign-safe: how much the champion beat the baseline on each region (R deltas)
        d_search = champ_s["total_r"] - R0
        d_hold = champ_h["total_r"] - base_h["total_r"]
        carried = d_search > 0 and d_hold >= 0.5 * d_search
        real = (f"champ beat baseline by {d_search:+.1f}R on search vs {d_hold:+.1f}R on holdout -> "
                + ("✅ edge carried (real)" if carried else "⚠️ did NOT carry — likely overfit/mirage"))
    log.info(f"  verdict: {real}")

    pd.DataFrame(led).to_csv(LEDGER, index=False)
    CHAMP_OUT.write_text(json.dumps({"champion": champ["name"],
                                     "components": [c["name"] + ":" + json.dumps(c["p"], default=str)
                                                    for c in champ["comps"]],
                                     "search": champ_s, "holdout": champ_h,
                                     "baseline_search": base_s, "baseline_holdout": base_h,
                                     "target_hit": hit}, indent=2, default=str))
    summary = (f"🏁 WF search done.\nChampion: {champ['name']}\n"
               f"search {champ_s['total_r']:+.1f}R (base {R0:+.1f}, +15% target {'HIT' if hit else 'not hit'})\n"
               f"holdout: {real}\nledger -> data/wf_ledger.csv")
    log.info(summary)
    tg(summary)


if __name__ == "__main__":
    main()
