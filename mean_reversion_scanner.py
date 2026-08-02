#!/usr/bin/env python3
"""Mean reversion scanner -- two-stage (Watch -> Trigger).

Stage 1 (WATCH): price is stretched away from its 20-period mean on 5-min bars
  - abs(z-score) >= 2 vs SMA20
  - close beyond the 2-sigma Bollinger band
  - RSI(14) exhausted (<30 long / >70 short)
  - >= 1.5% deviation from session VWAP
  Records the setup and the break level; does NOT mean "enter".

Stage 2 (TRIGGER): the stretch starts releasing -> actionable entry.
  - SHORT: a later bar closes back INSIDE the upper band AND trades below the
           most recent extended bar low (break of the signal-bar low).
  - LONG : mirror (close back inside lower band + break of signal-bar high).
  Emits a trade ticket: entry / stop / target1(VWAP) / target2(SMA20) / R:R.
  Only fires if R:R >= MIN_RR. Each fired trigger is also appended as a
  structured record to data/mr_triggers_<date>.jsonl for the paper-trade logger.

Earnings filter: tickers inside their earnings blackout window (built daily by
build_earnings_cache.py) are skipped. Fail-open if the cache is missing/stale.

State persists across the 15-min cron runs in data/mr_state/<date>.json and
resets daily. Run: ./venv/bin/python mean_reversion_scanner.py --once
"""
import sys
import os
import json
import fcntl
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import yfinance as yf
import numpy as np
import pandas as pd

from log_setup import get_logger
# Two crons invoke this module under different flags at overlapping cadences (full scan
# */5, watch-only every minute). They used to share one get_logger("scanner_mr") sink --
# loguru's own per-process midnight-UTC rotation raced between the two processes' renames
# of the SAME logs/scanner_mr.log path (observed: FileNotFoundError, 2026-07-03). Giving
# watch-only its own agent name/file removes the shared path entirely.
log = get_logger("scanner_mr_watch" if "--watch-only" in sys.argv else "scanner_mr")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "mr_state"
MSG_PATH = DATA_DIR / "mr_message_latest.txt"
MR_WATCH_MSG_PATH = DATA_DIR / "mr_watch_message_latest.txt"  # fast trigger-loop output
LOCK_PATH = STATE_DIR / ".scan.lock"                          # serialize state R/W
EARN_CACHE = DATA_DIR / "earnings_cache.json"
ET = ZoneInfo("America/New_York")                            # used by the Alpaca fetch + helpers

LIVE_PARAMS_PATH = DATA_DIR / "live_params.json"
_LIVE_PARAMS_DEFAULT = {"mr": {"z": 1.5, "vdev": 0.015, "min_rr": 1.5},
                        "orb": {"vol_mult": 1.5, "max_range_frac": 0.0066,
                                "use_vwap": True, "use_vol": True},
                        # portfolio (2026-07-01): the SINGLE source of truth for slot count /
                        # concurrency / per-side cap / account capital -- unifies what used to
                        # be two independently-hardcoded constants (this file's NUM_SLOTS and
                        # portfolio_gate.py's MAX_CONCURRENT) that could silently drift apart.
                        # portfolio_gate.py reads this SAME file/key, not a copy of these values.
                        "portfolio": {"num_slots": 3, "max_concurrent": 3, "max_per_side": 2,
                                     "total_capital": 800.0}}


def load_live_params():
    """The trading-bot feedback loop's last-mile wire (closed 2026-06-28): a holdout-
    validated champion from continuous_search.py only matters if it's actually applied to
    the live scanners. promote_champion.py writes this file when the champion changes;
    both scanners read it at import time. Fail-open to the hardcoded defaults below (which
    match the values live before this loop existed) if the file is missing/corrupt, so a
    bad write can never silently break live trading."""
    try:
        data = json.loads(LIVE_PARAMS_PATH.read_text())
        return {"mr": {**_LIVE_PARAMS_DEFAULT["mr"], **data.get("mr", {})},
                "orb": {**_LIVE_PARAMS_DEFAULT["orb"], **data.get("orb", {})},
                "portfolio": {**_LIVE_PARAMS_DEFAULT["portfolio"], **data.get("portfolio", {})}}
    except Exception:
        return _LIVE_PARAMS_DEFAULT


