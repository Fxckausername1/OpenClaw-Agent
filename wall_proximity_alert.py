"""wall_proximity_alert.py -- Telegram alert when a tracked ticker's live price is
near its call wall or put wall (the dealer-hedging levels already computed 3x/day
into data/live_gex_snapshot.json by live_gex.py / gex_quant_engine.py). Covers the
FULL tracked universe (194 tickers), not just SPY/0DTE -- reuses live_gex_snapshot.json's
already-computed walls as-is rather than recomputing GEX here.

Walls only refresh 3x/day (~10:00/12:15/14:30 ET via live_gex_wrapper.sh) since
they're OI-based and T-1-lagged anyway (same reasoning already established for the
rest of this GEX pipeline) -- price is checked LIVE, on this script's own cadence
(intended: every few minutes during RTH, its own cron), independent of the
wall-refresh cadence. A wall that's a few hours stale is still the right level to
watch; that's the existing, accepted tradeoff everywhere else in this pipeline, not
a new one introduced here.

Threshold + dedup are both simple, tunable first-cut choices, not claimed to be
precisely calibrated:
- PROXIMITY_THRESHOLD_PCT is a flat % of price, not ATR-normalized -- a high-vol name
  could trivially clear this on ordinary noise, while a low-vol name hitting it is
  more meaningful. A volatility-normalized version is the natural v2 if the flat %
  turns out too noisy (or not sensitive enough) in practice.
- COOLDOWN_MINUTES is a flat per-ticker-per-wall cooldown (not real hysteresis based
  on price actually moving away and back) -- simplest thing that stops "sitting right
  at the wall" from re-alerting every single poll. Easy to retune once there's real
  observed behavior to react to.

Each alert (added 2026-07-04, heff's explicit ask) also carries:
- A plain-language "next move" read (next_move_read()) built from WSS + regime +
  P(C) -- all already computed and live. Deliberately NOT gex_quant_engine.py's
  Phase 4 AdvancedGammaExecutionEngine, which stays LOCAL ONLY per heff's own
  standing instruction -- its real gates need tick-level order flow this 5-min
  snapshot alert doesn't have (would mostly just print HOLD_STATE without it).
- Today's confluence_with_conviction.json row for that ticker, if one exists (lean,
  signal breakdown, conviction). Most alerted tickers won't have a row -- confluence
  only computes for names with 3+ signals agreeing, at most 1 disagreeing -- that's
  expected, not a gap, and is shown as "no strong signal today" rather than omitted.

Targeted P(C)/WSS refresh (added 2026-07-05, heff's explicit ask): P(C)/WSS only
update 3x/day (the full 194-ticker sweep's own cadence) -- too slow for names
actually testing a wall right now. Rather than run the full sweep more often (~21min,
not viable at a 5min cadence on this single-core box), refresh_advanced_gex() does a
FRESH live_gex.compute_live() -- same real chain-fetch + Phase 2/3 compute the full
sweep uses -- for JUST the ~10-15 tickers currently within the proximity threshold,
every 5min, and patches those rows into live_gex_snapshot.json. This runs for EVERY
currently-proximate ticker, not just cooldown-filtered alert-worthy ones -- a name
that's been sitting at a wall for the past hour should keep getting a fresh P(C)
even though its Telegram alert already fired and is cooling down. Cheap enough for
this cadence (~10-15 chain fetches vs. 194), and gets freshness exactly where it's
actually needed instead of paying for it universe-wide.

Curated watchlist (added 2026-07-09, heff's explicit ask): wants broader market/sector
GEX visibility on a faster cadence than the 4x/day full sweep, WITHOUT running that full
sweep more often -- OI is T-1-lagged regardless of pull time (same reasoning
live_gex_wrapper.sh already documents), so a faster full sweep wouldn't show fresher WALL
LEVELS for most names anyway, while spot/WSS/P(C) DO update meaningfully more often --
and without the CPU-contention risk of a ~21min job competing with mean_reversion_scanner.py/
orb_scanner.py's continuous 2-5min cadence on this single-core box. So instead: SPY + the 11
SPDR sector ETFs (_watchlist_tickers(), reusing sector_rotation.SECTOR_ETFS -- one source of
truth, not a second hardcoded copy, same precedent dashboard_snapshot.py's _etf_tickers()
already set) get folded into the SAME 5-min refresh_advanced_gex() call this section
describes, unconditionally -- not gated on wall proximity. They never trigger a Telegram
alert or CVD read (that stays proximity-only); they just stay fresh in live_gex_snapshot.json
for the dashboard.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from advanced_gex import load_state, save_state
from gex import DEFAULT_R
from live_gex import compute_live, fetch_chain
from momentum_breakdown import CVD_Engine, cvd_slope
from wide_universe import _headers

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ET = ZoneInfo("America/New_York")

LIVE_GEX_PATH = DATA / "live_gex_snapshot.json"
WSS_INTRADAY_LOG = DATA / "wss_intraday_history.jsonl"
COOLDOWN_PATH = DATA / "wall_proximity_cooldown.json"
CONFLUENCE_PATH = DATA / "confluence_with_conviction.json"
HOT_SETUP_CHAINS_PATH = DATA / "hot_setup_chains.json"

PROXIMITY_THRESHOLD_PCT = 0.25  # tunable -- see module docstring
COOLDOWN_MINUTES = 60  # tunable -- see module docstring
CVD_LOOKBACK_BARS = 10  # ~50min of 5-min bars -- tunable, same "simple first cut" spirit
CVD_BARS_WINDOW_MIN = 150  # how far back to fetch bars for (comfortably >= lookback+warmup)

ULTRA_TIGHT_DIST_PCT = 0.05  # 2026-07-10, heff's ask: a second, higher-conviction tier
# within the existing 🎯 hot-setup population. REVISED 2026-07-10 (same day, later):
# wall_alert_scoring.py's ALERT_RE had two real parsing bugs (an unhandled 🎯 marker
# prefix since 2026-07-09, and an unhandled CVD-reversal-note line since 2026-07-05)
# that were silently dropping ~26% of ALL logged events from the ledger -- including
# the very ones this tier's "7/7 (100%)" claim was based on. After fixing both and
# re-scoring the full log (single canonical decluster by ticker+date, THEN split by
# dist_pct -- splitting first double-counted a few same-day ticker+date pairs that had
# both an ultra-tight AND a looser-band hit), the real numbers are: ultra-tight n=11,
# 9/11 (81.8%) inverted, z=-2.11; looser band (0.05-0.25%) n=29, 19/29 (65.5%) inverted.
# Ultra-tight is still the stronger sub-tier, just not literally perfect, and n=11 is
# thin enough that this should keep getting re-checked as more days accumulate.
# Informational tiering only, does not change is_hot_setup's own gate.

CHAIN_QUICKVIEW_BAND = 0.03  # +/-3% around spot -- hot setups are already within
# PROXIMITY_THRESHOLD_PCT (0.25%) of their wall, so this comfortably covers both spot
# and the wall level plus a few strikes either side without over-fetching. Same
# "simple first cut" tunable discipline as PROXIMITY_THRESHOLD_PCT itself.
CHAIN_QUICKVIEW_STRIKES = 3  # strikes shown each side of the wall level


def chain_quickview(ticker, exp, spot, wall_type, wall_price):
    """Nearby strikes (same option type as the wall itself -- a put wall shows put
    strikes, a call wall shows call strikes) + live IV + bid/ask, for a ticker/
    expiration a 🎯 high-conviction setup just fired on (2026-07-10, heff's ask).
    Reuses live_gex.fetch_chain() -- the exact Alpaca indicative-chain + free-OI
    fetch the rest of this pipeline already trusts (compute_live's 4x/day sweep,
    refresh_advanced_gex's 5-min near-wall refresh) -- not a second data source.
    Fails open (returns []) on any fetch error so a missing quick-view never blocks
    the alert itself, same discipline as refresh_advanced_gex's per-ticker try/except."""
    try:
        rows = fetch_chain(ticker, exp, spot, CHAIN_QUICKVIEW_BAND, DEFAULT_R)
    except Exception as e:
        print(f"  [warn] chain quickview failed for {ticker}: {e!r}")
        return []
    is_call = wall_type == "call"
    same_type = [r for r in rows if r["is_call"] == is_call and r.get("bid") and r.get("ask")]
    same_type.sort(key=lambda r: abs(r["strike"] - wall_price))
    nearest = sorted(same_type[:CHAIN_QUICKVIEW_STRIKES * 2], key=lambda r: r["strike"])
    return [{
        "strike": r["strike"], "is_call": r["is_call"], "iv": round(r["iv"], 3),
        "bid": r["bid"], "ask": r["ask"], "spread": round(r["ask"] - r["bid"], 2),
        "oi": r["oi"],
    } for r in nearest]


def _watchlist_tickers():
    """SPY + the 11 SPDR sector ETFs, unconditionally refreshed every 5min cycle
    regardless of wall proximity -- see module docstring's 'Curated watchlist' note.
    Reuses sector_rotation.SECTOR_ETFS (same single source of truth
    dashboard_snapshot.py's _etf_tickers() already pulls from) rather than a second
    hardcoded ticker list. Lazy import, matching that same precedent, to keep this
    module's own top-level import list minimal."""
    import sector_rotation as secrot
    return {"SPY"} | set(secrot.SECTOR_ETFS)


def market_is_open(now_et):
    """Deliberately a small local copy, not an import from mean_reversion_scanner.py
    -- that module pulls in pandas/numpy/Alpaca client setup at module load just for
    this one 5-line check, same 'minimal dependency surface' precedent live_gex.py
    already set for itself rather than importing options_orchestrator.py."""
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now_et <= close_t


def load_walls():
    """ticker -> {call_wall, put_wall, regime, wss_flag, wss_score, p_c,
    p_c_flow_state_tracked}, restricted to rows with a clean call/put wall read --
    skips 'no read'/error rows, same no-fabricated-levels honesty as the rest of
    this pipeline. WSS/P(C) are carried through even when null -- next_move_read()
    handles the missing case explicitly rather than silently dropping context."""
    if not LIVE_GEX_PATH.exists():
        return {}
    data = json.loads(LIVE_GEX_PATH.read_text())
    out = {}
    for r in data.get("results", []):
        if r.get("error") or r.get("call_wall") is None or r.get("put_wall") is None:
            continue
        out[r["ticker"]] = {
            "call_wall": r["call_wall"], "put_wall": r["put_wall"], "regime": r.get("regime"),
            "wss_flag": r.get("wss_flag"), "wss_score": r.get("wss_score"),
            "p_c": r.get("p_c"), "p_c_flow_state_tracked": r.get("p_c_flow_state_tracked"),
            "wall_confidence": r.get("wall_confidence"),
        }
    return out


def next_move_read(wss_flag, wss_score, p_c, p_c_partial, regime):
    """Plain-language structural read built from WSS + regime + P(C) -- all already
    computed and live (gex_quant_engine.py via advanced_gex.py). Deliberately NOT
    gex_quant_engine.py's Phase 4 AdvancedGammaExecutionEngine, which stays
    local-only per heff's own standing instruction: its real gates (CVD divergence,
    stacked imbalances) need tick-level order flow this 5-min snapshot alert doesn't
    have, and would mostly just return HOLD_STATE without it. This is a plain
    summary of already-validated fields, not a new signal or a trade call.
    """
    if wss_score is None or wss_flag is None:
        return "no wall-stability read available"

    if wss_score >= 0:
        strength = "wall structurally solid" if wss_flag == "HARD FLOOR" else "wall holding, some softening"
        lean = ("bounce/pin likely" if regime == "positive" else
                "watch closely -- negative-gamma regime can still amplify a break even off a currently-stable wall")
    else:
        strength = "wall cracking" if wss_flag == "FRACTURED" else "wall giving way"
        lean = "risk of continuation through the level"

    pc_bit = ""
    if p_c is not None:
        flag = "*" if p_c_partial else ""
        severity = " (elevated breakdown risk)" if p_c > 0.75 else ""
        pc_bit = f", P(C) {p_c * 100:.0f}%{flag}{severity}"

    # 1 decimal place, not 0 (2026-07-07 fix): a real score like -0.4 used to print as
    # "-0" -- wall_alert_scoring.py's regex then re-parsed that via int("-0") == 0,
    # silently losing the sign entirely for any score in (-1, 0). This corrupted 40 of
    # 236 logged events (17%) in the accuracy ledger's wss_score field -- their true
    # verdict text was still correct ("cracking"), but the paired score read as a
    # non-negative 0, making them indistinguishable from real small-positive readings
    # when bucketing by score alone. 1 decimal disambiguates sign down to +/-0.05.
    return f"{strength} ({wss_flag} {wss_score:+.1f}{pc_bit}) -- {lean}"


def load_confluence():
    """ticker -> confluence row from confluence_with_conviction.json (today's
    STRONG-confluence names only -- most alerted tickers won't have a row here,
    that's expected, not a gap: confluence rows only exist for names with 3+
    signals agreeing, at most 1 disagreeing)."""
    if not CONFLUENCE_PATH.exists():
        return {}
    data = json.loads(CONFLUENCE_PATH.read_text())
    return {r["ticker"]: r for r in data.get("rows", [])}


def fetch_live_prices(tickers):
    """Current price per ticker via Alpaca's bulk snapshot endpoint -- same batched
    call + auth helper already used by wide_universe.find_supplementary_movers(),
    reused here rather than re-implemented."""
    H = _headers()
    prices = {}
    tickers = sorted(tickers)
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        try:
            resp = requests.get("https://data.alpaca.markets/v2/stocks/snapshots", headers=H,
                                 params={"symbols": ",".join(batch), "feed": "iex"}, timeout=20)
            data = resp.json()
        except Exception as e:
            print(f"  [warn] snapshot batch failed: {e!r}")
            continue
        for sym in batch:
            snap = data.get(sym) or {}
            lt = snap.get("latestTrade")
            if lt and lt.get("p"):
                prices[sym] = lt["p"]
    return prices


def load_cooldowns():
    if not COOLDOWN_PATH.exists():
        return {}
    raw = json.loads(COOLDOWN_PATH.read_text())
    cutoff = datetime.now(timezone.utc).timestamp() - COOLDOWN_MINUTES * 60
    return {k: v for k, v in raw.items() if v > cutoff}


def save_cooldowns(cooldowns):
    DATA.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(cooldowns))


def send_telegram(message):
    subprocess.run(
        ["openclaw", "message", "send", "--channel", "telegram",
         "--target", "7590346809", "--message", message],
        capture_output=True,
    )


def find_proximate(walls, prices):
    """ALL ticker+wall pairs currently within PROXIMITY_THRESHOLD_PCT, regardless of
    alert cooldown. Deliberately separate from cooldown filtering -- this list drives
    the P(C)/WSS refresh (which should happen every cycle for any name currently near
    a wall, cooldown or not), while only the cooldown-filtered subset (see
    filter_new_hits) gets an actual Telegram alert."""
    proximate = []
    for ticker, w in walls.items():
        price = prices.get(ticker)
        if not price:
            continue
        for wall_type, wall_price in (("call", w["call_wall"]), ("put", w["put_wall"])):
            if not wall_price:
                continue
            dist_pct = abs(price - wall_price) / price * 100.0
            if dist_pct > PROXIMITY_THRESHOLD_PCT:
                continue
            proximate.append({
                "ticker": ticker, "wall_type": wall_type, "wall_price": wall_price,
                "price": price, "dist_pct": dist_pct, "regime": w["regime"],
                "wss_flag": w.get("wss_flag"), "wss_score": w.get("wss_score"),
                "p_c": w.get("p_c"), "p_c_flow_state_tracked": w.get("p_c_flow_state_tracked"),
                "wall_confidence": w.get("wall_confidence"),
            })
    return proximate


def filter_new_hits(proximate):
    """Cooldown-filtered subset of `proximate` -- what actually gets a Telegram
    alert this cycle. A ticker still sitting at a wall after its first alert keeps
    getting a fresh P(C) refresh (see refresh_advanced_gex) even while it's not
    alert-worthy again yet."""
    cooldowns = load_cooldowns()
    now_ts = datetime.now(timezone.utc).timestamp()
    hits = []
    for p in proximate:
        key = f"{p['ticker']}:{p['wall_type']}"
        if key in cooldowns:
            continue
        cooldowns[key] = now_ts
        hits.append(p)
    save_cooldowns(cooldowns)
    return hits


def check_proximity():
    """Convenience wrapper (kept for existing callers): cooldown-filtered NEW hits
    only, same behavior as before this file gained the proximate/refresh split."""
    walls = load_walls()
    if not walls:
        return []
    prices = fetch_live_prices(list(walls.keys()))
    return filter_new_hits(find_proximate(walls, prices))


def _append_intraday_history(ticker, res, now_et):
    """Appends one compact record per fresh WSS/GEX read for a wall-proximate ticker
    (heff's ask, 2026-07-07: the 3x/day full sweep + gex_daily_history.jsonl only ever
    capture ONE point per ticker per day, so there was no way to see how a wall's
    stability score actually moves through a single session). Reuses this function's
    existing 5-min cadence and already-computed values -- no new cron, no extra API
    calls, just an additive append. Fails open (never raises) -- a missed log line
    beats a crashed alert cycle.
    """
    try:
        record = {
            "ticker": ticker, "ts": now_et.isoformat(),
            "wss_score": res.get("wss_score"), "wss_flag": res.get("wss_flag"),
            "wall_confidence": res.get("wall_confidence"), "p_c": res.get("p_c"),
            "regime": res.get("regime"), "net_gex": res.get("net_gex"),
            "flip": res.get("flip"), "spot": res.get("spot"),
            "call_wall": res.get("call_wall"), "put_wall": res.get("put_wall"),
        }
        with WSS_INTRADAY_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"  [warn] intraday history append failed for {ticker}: {e!r}")


