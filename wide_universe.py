#!/usr/bin/env python3
"""wide_universe.py - widens live scanning from the curated 99-name list to NYSE/NASDAQ
common stock priced $5-$266 (heff's direction 2026-06-26: not just the backtested 99, "all under
that threshold that's not penny stocks"). Three pieces:

1) build_universe() - Alpaca tradable assets -> NYSE/NASDAQ common stock filter -> batched
   snapshot price filter ($5-$266) -> 30-day ADV liquidity filter (added 2026-06-28, see below).
   Cached to disk (refreshed ~daily via cron, not every scan tick -- the asset list and prices
   don't need to be re-pulled every 2 minutes).

2) fetch_daily_volume_batch() - the ADV filter's data source (added 2026-06-28): same batched
   multi-symbol pattern as fetch_bars_batch below, on 1Day bars, used to size/filter the universe
   by liquidity before it ever reaches the live scanners.

3) fetch_bars_batch() - the reason the WIDE part is even possible: the live scanners' existing
   fetch_5m_alpaca() hits Alpaca ONE SYMBOL AT A TIME (confirmed ~0.12s/symbol) -- at thousands of
   symbols that's many minutes per scan, incompatible with a 2-5min cadence. This uses Alpaca's
   multi-symbol /v2/stocks/bars endpoint (100 symbols/request, paginated via next_page_token)
   to fetch the whole universe in a handful of requests. Returns the SAME per-ticker dataframe
   shape (ET tz-aware index, Open/High/Low/Close/Volume, RTH-filtered) the rest of the scanner
   code already expects, so it's a drop-in replacement for the per-symbol loop, not a rewrite of
   the signal logic.

NOTE on the validation gap (said plainly, not hidden): z=1.5 mean-rev and tight-range ORB were
backtested and holdout-validated ONLY on the curated 99 large-caps. The wide universe -- whether
the original ~5,600-name unfiltered version (2026-06-26) or this narrower ADV>500K, ~196-name
version (2026-06-28, heff's explicit call after sizing {2M:18, 1M:77, 750K:120, 500K:196,
250K:422} tickers and picking 500K to land closest to a 250-350 target) -- is UNTESTED: small/
mid-caps trade differently (wider spreads, more gaps, thinner liquidity) and the same thresholds
might not carry the same edge. Deployed live anyway per heff's explicit call (paper money, "try
it and learn" same as the options tournament) -- trades from outside the original 99 are tagged
"wide500k" (replacing the old unfiltered "wide" tag going forward) so this cohort's performance
can be watched on its own rather than silently blended into the proven track record OR the old,
now-superseded unfiltered-wide cohort's history.
"""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from io import StringIO

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
CACHE_PATH = ROOT / "data" / "wide_universe.json"
ALPACA_KEY_PATH = ROOT / "credentials" / "alpaca_key.txt"
ALPACA_SECRET_PATH = ROOT / "credentials" / "alpaca_secret.txt"
PRICE_MIN = 5.0
PRICE_MAX = 266.0
ADV_MIN = 500_000  # 30-day Average Daily Volume floor, added 2026-06-28 (heff's call, see build_universe)
CACHE_MAX_AGE_HOURS = 20
HIGH_PRICE_LIQUID_CACHE_PATH = ROOT / "data" / "high_price_liquid_universe.json"
HIGH_PRICE_LIQUID_MAX_AGE_HOURS = 20
DOLLAR_ADV_MIN = 50_000_000  # $/day floor for the price-uncapped GEX-only supplement below


def _atomic_write_json(path, payload):
    """write_text() is a plain open+write+close -- if two scanners both find the cache stale
    on the same tick (real risk: mean-rev/orb/premarket/continuation all call load_universe()
    on independent cron cadences that regularly overlap), two concurrent writers can interleave
    bytes and corrupt the file for every reader. Write to a same-directory temp file + os.replace
    (atomic rename on POSIX) instead -- any concurrent reader sees the old complete file or the
    new complete file, never a partial one. No reader-side change needed."""
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)