_LIVE_PARAMS = load_live_params()
VWAP_DEV_PCT = _LIVE_PARAMS["mr"]["vdev"]
# Z_THRESH controls BOTH the Bollinger band width (sma +/- Z*std) and the
# z-score entry gate. Walk-forward sweep (99 sym, 2yr Databento, 6bp costs)
# found z=1.5 beats the old z=2.0 baseline: +0.203 vs +0.137 R/tr standalone,
# and the edge carried out-of-sample on the locked holdout (Sharpe 3.91 vs 2.60).
# Switched 2026-06-13 to log the new baseline live. Old value was 2.0.
# Now live-promotable -- see load_live_params() above; this default is the fail-open
# fallback only, not a second source of truth.
Z_THRESH = _LIVE_PARAMS["mr"]["z"]
STRATEGY_VERSION = f"mr_z{Z_THRESH}"   # tags trigger records so reports can isolate this cohort; tracks Z_THRESH so a promotion can't make this tag stale
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MIN_RR = _LIVE_PARAMS["mr"]["min_rr"]
STOP_BUFFER = 0.0015
# MAX_PRICE = the ORB cap (orb_scanner reads mrs.MAX_PRICE). Kept at $250 because the
# walk-forward test (2026-06-13) showed high-priced ORB breakouts OVERFIT — they look
# good in-sample but collapse out-of-sample (holdout Sharpe 1.41 -> 0.47 uncapped).
MAX_PRICE = 250.0
MAX_WATCH_CHECKS = 12

# --- whole-share position sizing (RH fractional shares can't hold our stops) ---
# TOTAL_CAPITAL/NUM_SLOTS now derive from live_params.json's "portfolio" block (2026-07-01
# unification) -- portfolio_gate.py reads the SAME key for MAX_CONCURRENT/MAX_PER_SIDE, so
# slot count and concurrency cap can never independently drift again.
TOTAL_CAPITAL = _LIVE_PARAMS["portfolio"]["total_capital"]   # account equity
NUM_SLOTS = _LIVE_PARAMS["portfolio"]["num_slots"]            # concurrent positions
MAX_RISK_PCT = 1.0      # max risk/trade = 1% of TOTAL_CAPITAL
# MR_MAX_PRICE = mean-rev cap. The backtest favored REMOVING it (high-priced names are
# good mean-rev signals), BUT Robinhood can't place stop/limit orders on FRACTIONAL shares
# (no stops; fractional limits auto-cancel in 5min; market = Not-Held slippage). This
# strategy needs precise limit entries + 0.7% hard stops -> WHOLE shares only. So cap at
# ONE SLOT's capital (TOTAL_CAPITAL/NUM_SLOTS), guaranteeing every signal is affordable as
# >=1 whole share -- this is DERIVED, not a separate hardcode, specifically so it can never
# fall out of sync with NUM_SLOTS again (previously a fixed $266 that would have silently
# started producing "0 sh SKIP" on $200-266 names the moment NUM_SLOTS changed without it).
# (ORB's MAX_PRICE=$250 above is a deliberate STRATEGY cap, not an affordability cap, so it
# intentionally does NOT scale with account size -- see its comment.)
MR_MAX_PRICE = TOTAL_CAPITAL / NUM_SLOTS