def refresh_advanced_gex(tickers):
    """For every ticker currently near a wall, fetch a FRESH options chain and
    recompute the full live_gex.py read (net_gex/flip/walls/WSS/P(C)/ghost_wall) via
    the exact same compute_live() path the 3x/day full sweep uses -- just for this
    small subset, not all 194 tickers. Patches those rows into
    live_gex_snapshot.json via an atomic write (temp file + os.replace(), same
    pattern wide_universe.py already uses for its own concurrent-writer risk) so a
    reader never sees a half-written file. Returns {ticker: fresh_result} so the
    caller can use the just-computed numbers immediately (e.g. in the alert
    message) instead of the pre-refresh values `find_proximate` was built from a
    few seconds earlier. Fails open per-ticker (thin chain, rate limit, etc) --
    a stale-but-present row beats a crash.
    """
    if not tickers:
        return {}
    iv_state, vex_history = load_state()
    fresh = {}
    now_et = datetime.now(ET)
    for t in tickers:
        try:
            res = compute_live(t, iv_state=iv_state, vex_history=vex_history)
            if not res.get("error"):
                fresh[t] = res
                _append_intraday_history(t, res, now_et)
        except Exception as e:
            print(f"  [warn] refresh failed for {t}: {e!r}")
    save_state(iv_state, vex_history)

    if fresh and LIVE_GEX_PATH.exists():
        snapshot = json.loads(LIVE_GEX_PATH.read_text())
        results = snapshot.get("results", [])
        by_ticker = {r["ticker"]: i for i, r in enumerate(results)}
        for t, res in fresh.items():
            if t in by_ticker:
                results[by_ticker[t]] = res
            else:
                results.append(res)
        snapshot["results"] = results
        tmp_path = LIVE_GEX_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(snapshot, indent=2))
        tmp_path.replace(LIVE_GEX_PATH)
    return fresh