# Hardcoded blocklist, added 2026-06-29 after the first continuous_search.py champion run on
# the 196-name wide500k universe came back with implausible numbers (Sharpe 15.4 / 0.50R-tr vs
# the curated-99 baseline's 2.49 / 0.096R-tr). Per-symbol breakdown traced ~90% of the inflation
# to two categories the price+ADV filter alone can't see:
#   - 2x leveraged/inverse SINGLE-STOCK ETFs (NVD/TSDD/TSLG) -- structurally different
#     instruments (daily-reset leverage decay), not "stocks" in the sense heff meant by
#     "all stocks under $266, not penny stocks"; their amplified swings mechanically inflate
#     mean-reversion R without representing a repeatable edge.
#   - extreme-volatility speculative micro/small-cap momentum names that happened to have huge
#     swings in the 2024-2026 AI/drone/quantum/crypto-mining hype cycle -- real stocks that pass
#     price/ADV but are unrepresentative of the liquid, calmer large-caps this strategy was
#     actually validated on; heff's call (2026-06-29) was to exclude them too so champion-search
#     results stay trustworthy/comparable to the curated-99 baseline.
# Revisit if heff wants a smarter ongoing filter (e.g. realized-vol/ATR% cap) instead of a
# hand-maintained list -- this is the fast fix, not necessarily the permanent one.
EXCLUDED_SYMBOLS = {
    "NVD", "TSDD", "TSLG",  # leveraged/inverse single-stock ETFs
    "RGTI", "RCAT", "POET", "NVTS", "ONDS", "EOSE", "WULF", "PL", "RXT", "KEEL", "INFQ", "TE",
    # round 2 (2026-06-29): confirmed via per-symbol R-contribution breakdown on the 181-name
    # universe -- still present AFTER the $2B market-cap filter, since these are genuinely
    # multi-billion-dollar companies precisely because they are hype-driven, not despite it.
    "QUBT", "JOBY", "PTON", "RUN", "QXO", "LUMN", "QBTS", "IONQ", "BB", "CLSK", "APLD", "CORZ",
    "ASTS", "IREN", "ECHO", "QS", "NTSK", "SOUN", "RIG", "RKLB", "HIMS", "MARA",
}

# Phase 1 institutional-grade purification (2026-06-29): the hand-maintained blocklist above
# kept finding MORE offenders (TSLL/PLTD leveraged ETFs, QUBT/IONQ/RGTI-style quantum/crypto-
# mining/space hype names) every time the per-symbol R breakdown was re-run -- top 25 of 181
# names still accounted for 45% of total backtest R. A market-cap floor is a structural filter
# instead of whack-a-mole: small/micro-caps are mechanically more likely to be the speculative,
# violently-reverting names driving the inflation. NOTE this does NOT by itself fix leveraged
# single-stock ETFs (an ETF's "market cap" reflects its AUM, not the underlying's size, and is
# an orthogonal axis to the leverage problem) -- EXCLUDED_SYMBOLS above still carries that load.
MARKET_CAP_MIN = 2_000_000_000  # $2B floor


def _creds():
    return ALPACA_KEY_PATH.read_text().strip(), ALPACA_SECRET_PATH.read_text().strip()