# --- A/B regime tag (carry-both for the paper month) ---
# Every z=1.5 trigger is logged regardless; we just TAG whether it passes a 1h
# EMA20/50 trend filter (regime_ok). The paper evaluator then tracks two cohorts:
# ALL triggers (plain z=1.5) vs regime_ok-only (filtered). Backtest showed the
# filter ~doubles per-trade R + Sharpe OOS at the cost of ~60% of volume; the
# month decides which fits the manual-gate book. NOT a block — just a label.
REGIME_THRESH = 0.008   # |ema20-ema50|/ema50 above this = trending -> regime_ok=False
REGIME_EMA_FAST = 20
REGIME_EMA_SLOW = 50
REGIME_DAYS = 25        # calendar days of 5-min history to warm the 1h EMA50

EMOJI_EYES = "\U0001F440"
EMOJI_TARGET = "\U0001F3AF"
EMOJI_CHART = "\U0001F4C8"


def fetch_sp100():
    try:
        url = "https://en.wikipedia.org/wiki/S%26P_100"
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"class": "wikitable"})
        tickers = []
        if table:
            for row in table.find_all("tr")[1:]:
                cols = row.find_all("td")
                if not cols:
                    continue
                sym = cols[0].get_text(strip=True).replace(".", "-")
                tickers.append(sym)
            if len(tickers) >= 50:
                return tickers[:100]
    except Exception:
        pass
    return [
        "AAPL","MSFT","AMZN","NVDA","GOOG","GOOGL","META","TSLA","BRK-B","JNJ",
        "V","UNH","PG","MA","HD","BAC","XOM","CVX","KO","PFE",
        "MRK","ABBV","WMT","DIS","NFLX","ORCL","INTC","CSCO","T","VZ",
        "CRM","MCD","NKE","SBUX","UPS","MMM","CAT","BA","LLY","COST",
        "ABT","TXN","QCOM","AMGN","DHR","BMY","MDLZ","PM","HON","AMAT",
        "GILD","ADP","SPG","BLK","SYK","RTX","GE","ISRG","ZTS","BKNG",
        "SPGI","NOW","TMUS","CVS","SCHW","PLD","ADI","LMT","ATVI","CL",
        "EMR","FDX","GD","HLT","ICE","ITW","KMB","KMI","KHC","MDT",
        "MET","FIS","CI","VRTX","REGN","GPN","MS","GS","AXP","PYPL",
        "ZM","DXCM","ROP","LRCX","MU","SQ","MAR","EW","NOC","PNC"
    ]


def earnings_blacklist(today_date):
    """Tickers in their earnings blackout window today (fail-open on any error)."""
    try:
        data = json.loads(EARN_CACHE.read_text())
        ds = today_date.isoformat()
        return {t for t, dates in data.get("tickers", {}).items() if ds in dates}
    except Exception:
        return set()


def market_is_open(now_et):
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now_et <= close_t


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    out = out.where(avg_loss != 0, 100.0)
    return out


ALPACA_KEY_PATH = ROOT / "credentials" / "alpaca_key.txt"
ALPACA_SECRET_PATH = ROOT / "credentials" / "alpaca_secret.txt"
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/{sym}/bars"


def alpaca_creds():
    k = os.environ.get("ALPACA_API_KEY")
    s = os.environ.get("ALPACA_API_SECRET")
    if k and s:
        return k.strip(), s.strip()
    if ALPACA_KEY_PATH.exists() and ALPACA_SECRET_PATH.exists():
        return ALPACA_KEY_PATH.read_text().strip(), ALPACA_SECRET_PATH.read_text().strip()
    return None, None


def fetch_5m_alpaca(ticker, days=5):
    """Real-time 5-min RTH bars from Alpaca (free IEX feed). Returns a df with an
    ET tz-aware index + Open/High/Low/Close/Volume cols, matching the yfinance
    shape the rest of the scanner expects. None if no creds / error / empty."""
    key, secret = alpaca_creds()
    if not key or not secret:
        return None
    sym = ticker.replace("-", ".")  # BRK-B -> BRK.B (Alpaca class-share symbology)
    start = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    try:
        r = requests.get(
            ALPACA_BARS_URL.format(sym=sym),
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            params={"timeframe": "5Min", "start": start, "feed": "iex",
                    "adjustment": "raw", "limit": 10000, "sort": "asc"},
            timeout=20)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars") or []
        if not bars:
            return None
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
        df = (df.set_index("t")
                .rename(columns={"o": "Open", "h": "High", "l": "Low",
                                 "c": "Close", "v": "Volume"}))
        df = df[["Open", "High", "Low", "Close", "Volume"]].between_time("09:30", "16:00")
        return df.dropna() if not df.empty else None
    except Exception:
        return None