def fetch_recent_bars(tickers):
    """5-min OHLCV bars for the last CVD_BARS_WINDOW_MIN minutes, bulk-fetched via Alpaca's
    multi-symbol bars endpoint (same batching precedent as fetch_live_prices()). Feeds
    CVD_Engine.from_bars() (momentum_breakdown.py's Module 3 -- a FREE bar-based buy/sell
    PRESSURE estimate from OHLCV shape, NOT true tick-level aggressor-tagged order flow).
    Scoped to just the tickers passed in (the current proximate subset, ~10-15 names) --
    same cost discipline as refresh_advanced_gex(), not the full 194-ticker universe.
    Returns {ticker: DataFrame[Open,High,Low,Close,Volume]}, skipping tickers with no bars.
    """
    if not tickers:
        return {}
    H = _headers()
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=CVD_BARS_WINDOW_MIN)
    out = {}
    tickers = sorted(tickers)
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        try:
            resp = requests.get(
                "https://data.alpaca.markets/v2/stocks/bars", headers=H,
                params={"symbols": ",".join(batch), "timeframe": "5Min", "feed": "iex",
                        "limit": 10000, "start": start.isoformat(), "end": end.isoformat()},
                timeout=20,
            )
            data = resp.json().get("bars", {})
        except Exception as e:
            print(f"  [warn] bars batch failed: {e!r}")
            continue
        for sym, bars in data.items():
            if not bars:
                continue
            df = pd.DataFrame(bars).rename(
                columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
            out[sym] = df
    return out


def cvd_reversal_read(wall_type, regime, df):
    """Composite CVD-vs-wall reversal watch (heff's ask, 2026-07-05): does net buy/sell
    PRESSURE (the free bar-based CVD proxy, momentum_breakdown.py's CVD_Engine) confirm or
    contradict what a naive read of price-at-a-wall would expect? A put-wall test in a
    NEGATIVE regime is structurally the more fragile case (dealers destabilizing, more room
    to keep breaking down) -- if CVD is ALSO ticking UP there (net aggressive buying
    building), that's a genuine divergence: bulls stepping in exactly where the setup looks
    weakest. Symmetric call-wall + CVD-ticking-down case for the bearish side. Mirrors
    gex_quant_engine.py's Phase 4 AdvancedGammaExecutionEngine test scenarios ("Put Wall +
    bullish CVD divergence -> MEAN_REVERSION_LONG") but fed the free bar-based proxy instead
    of the tick-level order flow that engine needs and this pipeline doesn't have (same
    reasoning next_move_read() already documents for staying off that engine).

    Deliberately informational only -- never gates/blocks anything, same discipline as every
    other signal in this codebase. Returns None (fail-quiet) if there's not enough bar
    history yet or the CVD slope doesn't point the "confirming" direction for this wall.
    """
    if df is None or len(df) < CVD_LOOKBACK_BARS + 1:
        return None
    cvd = CVD_Engine.from_bars(df)
    slope = float(cvd_slope(cvd, CVD_LOOKBACK_BARS).iloc[-1])
    if wall_type == "put" and slope > 0:
        tag = "reversal-watch" if regime == "negative" else "buying pressure building"
        return f"CVD +{slope:,.0f} net buying/{CVD_LOOKBACK_BARS}bars (bar-est.) -- {tag}"
    if wall_type == "call" and slope < 0:
        tag = "reversal-watch" if regime == "positive" else "selling pressure building"
        return f"CVD {slope:,.0f} net selling/{CVD_LOOKBACK_BARS}bars (bar-est.) -- {tag}"
    return None


def patch_cvd_into_snapshot(notes):
    """Writes cvd_reversal_note onto each ticker's row in live_gex_snapshot.json (same
    atomic temp-file+replace pattern refresh_advanced_gex() already uses) so the dashboard's
    existing gex_view() pass-through picks it up with zero plumbing changes on that side --
    same precedent as the flip_reason field (2026-07-05). notes: {ticker: str or None}."""
    if not notes or not LIVE_GEX_PATH.exists():
        return
    snapshot = json.loads(LIVE_GEX_PATH.read_text())
    results = snapshot.get("results", [])
    by_ticker = {r["ticker"]: r for r in results}
    for t, note in notes.items():
        if t in by_ticker:
            by_ticker[t]["cvd_reversal_note"] = note
    snapshot["results"] = results
    tmp_path = LIVE_GEX_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2))
    tmp_path.replace(LIVE_GEX_PATH)