def _headers():
    k, s = _creds()
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def fetch_daily_volume_batch(tickers, days=35, batch_size=100):
    """Batched daily-bar fetch -> {ticker: mean Volume over the available bars (up to the
    most recent 30 trading days)}. Same multi-symbol-per-request + page_token pagination as
    fetch_bars_batch below, but WITHOUT its .between_time("09:30","16:00") filter -- Alpaca's
    1Day bars are timestamped 04:00:00Z (midnight ET), outside that window, so reusing
    fetch_bars_batch unmodified would silently empty out every daily bar (verified empirically
    2026-06-28 before writing this)."""
    H = _headers()
    start = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    out = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        rows_by_sym = {s: [] for s in batch}
        page_token = None
        while True:
            params = {"symbols": ",".join(batch), "timeframe": "1Day", "start": start,
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
            vols = [b["v"] for b in bars[-30:]]
            if vols:
                out[sym] = sum(vols) / len(vols)
    return out


def _priced_candidates(price_min=PRICE_MIN, price_max=PRICE_MAX):
    """Tradable NYSE/NASDAQ common stock, price-filtered via batched snapshots. Factored out
    of build_universe() (2026-06-29) so sweep_adv_for_target() can share this exact stage
    instead of re-implementing it -- both need the same starting candidate pool before
    diverging on which ADV/market-cap thresholds to apply.
    Excludes warrants/units/rights (symbol suffixes W/U/R -- SPAC structures list a unit XYZU,
    warrant XYZW, and rights XYZR alongside the common stock XYZ; the first deploy of this
    (2026-06-26) only excluded W and let thousands of low-quality SPAC units/rights through,
    which have no real Alpaca bar data and were cascading into slow per-symbol yfinance
    fallback lookups one at a time -- exactly the bottleneck this script exists to avoid),
    class-share dot symbols, tickers with a hyphen (Alpaca's BRK-B style -- excluded here for
    simplicity), and the hand-maintained EXCLUDED_SYMBOLS blocklist."""
    H = _headers()
    r = requests.get("https://paper-api.alpaca.markets/v2/assets", headers=H,
                      params={"status": "active", "asset_class": "us_equity"}, timeout=30)
    r.raise_for_status()
    assets = r.json()
    common = [a["symbol"] for a in assets if a.get("tradable") and a.get("status") == "active"
              and a.get("exchange") in ("NYSE", "NASDAQ") and a.get("class") == "us_equity"
              and not a["symbol"].endswith(("W", "U", "R")) and len(a["symbol"]) <= 5
              and "." not in a["symbol"] and "-" not in a["symbol"]
              and a["symbol"] not in EXCLUDED_SYMBOLS]

    kept = []
    for i in range(0, len(common), 100):
        batch = common[i:i + 100]
        try:
            resp = requests.get("https://data.alpaca.markets/v2/stocks/snapshots", headers=H,
                                 params={"symbols": ",".join(batch), "feed": "iex"}, timeout=20)
            data = resp.json()
        except Exception:
            continue
        for sym in batch:
            lt = (data.get(sym) or {}).get("latestTrade")
            if lt and price_min <= lt["p"] <= price_max:
                kept.append(sym)
    return kept


def fetch_market_caps(symbols, sleep_between=0.05):
    """Per-symbol market cap via yfinance fast_info (yfinance has no batch market-cap
    endpoint, unlike Alpaca's snapshot/bars calls elsewhere in this file -- this is
    deliberately the LAST, most expensive filter stage, applied only to whatever small pool
    survives the price+ADV cuts first). Every lookup is independently try/excepted: one
    delisted/throttled/no-data symbol must never abort the whole pull.
    Fails OPEN (returns {}) if yfinance can't be imported, or if the overall success rate
    comes back under 20% (Yahoo blocking this datacenter IP is a real, precedented risk here
    -- see openclaw-server memory re: yt-dlp/YouTube from this same box) -- callers MUST treat
    an empty dict as "skip this filter", never as "every symbol failed the bar", or a transient
    network/throttling problem would silently produce an empty universe."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            mc = None
            try:
                mc = t.fast_info.get("marketCap") or t.fast_info.get("market_cap")
            except Exception:
                mc = None
            if not mc:
                mc = (t.info or {}).get("marketCap")
            if mc:
                out[sym] = float(mc)
        except Exception:
            pass
        if sleep_between:
            time.sleep(sleep_between)
    if symbols and (len(out) / len(symbols)) < 0.2:
        return {}  # circuit breaker -- treat as a failed pull, not "everyone's sub-$2B"
    return out


def build_universe(price_min=PRICE_MIN, price_max=PRICE_MAX, adv_min=ADV_MIN,
                   market_cap_min=MARKET_CAP_MIN):
    """Fresh pull: tradable NYSE/NASDAQ common stock (see _priced_candidates), price-filtered,
    THEN liquidity-filtered by 30-day Average Daily Volume (added 2026-06-28 -- heff's call
    after sizing the unfiltered $5-$266 universe at ~5,000 names and finding it both far wider
    than a 250-350 target AND, more importantly, never holdout-validated; ADV>500K landed
    closest to target at 196 names of the thresholds swept {2M:18, 1M:77, 750K:120, 500K:196,
    250K:422}), THEN market-cap-filtered (added 2026-06-29, Phase 1 institutional-grade
    purification -- see MARKET_CAP_MIN). adv_min=None / market_cap_min=None skip that
    respective filter for any caller that explicitly wants the older/looser behavior. The
    market-cap pull fails OPEN (see fetch_market_caps) -- if yfinance is unreachable this
    stage is silently skipped rather than risk zeroing out the universe."""
    kept = _priced_candidates(price_min, price_max)

    adv = {}
    if adv_min is not None:
        adv = fetch_daily_volume_batch(kept)
        kept = [s for s in kept if adv.get(s, 0) > adv_min]

    if market_cap_min is not None:
        mktcap = fetch_market_caps(kept)
        if mktcap:
            kept = [s for s in kept if mktcap.get(s, 0) > market_cap_min]

    payload = {"built_at": datetime.now(ET).isoformat(), "price_min": price_min,
               "price_max": price_max, "adv_min": adv_min, "market_cap_min": market_cap_min,
               "symbols": sorted(kept)}
    _atomic_write_json(CACHE_PATH, payload)
    return kept


def sweep_adv_for_target(target_min=150, target_max=180,
                         candidate_thresholds=(500_000, 450_000, 400_000, 350_000, 300_000, 250_000),
                         price_min=PRICE_MIN, price_max=PRICE_MAX, market_cap_min=MARKET_CAP_MIN):
    """One-time calibration (Phase 1, 2026-06-29): fetch ADV and market cap ONCE for the
    broadest candidate pool under consideration (price-filtered, ADV above the LOWEST
    threshold in candidate_thresholds), then sweep every threshold purely in-memory to find
    the one that lands the final universe in [target_min, target_max] names. Deliberately
    single-pass: market cap does not depend on the ADV threshold, so re-fetching it per
    threshold would multiply slow per-symbol yfinance calls for zero benefit (and raise
    throttling risk on this datacenter IP for nothing).
    Writes the winning universe to CACHE_PATH (same payload shape build_universe() writes, so
    load_universe() needs no changes) and returns (chosen_threshold, final_symbols, sizing)
    where sizing = {threshold: count} for every candidate tried, so the choice is auditable.
    If no threshold lands exactly in range, picks whichever is numerically closest to the
    window's midpoint rather than silently returning nothing."""
    floor = min(candidate_thresholds)
    priced = _priced_candidates(price_min, price_max)
    adv = fetch_daily_volume_batch(priced)
    adv_pool = [s for s in priced if adv.get(s, 0) > floor]

    mktcap = fetch_market_caps(adv_pool) if market_cap_min is not None else {}
    mktcap_active = bool(mktcap)  # fetch_market_caps already fails-open to {} on a bad pull

    def _pool_at(thresh):
        pool = [s for s in adv_pool if adv.get(s, 0) > thresh]
        if market_cap_min is not None and mktcap_active:
            pool = [s for s in pool if mktcap.get(s, 0) > market_cap_min]
        return pool

    sizing = {}
    chosen = None
    for thresh in sorted(candidate_thresholds, reverse=True):
        n = len(_pool_at(thresh))
        sizing[thresh] = n
        if chosen is None and target_min <= n <= target_max:
            chosen = thresh
    if chosen is None:
        mid = (target_min + target_max) / 2.0
        chosen = min(sizing, key=lambda t: abs(sizing[t] - mid))

    final_pool = sorted(_pool_at(chosen))
    payload = {"built_at": datetime.now(ET).isoformat(), "price_min": price_min,
               "price_max": price_max, "adv_min": chosen,
               "market_cap_min": market_cap_min if mktcap_active else None,
               "symbols": final_pool}
    _atomic_write_json(CACHE_PATH, payload)
    return chosen, final_pool, sizing


def load_universe(max_age_hours=CACHE_MAX_AGE_HOURS, rebuild_if_stale=True):
    """Cached wide universe; rebuilds if missing/stale. Fail-safe: if a rebuild errors and no
    cache exists at all, returns [] (caller should fall back to the curated 99, never silently
    scan nothing).

    Backtest/research callers (walkforward_search.py, continuous_search.py, the
    sector_*_check.py / backtest_*.py scripts, etc.) should keep calling THIS function
    unchanged -- they need a stable, reproducible universe definition. Live scanners
    should call load_universe_for_scanning() instead (see below) -- it adds today's
    supplementary movers, which is deliberately NOT reproducible/backtest-safe."""
    if CACHE_PATH.exists():
        try:
            payload = json.loads(CACHE_PATH.read_text())
            built = datetime.fromisoformat(payload["built_at"])
            age_h = (datetime.now(ET) - built).total_seconds() / 3600.0
            if age_h <= max_age_hours or not rebuild_if_stale:
                return payload["symbols"]
        except Exception:
            pass
    try:
        return build_sp500_universe()  # top-down index filter, see build_sp500_universe()
    except Exception:
        if CACHE_PATH.exists():
            try:
                return json.loads(CACHE_PATH.read_text())["symbols"]
            except Exception:
                pass
        return []


SUPP_MOVERS_CACHE_PATH = ROOT / "data" / "supplementary_movers.json"
SUPP_MOVERS_MAX_AGE_HOURS = 4  # refreshed a few times/day, not re-swept on every scanner poll


def find_supplementary_movers(current_universe, price_min=PRICE_MIN, gap_min_pct=5.0):
    """S&P 500 names NOT already in current_universe that gapped/moved >= gap_min_pct
    vs. their previous close, checked directly via Alpaca snapshots (prevDailyBar close
    vs. latestTrade) -- independent of the top-180-by-IEX-volume ranking that
    build_sp500_universe() uses.

    Why this exists (2026-07-04): that volume-ranked universe is inherently reactive --
    a name has to ALREADY be trading at unusually high volume to make the top 180, so a
    brand-new mover in a name that isn't normally top-180-by-volume can be invisible to
    every scanner on day one of its move. Confirmed against the real semiconductor/
    AI-capex selloff (SK Hynix HBM guidance cut + the Michael Burry CAT short, both
    2026-07-01): KLAC/SNDK/WDC/MU/LRCX/AMAT/GLW/VRT all gapped -5% to -8% that morning
    but were entirely absent from the pre-crash universe snapshot (data/
    wide_universe_164blocklist_archive.json, 2026-06-29) -- they only entered by the
    next rebuild, as a consequence of the crash itself inflating their volume. The real
    premarket-scanner outcomes ledger for that day shows only AAPL/NVDA as picks --
    every actual mover was invisible to the scan.

    This checks the FULL S&P 500 (not just the current 180) directly for today's gap,
    so a name qualifies by what it's actually doing right now, not by its trailing
    volume rank. gap_min_pct defaults higher (5%) than premarket_scanner's own 2%
    threshold deliberately -- this is a coarse "does this deserve a look at all today"
    filter for names outside the normal universe; the scanners' own finer thresholds
    still apply on top once a name is pulled in.
    """
    current = set(current_universe)
    candidates = [t for t in fetch_sp500_tickers() if t not in EXCLUDED_SYMBOLS and t not in current]
    H = _headers()
    movers = []
    for i in range(0, len(candidates), 100):
        batch = candidates[i:i + 100]
        try:
            resp = requests.get("https://data.alpaca.markets/v2/stocks/snapshots", headers=H,
                                 params={"symbols": ",".join(batch), "feed": "iex"}, timeout=20)
            data = resp.json()
        except Exception:
            continue
        for sym in batch:
            snap = data.get(sym) or {}
            lt = snap.get("latestTrade")
            prev = snap.get("prevDailyBar")
            if not lt or not prev or not prev.get("c") or lt["p"] < price_min:
                continue
            gap_pct = (lt["p"] - prev["c"]) / prev["c"] * 100.0
            if abs(gap_pct) >= gap_min_pct:
                movers.append(sym)
    return sorted(movers)


def fetch_high_price_liquid_names(exclude, price_floor=PRICE_MAX, dollar_adv_min=DOLLAR_ADV_MIN):
    """S&P 500 names priced ABOVE wide_universe's own PRICE_MAX ($266) but still genuinely
    liquid by DOLLAR volume (price x 30-day average SHARE volume) -- heff's ask (2026-07-06):
    the GEX dashboard should surface real megacaps regardless of price (COST, BKNG, NVR, AZO,
    MELI, REGN, etc. all structurally fail PRICE_MAX despite dwarfing most of the $5-$266
    cohort in actual dollar liquidity). Share-count ADV (the trading scanners' own
    ADV_MIN=500K shares) isn't a fair yardstick for four-figure stocks -- a $3000 name trading
    only 50K shares/day still moves $150M/day. GEX-DASHBOARD-ONLY: deliberately NOT folded into
    load_universe()/load_universe_for_scanning() (the trading scanners' universe), since these
    names were never backtested for the mean-reversion/ORB strategies the priced $5-$266
    cohort was -- see live_gex.py's main() for where this actually gets used."""
    candidates = [t for t in fetch_sp500_tickers() if t not in exclude and t not in EXCLUDED_SYMBOLS]
    H = _headers()
    high_priced = {}
    for i in range(0, len(candidates), 100):
        batch = candidates[i:i + 100]
        try:
            resp = requests.get("https://data.alpaca.markets/v2/stocks/snapshots", headers=H,
                                 params={"symbols": ",".join(batch), "feed": "iex"}, timeout=20)
            data = resp.json()
        except Exception:
            continue
        for sym in batch:
            lt = (data.get(sym) or {}).get("latestTrade")
            if lt and lt["p"] > price_floor:
                high_priced[sym] = lt["p"]
    if not high_priced:
        return []
    adv = fetch_daily_volume_batch(list(high_priced))
    return sorted(sym for sym, px in high_priced.items()
                  if px * adv.get(sym, 0) >= dollar_adv_min)


def load_high_price_liquid_universe(exclude, max_age_hours=HIGH_PRICE_LIQUID_MAX_AGE_HOURS):
    """Cached wrapper around fetch_high_price_liquid_names() -- same daily-ish refresh cadence
    as load_universe() itself, since this sweeps the whole S&P 500 (~500 snapshot + volume
    calls), not something to redo on every GEX sweep tick. Fails open to the last cached list
    (or []) on any fetch error -- a broken pull here must never itself shrink the GEX
    dashboard's ticker list below what it already had."""
    cached = None
    if HIGH_PRICE_LIQUID_CACHE_PATH.exists():
        try:
            payload = json.loads(HIGH_PRICE_LIQUID_CACHE_PATH.read_text())
            built = datetime.fromisoformat(payload["built_at"])
            age_h = (datetime.now(ET) - built).total_seconds() / 3600.0
            cached = payload["symbols"]
            if age_h <= max_age_hours:
                return cached
        except Exception:
            pass
    try:
        fresh = fetch_high_price_liquid_names(exclude)
        _atomic_write_json(HIGH_PRICE_LIQUID_CACHE_PATH,
                            {"built_at": datetime.now(ET).isoformat(), "symbols": fresh})
        return fresh
    except Exception:
        return cached or []


def load_universe_for_scanning(max_age_hours=CACHE_MAX_AGE_HOURS, rebuild_if_stale=True,
                                gap_min_pct=5.0, movers_max_age_hours=SUPP_MOVERS_MAX_AGE_HOURS):
    """load_universe() unioned with today's supplementary movers (see
    find_supplementary_movers). This is what LIVE scanners (premarket_scanner.py,
    continuation_scanner.py, mean_reversion_scanner.py, orb_scanner.py) should call --
    NOT backtest/research scripts, which need load_universe()'s plain, reproducible
    list. Movers are cached separately (movers_max_age_hours, default 4h) so repeated
    scanner polls within the same window don't each re-sweep the ~320 untracked S&P
    names. Fails open to the base universe alone if the movers sweep errors -- this is
    a supplement, never a blocker."""
    base = load_universe(max_age_hours=max_age_hours, rebuild_if_stale=rebuild_if_stale)
    movers = []
    stale = True
    if SUPP_MOVERS_CACHE_PATH.exists():
        try:
            payload = json.loads(SUPP_MOVERS_CACHE_PATH.read_text())
            built = datetime.fromisoformat(payload["built_at"])
            age_h = (datetime.now(ET) - built).total_seconds() / 3600.0
            if age_h <= movers_max_age_hours:
                movers = payload["symbols"]
                stale = False
        except Exception:
            pass
    if stale:
        try:
            movers = find_supplementary_movers(base, gap_min_pct=gap_min_pct)
            _atomic_write_json(SUPP_MOVERS_CACHE_PATH,
                                {"built_at": datetime.now(ET).isoformat(),
                                 "gap_min_pct": gap_min_pct, "symbols": movers})
        except Exception:
            movers = []
    return sorted(set(base) | set(movers))


def fetch_bars_batch(tickers, days=5, batch_size=100, timeframe="5Min"):
    """Multi-symbol live 5-min RTH bars from Alpaca (free IEX feed), paginated.
    Returns {ticker: df} in the same shape fetch_5m_alpaca() returns per-symbol (ET tz-aware
    index, Open/High/Low/Close/Volume, RTH 09:30-16:00). Tickers with no data are simply
    absent from the result dict (caller should treat a missing key like the old None return)."""
    H = _headers()
    start = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    out = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        rows_by_sym = {s: [] for s in batch}
        page_token = None
        while True:
            params = {"symbols": ",".join(batch), "timeframe": timeframe, "start": start,
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
            df = df[["Open", "High", "Low", "Close", "Volume"]].between_time("09:30", "16:00")
            df = df.dropna()
            if not df.empty:
                out[sym] = df
    return out


SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
# Wikipedia 403s the default urllib User-Agent pandas.read_html uses internally -- fetch the
# page ourselves with a browser UA first, then hand read_html the HTML text (confirmed working
# 2026-06-29; this is the same "fetch with proper headers" pattern as every Alpaca call in this
# file, just a different 403 cause).
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_sp500_tickers():
    """S&P 500 constituent list via Wikipedia (added 2026-06-29 -- Phase 1 pivot to TOP-DOWN
    index-membership filtering). Three rounds of bottom-up filtering (price+ADV, then a $2B
    market-cap floor, then a realized-vol attempt) each kept finding a NEW category of
    contamination -- leveraged single-stock ETFs, then AI/quantum/crypto-mining hype
    microcaps, then backfilled metals-miner/crypto-miner/recent-IPO names -- because every one
    of those is an indirect proxy for "calm, stable company" rather than the thing itself.
    S&P 500 membership is the real thing: the index committee already screens for sustained
    market cap, profitability, and liquidity, so this excludes all three contamination
    categories structurally, in one pass, instead of continuing to chase new offenders one
    blocklist round at a time. Returns ~503 tickers (some constituents list dual share
    classes, e.g. GOOG/GOOGL, separately) in Alpaca's own symbol convention -- dotted
    class-share tickers like BRK.B/BF.B pass through unchanged, no W/U/R-suffix or hyphen
    filtering needed since every name here is already a real, vetted common stock."""
    r = requests.get(SP500_WIKI_URL, headers={"User-Agent": _BROWSER_UA}, timeout=20)
    r.raise_for_status()
    df = pd.read_html(StringIO(r.text))[0]
    return sorted(df["Symbol"].astype(str).str.strip().tolist())


def build_sp500_universe(price_min=PRICE_MIN, target_max=180):
    """Top-down universe (2026-06-29): S&P 500 membership (fetch_sp500_tickers) -> price
    floor only, no ceiling (batched Alpaca snapshots; heff's spec only asked for the $5
    floor -- the old $266 ceiling was an affordability/whole-share constraint that already
    lives downstream in the scanners' own MAX_PRICE/MR_MAX_PRICE, not a universe-membership
    rule) -> if more than target_max names survive, keep the target_max MOST-TRADED by IEX
    volume as a RELATIVE ranking signal only.
    NO absolute ADV floor is applied here (confirmed 2026-06-29: Alpaca's free feed is IEX
    only, ~2-3% of consolidated US volume -- median IEX-only ADV across the WHOLE S&P 500
    came back ~166K, below even the old 250K floor, despite every name here being liquid by
    definition of index membership; an absolute IEX-derived threshold is meaningless for
    genuinely liquid mega-caps and would have silently gutted this universe again). IEX
    volume is only used to rank-and-trim when there are more candidates than target_max.
    EXCLUDED_SYMBOLS is still applied too, belt-and-suspenders (no current S&P 500 member is
    on that list, but a future corporate action/ticker reuse shouldn't silently bypass it).
    Replaces build_universe()/sweep_adv_for_target() as what load_universe() actually calls
    -- those bottom-up functions are kept in this file for reference, not deleted, but are no
    longer on the live path."""
    tickers = [t for t in fetch_sp500_tickers() if t not in EXCLUDED_SYMBOLS]
    H = _headers()
    priced = []
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        try:
            resp = requests.get("https://data.alpaca.markets/v2/stocks/snapshots", headers=H,
                                 params={"symbols": ",".join(batch), "feed": "iex"}, timeout=20)
            data = resp.json()
        except Exception:
            continue
        for sym in batch:
            lt = (data.get(sym) or {}).get("latestTrade")
            if lt and lt["p"] >= price_min:
                priced.append(sym)

    final = sorted(priced)
    if len(priced) > target_max:
        adv = fetch_daily_volume_batch(priced)  # relative ranking signal only, see docstring
        final = sorted(sorted(priced, key=lambda s: -adv.get(s, 0))[:target_max])

    payload = {"built_at": datetime.now(ET).isoformat(), "price_min": price_min,
               "price_max": None, "adv_min": None,
               "source": "sp500_index_top_by_iex_volume", "symbols": final}
    _atomic_write_json(CACHE_PATH, payload)
    return final


if __name__ == "__main__":
    import sys
    if "--build" in sys.argv:
        syms = build_universe()
        print(f"built wide universe: {len(syms)} symbols ($5-$266, ADV>{ADV_MIN:,}) -> {CACHE_PATH}")
    elif "--test-bars" in sys.argv:
        syms = load_universe()[:250]
        t0 = time.time()
        data = fetch_bars_batch(syms, days=5)
        print(f"fetched bars for {len(data)}/{len(syms)} symbols in {time.time()-t0:.1f}s")
        if data:
            sym0 = next(iter(data))
            print(sym0, data[sym0].tail(3))
    else:
        syms = load_universe()
        print(f"wide universe: {len(syms)} symbols loaded")