def fetch_5m_yfinance(ticker, days=5):
    try:
        df = yf.download(ticker, period=f"{days}d", interval="5m", progress=False, threads=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception:
        return None


_BATCH_BARS = {}       # populated once per scan by prefetch_batch_bars(); get_5m_data() checks it first
_BATCH_REQUESTED = set()  # tickers that WERE part of a batch request (vs batch never attempted)
_CORE99 = None


def core99_set():
    """The original curated/backtested 99-name universe, cached after first call. Used to tag
    each trigger so the WIDE-universe cohort (untested, 2026-06-26) can be tracked separately
    from the proven track record rather than silently blended into it."""
    global _CORE99
    if _CORE99 is None:
        _CORE99 = set(fetch_sp100())
    return _CORE99


def prefetch_batch_bars(tickers, days):
    """Populate the batch cache once per scan via wide_universe's multi-symbol fetch (~57
    requests for the full ~5,600-name universe vs one request per ticker -- the per-symbol path
    doesn't scale past the curated 99). get_5m_data() below checks this first. Fails open to the
    existing per-symbol Alpaca/yfinance path ONLY if the batch call itself errors entirely (not
    for individual tickers Alpaca had no data for -- see get_5m_data)."""
    global _BATCH_BARS, _BATCH_REQUESTED
    try:
        import wide_universe as wu
        _BATCH_BARS = wu.fetch_bars_batch(tickers, days=days)
        _BATCH_REQUESTED = set(tickers)
    except Exception:
        _BATCH_BARS = {}
        _BATCH_REQUESTED = set()


def get_5m_data(ticker, days=5):
    """Batch-prefetched bars first (wide-universe scans). If a ticker WAS part of the batch
    request but Alpaca had no usable data for it, skip cleanly -- do NOT cascade into a
    per-symbol Alpaca+yfinance fallback, which is fine for the curated 99 but at ~5,600
    symbols turns a handful of misses into a multi-minute serial slowdown (this caused
    overlapping scanner runs to stack up on 2026-06-26, fixed same day). The per-symbol
    Alpaca/yfinance fallback path only runs for tickers that were never part of a batch at all
    (the watch-only fast loop, or total wide_universe failure)."""
    cached = _BATCH_BARS.get(ticker)
    if cached is not None and len(cached) >= 25:
        return cached
    if ticker in _BATCH_REQUESTED:
        return None
    df = fetch_5m_alpaca(ticker, days)
    if df is not None and len(df) >= 25:
        return df
    y = fetch_5m_yfinance(ticker, days)
    return y if y is not None else df


def session_vwap(df, today_date):
    todays = df[df.index.date == today_date]
    if todays.empty:
        return None
    typ = (todays["High"] + todays["Low"] + todays["Close"]) / 3.0
    vol = todays["Volume"]
    cum = vol.sum()
    if cum <= 0:
        return None
    return float((typ * vol).sum() / cum)


def compute(ticker, today_date):
    df = get_5m_data(ticker)
    if df is None or len(df) < 25:
        return None
    close = df["Close"]
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    rsi14 = rsi(close, 14)

    c = float(close.iloc[-1])
    s = float(sma20.iloc[-1])
    sd = float(std20.iloc[-1])
    rv = float(rsi14.iloc[-1])
    if sd == 0 or np.isnan(sd) or np.isnan(s) or np.isnan(rv):
        return None

    vwap = session_vwap(df, today_date)
    if not vwap:
        return None

    return {
        "ticker": ticker,
        "close": c,
        "high": float(df["High"].iloc[-1]),
        "low": float(df["Low"].iloc[-1]),
        "sma20": s,
        "rsi": rv,
        "upper": s + Z_THRESH * sd,
        "lower": s - Z_THRESH * sd,
        "z": (c - s) / sd,
        "vwap": vwap,
        "vwap_dev": (c - vwap) / vwap,
    }


def load_state(today_date):
    p = STATE_DIR / f"{today_date.isoformat()}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_state(today_date, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{today_date.isoformat()}.json").write_text(json.dumps(state, indent=2))