def main():
    now_et = datetime.now(ET)
    if not market_is_open(now_et):
        print(f"{now_et.isoformat()} market closed, skipping")
        return

    walls = load_walls()
    if not walls:
        print(f"{now_et.isoformat()} no wall data available")
        return
    prices = fetch_live_prices(list(walls.keys()))
    proximate = find_proximate(walls, prices)

    proximate_tickers = sorted({p["ticker"] for p in proximate})
    watchlist = _watchlist_tickers()
    refresh_tickers = sorted(set(proximate_tickers) | watchlist)
    fresh = refresh_advanced_gex(refresh_tickers)
    if refresh_tickers:
        overlap = len(watchlist & set(proximate_tickers))
        print(f"{now_et.isoformat()} refreshed P(C)/WSS for {len(fresh)}/{len(refresh_tickers)} tickers "
              f"({len(proximate_tickers)} wall-proximate + {len(watchlist) - overlap} watchlist, {overlap} overlap)")

    # Use the numbers just computed above, not the pre-refresh values `proximate`
    # was built from moments earlier -- otherwise the alert would print stale
    # WSS/P(C) right after a fresher read was already fetched.
    for p in proximate:
        r = fresh.get(p["ticker"])
        if r:
            p["wss_flag"], p["wss_score"] = r.get("wss_flag"), r.get("wss_score")
            p["p_c"], p["p_c_flow_state_tracked"] = r.get("p_c"), r.get("p_c_flow_state_tracked")
            p["wall_confidence"] = r.get("wall_confidence")

    # CVD reversal watch (2026-07-05, heff's ask): same proximate-subset scoping as the
    # P(C)/WSS refresh above -- bars for ~10-15 names, not the full universe.
    bars_by_ticker = fetch_recent_bars(proximate_tickers)
    cvd_notes = {}
    for p in proximate:
        note = cvd_reversal_read(p["wall_type"], p["regime"], bars_by_ticker.get(p["ticker"]))
        p["cvd_note"] = note
        if note:
            cvd_notes[p["ticker"]] = note
    patch_cvd_into_snapshot(cvd_notes)

    hits = filter_new_hits(proximate)
    if not hits:
        print(f"{now_et.isoformat()} no NEW wall-proximity hits (cooldown or none)")
        return

    confluence = load_confluence()

    lines = [f"WALL ALERT ({now_et.strftime('%Y-%m-%d %H:%M ET')}):"]
    alerted, suppressed = 0, 0
    # High-conviction setup (2026-07-09, heff's ask): put wall + negative gamma + "holding"
    # verdict (wss_score>=0). Research across 4 days/21 events found this exact combo's
    # "holding" call is WRONG 85.7% of the time (z=-3.27) -- the crack is far more likely
    # than the bounce it's nominally predicting. Collected separately here so it can be
    # sent as its own standout Telegram message below, not buried inside the regular
    # multi-ticker roll-up -- heff's own words were "I want an edge I can trade that
    # doesn't give a million signals."
    hot_setups = []
    for h in hits:
        # Thin-chain suppression (2026-07-09, heff's ask): don't send a confidently-worded
        # holding/cracking verdict for a read that's been shown to flip-flop sign on
        # essentially every refresh (see live_gex.py's MIN_OI_FOR_CONFIDENT_WSS / KDP case).
        # Still counted against this cycle's cooldown (filter_new_hits already ran above) --
        # simplest correct behavior, costs at most one missed re-alert within the 60min
        # cooldown window if the chain becomes reliable again soon after.
        if h.get("wall_confidence") == "thin":
            suppressed += 1
            continue
        alerted += 1
        is_hot_setup = (
            h["regime"] == "negative"
            and h.get("wss_score") is not None and h["wss_score"] >= 0
        )
        # Ultra-tight tier (2026-07-10): informational-only refinement within is_hot_setup,
        # does not change the gate itself -- see ULTRA_TIGHT_DIST_PCT above.
        h["is_ultra_tight"] = is_hot_setup and h["dist_pct"] <= ULTRA_TIGHT_DIST_PCT
        if is_hot_setup:
            hot_setups.append(h)
        marker = "🎯🎯 " if h["is_ultra_tight"] else ("🎯 " if is_hot_setup else "")
        lines.append(
            f"{marker}{h['ticker']}: ${h['price']:.2f} within {h['dist_pct']:.2f}% of "
            f"{h['wall_type']} wall ${h['wall_price']:.2f} ({h['regime'] or 'no read'})"
        )
        lines.append("  " + next_move_read(h["wss_flag"], h["wss_score"], h["p_c"],
                                            h["p_c_flow_state_tracked"], h["regime"]))
        if h.get("cvd_note"):
            lines.append("  " + h["cvd_note"])
        c = confluence.get(h["ticker"])
        if c:
            conv = "high conviction" if c.get("conviction") == "high" else "flow-only"
            sig_str = ",".join(c.get("signals", {}).keys())
            lines.append(f"  Confluence: {c['lean']} {c['n_bull']}b/{c['n_bear']}b of "
                          f"{c['n_total']} [{sig_str}] ({conv})")
        else:
            lines.append("  Confluence: no strong signal today")

    if alerted == 0:
        print(f"{now_et.isoformat()} {suppressed} hit(s) suppressed (thin chain, unreliable read), nothing alert-worthy")
        return

    lines.append("* = P(C) partial (order-flow state not tracked live yet)")
    if suppressed:
        lines.append(f"({suppressed} additional hit(s) suppressed -- thin chain, unreliable read)")
    message = "\n".join(lines)
    print(message)  # kept for wall_alert_scoring.py's log-parsing pipeline -- NOT sent to Telegram anymore

    # Standalone high-conviction ping (2026-07-09): sent as its OWN message, separate from
    # the roll-up above, specifically so it stands out instead of scrolling past inside a
    # multi-ticker alert. Only fires when this exact researched combo is present.
    if hot_setups:
        n_ultra = sum(1 for h in hot_setups if h["is_ultra_tight"])
        hs_lines = [
            f"🎯 HIGH-CONVICTION SETUP ({len(hot_setups)}"
            + (f", {n_ultra} \U0001F3AF\U0001F3AF ultra-tight" if n_ultra else "")
            + ") -- negative gamma + 'holding' call",
            "",
            "This combo's 'holding' verdict has been WRONG 70.0% of the time historically "
            "(n=40 events / 5 trading days, z=-2.53, pooled across BOTH put and call walls -- "
            "the mechanism is the negative-gamma regime itself, not which wall is being tested) "
            "-- the crack is more likely than the bounce it's nominally predicting, though "
            "not as lopsided as earlier readings claimed (a scoring-pipeline bug was silently "
            "dropping ~26% of events; fixed and backfilled 2026-07-10). "
            f"\U0001F3AF\U0001F3AF marks dist_pct<={ULTRA_TIGHT_DIST_PCT}% -- 9/11 (81.8%) inverted "
            "in the same research, vs. 65.5% for the looser band.",
            "",
            "If trading the inversion: exit 15-30min after this alert (win rate peaks there, then "
            "decays every step after); 60min is still defensible for a bigger average $ move but win rate "
            "has already faded by then; NEVER hold into the close (worst exit on every metric).",
            "",
        ]
        # Chain quick-view (2026-07-10, heff's ask): near-wall strikes/IV/spread for each
        # hot-setup ticker, reusing the expiration compute_live() already picked this cycle
        # (see `fresh`, built above by refresh_advanced_gex). Also persisted to
        # HOT_SETUP_CHAINS_PATH so dashboard_snapshot.py can surface the same read as a
        # dashboard panel -- one live chain pull serves both surfaces.
        chain_by_ticker = {}
        for h in hot_setups:
            exp = fresh.get(h["ticker"], {}).get("expiration")
            if not exp:
                continue
            chain = chain_quickview(h["ticker"], exp, h["price"], h["wall_type"], h["wall_price"])
            if chain:
                chain_by_ticker[h["ticker"]] = chain

        for h in hot_setups:
            tier_marker = "🎯🎯" if h["is_ultra_tight"] else "🎯"
            hs_lines.append(
                f"{tier_marker} {h['ticker']}: ${h['price']:.2f} within {h['dist_pct']:.2f}% of "
                f"{h['wall_type']} wall ${h['wall_price']:.2f}"
            )
            hs_lines.append("  " + next_move_read(h["wss_flag"], h["wss_score"], h["p_c"],
                                                   h["p_c_flow_state_tracked"], h["regime"]))
            chain = chain_by_ticker.get(h["ticker"])
            if chain:
                opt = "C" if h["wall_type"] == "call" else "P"
                strikes_str = ", ".join(
                    f"${r['strike']:.0f}{opt} iv={r['iv'] * 100:.0f}% "
                    f"${r['bid']:.2f}/{r['ask']:.2f} oi={r['oi']:.0f}"
                    for r in chain
                )
                hs_lines.append(f"  Chain: {strikes_str}")
        hs_message = "\n".join(hs_lines)
        print(hs_message)
        send_telegram(hs_message)

        DATA.mkdir(parents=True, exist_ok=True)
        chains_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "setups": [
                {
                    "ticker": h["ticker"], "wall_type": h["wall_type"], "wall_price": h["wall_price"],
                    "price": h["price"], "dist_pct": h["dist_pct"], "is_ultra_tight": h["is_ultra_tight"],
                    "chain": chain_by_ticker.get(h["ticker"], []),
                }
                for h in hot_setups
            ],
        }
        tmp_path = HOT_SETUP_CHAINS_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(chains_payload, indent=2))
        tmp_path.replace(HOT_SETUP_CHAINS_PATH)


if __name__ == "__main__":
    main()