def calculate_position_size(stock_price, total_capital=1000.0, num_slots=4, max_risk_pct=1.0, stop_loss_pct=0.7):
    # Convert percentages to decimals
    risk_dec = max_risk_pct / 100
    stop_dec = stop_loss_pct / 100

    # 1. Capital Constraint (Max whole shares per slot)
    capital_per_slot = total_capital / num_slots
    shares_by_capital = int(capital_per_slot // stock_price)

    # 2. Risk Constraint (Max whole shares before hitting $8 risk limit)
    max_risk_dollars = total_capital * risk_dec
    risk_per_share = stock_price * stop_dec

    if risk_per_share > 0:
        shares_by_risk = int(max_risk_dollars // risk_per_share)
    else:
        shares_by_risk = shares_by_capital

    # 3. Final Position Size (The Bottleneck)
    final_shares = min(shares_by_capital, shares_by_risk)

    return final_shares


def regime_is_rotational(ticker):
    """A/B tag: True if the ticker is in a ROTATIONAL (mean-revert-friendly) regime,
    False if TRENDING. Mirrors the backtest's gen_mean_rev_regime gate: 1h EMA20/50,
    using the PRIOR completed hour (no intra-hour peeking). Fail-OPEN (return True =
    keep the trade in the filtered cohort) if data is short/unavailable, so a fetch
    hiccup never silently drops a setup. Computed only at trigger time (rare)."""
    try:
        df = get_5m_data(ticker, days=REGIME_DAYS)
        if df is None or len(df) < 60:
            return True
        h1 = df["Close"].resample("1h").last().dropna()
        if len(h1) < REGIME_EMA_SLOW + 2:
            return True
        ef = h1.ewm(span=REGIME_EMA_FAST, adjust=False).mean()
        es = h1.ewm(span=REGIME_EMA_SLOW, adjust=False).mean()
        es_prev = float(es.iloc[-2]); ef_prev = float(ef.iloc[-2])   # prior completed hour
        if es_prev == 0:
            return True
        stacked = abs(ef_prev - es_prev) / abs(es_prev)
        return stacked <= REGIME_THRESH
    except Exception:
        return True


def size_trade(entry, stop):
    """Whole-share size for a live trade. Feeds calculate_position_size the ACTUAL
    stop distance (not the 0.7% default) so the $8 risk cap is enforced on the real
    stop, then routes a strict integer share count. Returns (shares, dollars, risk$)."""
    stop_pct = abs(entry - stop) / entry * 100 if entry > 0 else 0.0
    shares = calculate_position_size(entry, total_capital=TOTAL_CAPITAL, num_slots=NUM_SLOTS,
                                     max_risk_pct=MAX_RISK_PCT, stop_loss_pct=stop_pct)
    shares = int(shares)  # strict whole-share integer routed to the broker
    return shares, round(shares * entry, 2), round(shares * abs(entry - stop), 2)


def ticket(tk, side, entry, stop, t1, t2, rr, m, shares, notional, risk_dollars):
    rps = abs(entry - stop)
    size = (f"{shares} sh (${notional:.0f}, risk ${risk_dollars:.2f})"
            if shares > 0 else "0 sh — SKIP: 1 whole share exceeds $8 risk cap")
    return (f"{tk} {side} | {size} | entry {entry:.2f} | stop {stop:.2f} "
            f"(risk {rps:.2f}/sh) | T1 {t1:.2f} | T2 {t2:.2f} | R:R {rr:.1f} "
            f"| RSI {m['rsi']:.0f}")


def advance_watch(side, st, m, manage_lifecycle=True):
    """Advance one watch-stage entry against the latest bar `m`. Mutates `st`.

    Returns ("trigger", (entry,stop,t1,t2,rr)) | ("none", None) | ("watch", None).

    manage_lifecycle=True  -> full */15 scan behavior: re-arm the signal level while
      still extended, bump the watch-check counter, and expire after MAX_WATCH_CHECKS.
    manage_lifecycle=False -> fast watch-only loop: ONLY refresh the protective-stop
      extreme and detect the break -> trigger. Does NOT touch checks/expiry/signal
      level, so polling every minute can't prematurely age out a watch (the */15
      scan stays the single owner of a setup's lifecycle).
    """
    if side == "SHORT":
        st["extreme_high"] = max(st["extreme_high"], m["high"])
        if m["close"] > m["upper"]:                       # still extended
            if manage_lifecycle:
                st["signal_low"] = m["low"]
                st["checks"] += 1
        elif m["low"] < st["signal_low"]:                 # break of signal-bar low
            entry = st["signal_low"]
            stop = st["extreme_high"] * (1 + STOP_BUFFER)
            t1, t2 = m["vwap"], m["sma20"]
            risk = stop - entry
            rr = (entry - t1) / risk if risk > 0 else 0
            if rr >= MIN_RR:
                st["stage"] = "triggered"
                return ("trigger", (entry, stop, t1, t2, rr))
            if manage_lifecycle:
                st["stage"] = "expired"
            return ("none", None)
        else:
            if manage_lifecycle:
                st["checks"] += 1
    else:  # LONG
        st["extreme_low"] = min(st["extreme_low"], m["low"])
        if m["close"] < m["lower"]:                        # still extended
            if manage_lifecycle:
                st["signal_high"] = m["high"]
                st["checks"] += 1
        elif m["high"] > st["signal_high"]:                # break of signal-bar high
            entry = st["signal_high"]
            stop = st["extreme_low"] * (1 - STOP_BUFFER)
            t1, t2 = m["vwap"], m["sma20"]
            risk = entry - stop
            rr = (t1 - entry) / risk if risk > 0 else 0
            if rr >= MIN_RR:
                st["stage"] = "triggered"
                return ("trigger", (entry, stop, t1, t2, rr))
            if manage_lifecycle:
                st["stage"] = "expired"
            return ("none", None)
        else:
            if manage_lifecycle:
                st["checks"] += 1
    if manage_lifecycle and st.get("stage") == "watch" and st["checks"] > MAX_WATCH_CHECKS:
        st["stage"] = "expired"
    return ("watch", None)


def run_scan(now_et, watch_only, max_tickers=100):
    """One pass. watch_only=False = full universe scan (finds setups, owns lifecycle).
    watch_only=True = fast loop over only currently-watched names (trigger detection).
    Returns (today_date, watch_alerts, trigger_alerts). State + trigger jsonl persisted
    here (under the caller's lock)."""
    today_date = now_et.date()
    entry_time = now_et.replace(tzinfo=None).isoformat(timespec="seconds")
    state = load_state(today_date)
    watch_alerts = []
    trigger_alerts = []
    trigger_records = []

    # sector-rotation TAG (2026-07-01) -- informational only, does NOT filter/block any
    # trigger (same "tag, don't filter" pattern as regime_ok above -- the ORB leg carried
    # this gate on the holdout, the MR leg didn't, so MR stays untouched pending its own
    # live evidence). Loaded once per scan rather than per-ticker.
    import sector_rotation as secrot
    _sector_map = secrot.load_sector_map()
    _sector_quadrants = secrot.load_latest_quadrants()

    def record(tk, side, entry, stop, t1, t2, rr, shares, notional, risk_dollars, regime_ok,
               z=None, rsi=None, vwap_dev=None):
        sec_tag = secrot.ticker_sector_tag(tk, _sector_map, _sector_quadrants)
        trigger_records.append({
            "trade_id": f"{tk}:{side}:{today_date.isoformat()}",
            "ticker": tk, "side": side,
            "entry": round(entry, 4), "stop": round(stop, 4),
            "t1": round(t1, 4), "t2": round(t2, 4),
            "planned_rr": round(rr, 2), "entry_time": entry_time,
            "strategy": STRATEGY_VERSION,
            "shares": int(shares), "notional": notional, "risk_dollars": risk_dollars,
            "regime_ok": bool(regime_ok),   # A/B: passes the 1h-EMA rotational filter?
            "z": z, "rsi": rsi, "vwap_dev": vwap_dev,   # technical readout for the dashboard
            "sector_etf": sec_tag["sector_etf"] if sec_tag else None,
            "sector_quadrant": sec_tag["sector_quadrant"] if sec_tag else None,
            "sector_hot": sec_tag["sector_hot"] if sec_tag else None,
            # "wide500k" (not the old unfiltered "wide") since 2026-06-28 -- the universe
            # itself narrowed from ~5,600 unfiltered to ~196 names (30-day ADV>500K), a
            # different/smaller untested cohort that shouldn't blend with old "wide" history.
            "universe": "core99" if tk in core99_set() else "wide500k",
        })

    if watch_only:
        # only the handful of names sitting in a watch stage -> tiny load regardless of
        # universe size (watch entries are created by the full scan only)
        tickers = sorted({k.split(":")[0] for k, v in state.items()
                          if isinstance(v, dict) and v.get("stage") == "watch"})
        if not tickers:
            return today_date, watch_alerts, trigger_alerts
        blacklist = set()
    else:
        blacklist = earnings_blacklist(today_date)
        # WIDE universe (2026-06-26): all NYSE/NASDAQ common stock $5-$266, not just the
        # backtested 99 -- see wide_universe.py. Fails open to the curated 99 if it errors.
        try:
            import wide_universe as wu
            tickers = wu.load_universe_for_scanning(rebuild_if_stale=True) or list(core99_set())
        except Exception:
            tickers = list(core99_set())
        tickers = tickers[:max_tickers]
        prefetch_batch_bars(tickers, days=2)   # mean-rev needs ~20 bars; 2 days covers early-session warmup

    for tk in tickers:
        if tk in blacklist:
            continue
        try:
            m = compute(tk, today_date)
        except Exception as e:
            log.error(f"Error {tk}: {e}")
            continue
        if m is None:
            continue

        if not watch_only:
            setup_long = (m["z"] <= -Z_THRESH and m["close"] < m["lower"]
                          and m["rsi"] < RSI_OVERSOLD and m["vwap_dev"] <= -VWAP_DEV_PCT)
            setup_short = (m["z"] >= Z_THRESH and m["close"] > m["upper"]
                           and m["rsi"] > RSI_OVERBOUGHT and m["vwap_dev"] >= VWAP_DEV_PCT)

        for side in ("SHORT", "LONG"):
            key = f"{tk}:{side}"
            st = state.get(key)
            if st is None:
                if watch_only:
                    continue
                # full scan only: promote a fresh setup into WATCH
                if side == "SHORT" and setup_short and m["close"] <= MR_MAX_PRICE:
                    state[key] = {"stage": "watch", "signal_low": m["low"],
                                  "extreme_high": m["high"], "checks": 0}
                    watch_alerts.append(f"{tk} SHORT-watch | px={m['close']:.2f} z={m['z']:.2f} "
                                        f"RSI={m['rsi']:.0f} {m['vwap_dev']*100:+.1f}% vs VWAP")
                elif side == "LONG" and setup_long and m["close"] <= MR_MAX_PRICE:
                    state[key] = {"stage": "watch", "signal_high": m["high"],
                                  "extreme_low": m["low"], "checks": 0}
                    watch_alerts.append(f"{tk} LONG-watch | px={m['close']:.2f} z={m['z']:.2f} "
                                        f"RSI={m['rsi']:.0f} {m['vwap_dev']*100:+.1f}% vs VWAP")
            elif st["stage"] == "watch":
                status, vals = advance_watch(side, st, m, manage_lifecycle=not watch_only)
                if status == "trigger":
                    entry, stop, t1, t2, rr = vals
                    shares, notional, risk_dollars = size_trade(entry, stop)  # whole-share int
                    regime_ok = regime_is_rotational(tk)   # A/B tag (computed only on a fire)
                    line = ticket(tk, side, entry, stop, t1, t2, rr, m, shares, notional, risk_dollars)
                    line += "  [regime ✓]" if regime_ok else "  [⚠ trending — plain cohort only]"
                    trigger_alerts.append(line)
                    record(tk, side, entry, stop, t1, t2, rr, shares, notional, risk_dollars, regime_ok,
                           z=m["z"], rsi=m["rsi"], vwap_dev=m["vwap_dev"])

    save_state(today_date, state)
    if trigger_records:
        tp = DATA_DIR / f"mr_triggers_{today_date.isoformat()}.jsonl"
        with tp.open("a") as f:
            for rec in trigger_records:
                f.write(json.dumps(rec) + "\n")
    return today_date, watch_alerts, trigger_alerts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--watch-only", action="store_true",
                        help="fast loop: poll only names already in WATCH, fire TRIGGER asap")
    parser.add_argument("--max-tickers", type=int, default=6000)
    args = parser.parse_args()

    now_et = datetime.now(ZoneInfo("America/New_York"))
    if not args.force and not market_is_open(now_et):
        log.info(f"Market closed at {now_et.isoformat()}; skipping.")
        return

    # Serialize state R/W between the */15 full scan and the */1 watch loop.
    # Full scan = blocking lock (it's the authority); watch loop = non-blocking
    # (if the full scan is mid-pass, skip this minute — it'll catch the trigger).
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fp = open(LOCK_PATH, "w")
    acquired = False
    try:
        if args.watch_only:
            try:
                fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                log.debug("full scan holds the lock; watch-only skipping this minute.")
                return
        else:
            fcntl.flock(lock_fp, fcntl.LOCK_EX)
            acquired = True
        today_date, watch_alerts, trigger_alerts = run_scan(
            now_et, watch_only=args.watch_only, max_tickers=args.max_tickers)
    finally:
        if acquired:
            try:
                fcntl.flock(lock_fp, fcntl.LOCK_UN)
            except OSError:
                pass
        lock_fp.close()

    msg_path = MR_WATCH_MSG_PATH if args.watch_only else MSG_PATH
    parts = []
    if trigger_alerts:
        parts.append(f"{EMOJI_TARGET} TRIGGER -- entry confirmed (confirm before placing):")
        parts.extend(trigger_alerts)
    if watch_alerts:  # only the full scan emits these
        parts.append("")
        parts.append(f"{EMOJI_EYES} WATCH -- setup forming, do NOT enter yet:")
        parts.extend(watch_alerts)

    if not parts:
        tag = "watch-only" if args.watch_only else "scan"
        log.info(f"No new mean-reversion activity ({tag}) at {now_et.strftime('%H:%M %Z')}")
        if msg_path.exists():
            msg_path.unlink()
        return

    label = "Mean Reversion TRIGGER" if args.watch_only else "Mean Reversion"
    header = f"{EMOJI_CHART} {label} -- {now_et.strftime('%b %d %H:%M %Z')}"
    msg = header + "\n" + "\n".join(parts)
    msg_path.write_text(msg)
    log.info(msg)


if __name__ == "__main__":
    main()
