#!/usr/bin/env python3
"""live_gex.py — LIVE (today's) net GEX/flip/walls per name, computed from Alpaca's FREE
indicative options chain (live quotes + free open interest via /v2/options/contracts) -- NOT
the frozen Databento historical research pull gex.py normally reads (data/options/{SYM}_gex.
parquet, static at whatever date that one-time pull finished, currently 2026-05-29). Reuses
greeks.py's BS math (implied_vol) and gex.py's compute_day() aggregation UNCHANGED; this file
only adds the live data-fetch layer (contracts+quotes -> IV -> the DataFrame shape compute_day
already expects). No new paid data source -- OI is free on Alpaca's indicative feed (T-1 lag,
verified 2026-06-25), IV is derived from free live quotes, not bought.

Deliberately does NOT import options_orchestrator.py (avoids pulling its logging.basicConfig
reconfiguration + guardrails/options_eval import chain into what should be a small, frequent,
network-bound script) -- this is its own minimal Alpaca REST helper, same "minimal dependency
surface" call already made by guardrails.py/portfolio_gate.py's own local config loaders. The
single-expiration/NTM-band methodology mirrors the historical Databento pull's own simplification
(monthly-ish expiry, +-10-15% band) so live reads stay roughly comparable to the backtest baseline.

Writes data/live_gex_snapshot.json. dashboard_snapshot.py's gex_view() reads this file and falls
back to the historical parquet reader only if it doesn't exist yet -- so the ALREADY-DEPLOYED
dashboard GEX panel picks up live numbers with ZERO frontend changes (box-only, no Netlify
redeploy needed; output field shape is identical to what the frontend already renders).

Run: ./venv/bin/python live_gex.py [--tickers F,BAC,...] [--dte 30] [--band 0.15]
"""
import argparse
import json
import math
import time
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import greeks
import wide_universe
import sector_rotation
import advanced_gex
from gex import compute_day, DEFAULT_R

ROOT = Path(__file__).resolve().parent
KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}
PAPER = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
OUT_PATH = ROOT / "data" / "live_gex_snapshot.json"
HISTORY_PATH = ROOT / "data" / "gex_daily_history.jsonl"  # forward-accumulating daily series (see append_history)
OUT_PATH_0DTE = ROOT / "data" / "live_gex_0dte_snapshot.json"  # separate file: own cron/lock, no write-contention with the full sweep

# Fallback only, used if wide_universe's cache is unavailable -- the same 6 names the
# historical Databento pull successfully covered (see options-tournament memory:
# XLF/GDX/HOOD/NVDA/AMD-OI had gaps in THAT pull). Verified safe for live use 2026-07-02/03.
# Real default (2026-07-03, heff's direction: maximize forward GEX history collection) is the
# full wide_universe list below, in main() -- same ~180-symbol universe MR/ORB actually trade,
# so a future GEX-regime backtest has maximum overlap with the real signal universe instead of
# the 5-of-11-symbol overlap gex_regime_backtest.py was stuck with against the frozen Databento
# pull.
DEFAULT_TICKERS = ("AMD", "BAC", "CSCO", "F", "INTC", "PFE")


def _get(base, path, **params):
    r = requests.get(base + path, headers=H, params=params or None, timeout=25)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


def spot(ticker):
    sc, s = _get(DATA_BASE, f"/v2/stocks/{ticker}/snapshot", feed="iex")
    if sc != 200:
        return None
    return (s.get("latestTrade") or {}).get("p")


def _is_monthly_opex(d):
    """Standard monthly options expiration: 3rd Friday of the month."""
    return d.weekday() == 4 and 15 <= d.day <= 21


def pick_expiration(ticker, dte_target, min_dte=7):
    """Soonest standard MONTHLY OPEX (3rd Friday) that's >= min_dte days out -- changed
    2026-07-06 (heff's ask). The old version picked whichever listed expiration (weekly OR
    monthly) was closest to dte_target days away, which on 7/6 landed WMT on 8/7 -- a plain
    WEEKLY (first Friday of August, thin/off-cycle OI), not August's real monthly OPEX (8/21),
    just because 8/7 happened to be a few days closer to a raw 30-day count. Front-month OPEX
    carries the deep, institutional OI a GEX read actually wants. Always the CURRENT front
    month: 7/17 today, rolls to 8/21 automatically the next trading day after 7/17 passes
    (since expiration_date_gte=today naturally drops it from the listing), no dte_target
    involved in that case at all. dte_target is now only a LAST-RESORT fallback (closest ANY
    listed expiration) for the rare ticker with no monthly-OPEX contract listed at all
    (fail-open, e.g. a thin name that only lists weeklies)."""
    today = date.today()
    sc, c = _get(PAPER, "/v2/options/contracts", underlying_symbols=ticker,
                 expiration_date_gte=today.isoformat(), limit=10000)
    if sc != 200:
        return None
    exps = sorted({x["expiration_date"] for x in c.get("option_contracts", [])})
    valid = [e for e in exps if (date.fromisoformat(e) - today).days >= min_dte]
    monthly = sorted(e for e in valid if _is_monthly_opex(date.fromisoformat(e)))
    if monthly:
        return monthly[0]
    cand = [(abs((date.fromisoformat(e) - today).days - dte_target), e) for e in valid]
    return min(cand)[1] if cand else None


def fetch_chain(ticker, exp, S, band, r):
    """Contracts in an NTM band for one expiration, with live indicative quotes + free OI +
    COMPUTED IV -- returns rows shaped for gex.compute_day() (strike/is_call/T/iv/oi)."""
    lo, hi = S * (1 - band), S * (1 + band)
    sc, c = _get(PAPER, "/v2/options/contracts", underlying_symbols=ticker,
                 expiration_date=exp, strike_price_gte=f"{lo:.2f}", strike_price_lte=f"{hi:.2f}",
                 limit=10000)
    if sc != 200:
        return []
    rows = {x["symbol"]: x for x in c.get("option_contracts", [])}
    if not rows:
        return []
    syms = list(rows)
    T = max((date.fromisoformat(exp) - date.today()).days, 1) / 365.0
    out = []
    for i in range(0, len(syms), 100):                       # batch quote requests (API max)
        batch = syms[i:i + 100]
        sc, q = _get(DATA_BASE, "/v1beta1/options/quotes/latest", symbols=",".join(batch), feed="indicative")
        if sc != 200:
            continue
        for sym, quote in q.get("quotes", {}).items():
            bp, ap = quote.get("bp", 0), quote.get("ap", 0)
            if not (bp > 0 and ap > 0):
                continue
            meta = rows[sym]
            oi = meta.get("open_interest")
            if oi is None:
                continue
            K = float(meta["strike_price"])
            is_call = meta["type"] == "call"
            mid = 0.5 * (bp + ap)
            iv = float(greeks.implied_vol(mid, S, K, T, r, is_call))
            if not math.isfinite(iv) or iv <= 0:
                continue
            # 2026-07-04: mid_price retained for VolatilitySurfaceCalibrator (advanced_gex.py).
            # bid/ask retained 2026-07-10 for wall_proximity_alert.py's chain_quickview() --
            # gex.compute_day() only reads strike/is_call/T/iv/oi, so extra columns here are
            # inert for the existing GEX-math callers, same precedent as mid_price above.
            out.append({"strike": K, "is_call": is_call, "T": T, "iv": iv, "oi": float(oi),
                        "mid_price": mid, "bid": bp, "ask": ap})
        time.sleep(0.2)   # gentle pacing -- small fixed ticker list, no TokenBucket needed
    return out


# Minimum real-OI contracts before trusting wall_confidence's sign-based read at all
# (2026-07-09, heff's ask). Separate from -- and higher than -- dashboard_snapshot.py's
# _flip_reason() n_oi<6 threshold (that one only gates the flip LEVEL); this one exists
# because KDP (n_oi=9, clears that lower bar) was observed flip-flopping every ~other
# 5min refresh between FRACTURED (score~0, no flip) and YIELDING (score~+30, real flip
# ~ away from spot) while spot barely moved -- the SIGN itself is unstable on a chain
# this thin, not just the flip level. First-cut/tunable, not precisely calibrated, same
# discipline as PROXIMITY_THRESHOLD_PCT/COOLDOWN_MINUTES elsewhere in this pipeline.
MIN_OI_FOR_CONFIDENT_WSS = 15


def _apply_thin_chain_gate(out):
    """Overrides wall_confidence to "thin" when there isn't enough real OI to trust
    the WSS sign read, regardless of what compute_advanced() computed. Leaves wss_score/
    wss_flag/p_c untouched (still shown as-is) -- this only changes the CONFIDENCE label,
    same spirit as high/low already being informational-only, never gating the alert
    verdict text itself."""
    if out.get("wall_confidence") is not None and out.get("n_oi", 0) < MIN_OI_FOR_CONFIDENT_WSS:
        out["wall_confidence"] = "thin"
    return out


def compute_live(ticker, dte_target=30, band=0.15, r=DEFAULT_R,
                  vix_vxv_ratio=None, iv_state=None, vex_history=None):
    S = spot(ticker)
    if not S:
        return {"ticker": ticker, "error": "no spot"}
    exp = pick_expiration(ticker, dte_target)
    if not exp:
        return {"ticker": ticker, "error": "no expiration found"}
    rows = fetch_chain(ticker, exp, S, band, r)
    if not rows:
        return {"ticker": ticker, "error": "no quoted contracts in band"}
    df = pd.DataFrame(rows)
    df["date"] = date.today().isoformat()
    res = compute_day(df, S, r)
    out = {
        "ticker": ticker, "expiration": exp,
        "as_of": res["date"], "spot": round(float(res["spot"]), 2),
        "regime": res["regime"] if res["regime"] != "none" else None,
        "net_gex": None if not np.isfinite(res["net_gex"]) else round(float(res["net_gex"]), 2),
        "flip": None if not np.isfinite(res["flip"]) else round(float(res["flip"]), 2),
        "call_wall": None if not np.isfinite(res["call_wall"]) else round(float(res["call_wall"]), 2),
        "put_wall": None if not np.isfinite(res["put_wall"]) else round(float(res["put_wall"]), 2),
        "coverage": round(float(res["coverage"]), 2),
        "n_oi": int(res["n_oi"]), "m_contracts": int(res["m_contracts"]),
    }
    # Phase 2/3 second-order-Greek layer (gex_quant_engine.py via advanced_gex.py),
    # added 2026-07-04 -- opt-in via iv_state/vex_history so compute_live_0dte and
    # any other caller that doesn't pass them keeps the exact old return shape.
    # See advanced_gex.py's module docstring for what's real vs. honestly stubbed
    # (F_state/ghost_wall need intraday order-flow data this box doesn't ingest yet).
    if iv_state is not None and vex_history is not None:
        try:
            gamma_flip = res["flip"] if np.isfinite(res["flip"]) else None
            out.update(advanced_gex.compute_advanced(
                ticker, rows, S, r, gamma_flip, vix_vxv_ratio, iv_state, vex_history
            ))
            _apply_thin_chain_gate(out)
        except Exception as e:
            out["advanced_error"] = str(e)[:140]
    return out


def _logged_days():
    """(ticker, as_of) pairs already present in the history log, so each ticker gets at most
    one row per calendar day -- the FIRST successful read of the day (closest to the open),
    matching gex_regime_backtest.py's own T-1-OI-predicts-today framing. The live cron runs
    every 5min during RTH; without this dedup the log would grow ~90 rows/ticker/day for a
    value (OI) that barely moves intraday, per this module's own docstring."""
    if not HISTORY_PATH.exists():
        return set()
    seen = set()
    for line in HISTORY_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            seen.add((row["ticker"], row["as_of"]))
        except Exception:
            continue
    return seen


def append_history(results):
    """Append only the first clean (non-error) read of each new calendar day per ticker --
    builds a free, forward-accumulating daily GEX series from live_gex's own live reads, since
    Alpaca has no historical open-interest endpoint to reconstruct the past from (see this
    module's docstring). This is what eventually makes a real GEX-regime-vs-ORB backtest
    possible, the way gex_regime_backtest.py did against the frozen Databento pull -- just
    accumulated for free going forward instead of bought looking backward."""
    already = _logged_days()
    new_rows = [r for r in results if not r.get("error") and r.get("as_of")
                and (r["ticker"], r["as_of"]) not in already]
    if not new_rows:
        return
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a") as f:
        for r in new_rows:
            f.write(json.dumps({**r, "logged_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    print(f"appended {len(new_rows)} new day(s) to {HISTORY_PATH}")


def pick_0dte_expiration(ticker):
    """Today's own expiration, if the underlying lists one. Daily-expiry products (SPY/QQQ/
    IWM-style) list a same-day contract on every real trading day; returns None on holidays/
    non-trading days or if the underlying just doesn't have a same-day expiration."""
    today = date.today().isoformat()
    sc, c = _get(PAPER, "/v2/options/contracts", underlying_symbols=ticker,
                 expiration_date=today, limit=10000)
    if sc != 200 or not c.get("option_contracts"):
        return None
    return today


def compute_live_0dte(ticker, band=0.05, r=DEFAULT_R,
                       vix_vxv_ratio=None, iv_state=None, vex_history=None):
    """Same-day (0DTE) GEX read -- separate from compute_live()'s ~30 DTE monthly-cycle read,
    which structurally can't capture 0DTE-specific dealer positioning (added 2026-07-03, heff's
    direction: he day-trades SPY 0DTEs and needs the gamma picture for the contracts actually
    expiring TODAY, not a monthly proxy -- a real trade came down to this: bought 750 puts at
    SPY 751.20, exited early near breakeven at $20 when the contracts peaked near $836, because
    there was no confluence read to support staying in). Tighter default band (5% vs the 30-DTE
    path's 15%) since 0DTE open interest concentrates close to spot -- verified empirically
    (99 contracts at 5% vs 69 at 3% on a near-dated SPY test, both comfortably under one
    100-contract quote batch). Ticker is tagged "{SYM}-0DTE" so it reads as a distinct row
    alongside the regular per-name entries on the dashboard, no frontend changes needed.

    Phase 2/3 advanced layer (gex_quant_engine.py via advanced_gex.py) wired in 2026-07-06,
    same opt-in pattern as compute_live(): only runs when iv_state/vex_history are passed.
    Uses the SAME "{ticker}-0DTE" tag as the iv_state/vex_history key (not the bare ticker),
    so 0DTE's IV/VEX rolling state never mixes with the ~30-DTE monthly read's own state for
    the same underlying -- see advanced_gex.save_state()'s docstring for why that separation
    is what makes concurrent saves from this path and the full sweep safe."""
    tag = f"{ticker}-0DTE"
    S = spot(ticker)
    if not S:
        return {"ticker": tag, "error": "no spot"}
    exp = pick_0dte_expiration(ticker)
    if not exp:
        return {"ticker": tag, "error": "no same-day expiration listed (holiday/non-trading day?)"}
    rows = fetch_chain(ticker, exp, S, band, r)
    if not rows:
        return {"ticker": tag, "error": "no quoted 0DTE contracts in band"}
    df = pd.DataFrame(rows)
    df["date"] = date.today().isoformat()
    res = compute_day(df, S, r)
    out = {
        "ticker": tag, "expiration": exp,
        "as_of": res["date"], "spot": round(float(res["spot"]), 2),
        "regime": res["regime"] if res["regime"] != "none" else None,
        "net_gex": None if not np.isfinite(res["net_gex"]) else round(float(res["net_gex"]), 2),
        "flip": None if not np.isfinite(res["flip"]) else round(float(res["flip"]), 2),
        "call_wall": None if not np.isfinite(res["call_wall"]) else round(float(res["call_wall"]), 2),
        "put_wall": None if not np.isfinite(res["put_wall"]) else round(float(res["put_wall"]), 2),
        "coverage": round(float(res["coverage"]), 2),
        "n_oi": int(res["n_oi"]), "m_contracts": int(res["m_contracts"]),
    }
    if iv_state is not None and vex_history is not None:
        try:
            gamma_flip = res["flip"] if np.isfinite(res["flip"]) else None
            out.update(advanced_gex.compute_advanced(
                tag, rows, S, r, gamma_flip, vix_vxv_ratio, iv_state, vex_history
            ))
            _apply_thin_chain_gate(out)
        except Exception as e:
            out["advanced_error"] = str(e)[:140]
    return out


def main_0dte(tickers, band=0.05):
    """Fast, narrow path: just the 0DTE tickers, own output file, own cron lock -- still
    independent of the full-universe sweep's OUTPUT file/lock (different cadence entirely --
    this runs every 30min intraday, the big sweep 4x/day at fixed times). Since 2026-07-06
    this DOES share the Phase 2/3 advanced-math state files (iv_intraday_state.json,
    vex_history.json) with the full sweep -- safe because both load_state()/save_state()
    and compute_live_0dte()'s own "{ticker}-0DTE" key tag keep the two runs' state fully
    disjoint (see advanced_gex.save_state()'s docstring for the concurrency argument)."""
    vix_vxv_ratio = advanced_gex.fetch_vix_vxv_ratio()
    iv_state, vex_history = advanced_gex.load_state()
    results = []
    for t in tickers:
        try:
            res = compute_live_0dte(t, band=band, vix_vxv_ratio=vix_vxv_ratio,
                                     iv_state=iv_state, vex_history=vex_history)
        except Exception as e:
            res = {"ticker": f"{t}-0DTE", "error": str(e)[:140]}
        results.append(res)
        print(f"  {t}-0DTE: {res}")
    advanced_gex.save_state(iv_state, vex_history)
    snap = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results}
    OUT_PATH_0DTE.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH_0DTE.write_text(json.dumps(snap, indent=2))
    print(f"wrote {OUT_PATH_0DTE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--0dte", action="store_true", dest="mode_0dte",
                    help="compute same-day (0DTE) GEX instead of the ~30 DTE monthly read; "
                         "writes to a separate file, independent of the main sweep")
    ap.add_argument("--0dte-tickers",
                    default="SPY,AAPL,MSFT,GOOGL,GOOG,AMZN,NVDA,META,TSLA",
                    dest="tickers_0dte")  # SPY + Mag 7 (2026-07-03, heff's direction).
                    # Verified: AAPL/MSFT/GOOGL/AMZN/NVDA/META/TSLA list Mon/Wed/Fri
                    # expirations (real same-day contracts those days, not just Friday);
                    # GOOG (the other share class) only lists Friday -- gracefully returns
                    # "no same-day expiration" via pick_0dte_expiration() on other days,
                    # same mechanism as the holiday case. All 8 verified fast (~13s total,
                    # 0-42 contracts each) well within the existing 2min cron cadence.
    ap.add_argument("--0dte-band", type=float, default=0.05, dest="band_0dte")
    ap.add_argument("--tickers", default=None,
                    help="comma-separated override; default is the full wide_universe list, "
                         "falling back to DEFAULT_TICKERS if that cache is unavailable")
    ap.add_argument("--dte", type=int, default=30)
    ap.add_argument("--band", type=float, default=0.15)
    a = ap.parse_args()
    if a.mode_0dte:
        tickers = [t.strip().upper() for t in a.tickers_0dte.split(",") if t.strip()]
        main_0dte(tickers, band=a.band_0dte)
        return
    if a.tickers:
        tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    else:
        tickers = wide_universe.load_universe(rebuild_if_stale=False) or list(DEFAULT_TICKERS)
        # SPY + the 11 SPDR sector ETFs added 2026-07-03 (heff's direction: broad-market +
        # sector-level GEX as confluence signals, all surfaced at the top of the dashboard
        # panel -- see dashboard_snapshot.py's gex_view()). None of these are part of the
        # tradeable single-name universe so they're explicitly appended rather than picked up
        # from wide_universe. Reuses sector_rotation.py's own SECTOR_ETFS list (same 11 names
        # already used for the sector-rotation gate) rather than a second hardcoded copy.
        # CAVEAT (SPY specifically, verified 2026-07-03): this reuses the same single-expiration
        # (~30 DTE) methodology validated for individual stocks; SPY's real gamma exposure is
        # dominated by weekly/0DTE strikes this doesn't capture (that's what live_gex.py's
        # separate --0dte mode is for), so treat the regime direction as a reasonable simplified
        # read, not a precise aggregate SPY GEX the way a dedicated service (e.g. SpotGamma)
        # computes it. Sector ETFs verified individually 2026-07-03: 8-82 contracts each,
        # ~9.4s/ticker average, clean reads across all 11 -- comparable cost to a single stock.
        extra = [t for t in (["SPY"] + list(sector_rotation.SECTOR_ETFS)) if t not in tickers]
        tickers = tickers + extra
        # High-price-but-genuinely-liquid S&P 500 names added 2026-07-06 (heff's ask): the
        # trading scanners' wide_universe caps at PRICE_MAX=$266, which structurally excludes
        # real megacaps that just trade for four figures (COST, BKNG, NVR, AZO, MELI, REGN,
        # etc.) even though their DOLLAR liquidity dwarfs most of the priced cohort. GEX
        # dashboard should see these regardless of price -- see
        # wide_universe.fetch_high_price_liquid_names()'s docstring for why share-count ADV
        # isn't a fair filter here. GEX-dashboard-only, same as the SPY+sector-ETF addition
        # above: NOT folded into the trading scanners' own universe (never backtested there).
        try:
            liquid_extra = [t for t in wide_universe.load_high_price_liquid_universe(set(tickers))
                             if t not in tickers]
            tickers = tickers + liquid_extra
        except Exception as e:
            print(f"  high-price-liquid supplement failed (non-fatal, skipping): {e}")
    results = []
    vix_vxv_ratio = advanced_gex.fetch_vix_vxv_ratio()
    iv_state, vex_history = advanced_gex.load_state()
    for t in tickers:
        try:
            res = compute_live(t, dte_target=a.dte, band=a.band,
                                vix_vxv_ratio=vix_vxv_ratio, iv_state=iv_state, vex_history=vex_history)
        except Exception as e:
            res = {"ticker": t, "error": str(e)[:140]}
        results.append(res)
        print(f"  {t}: {res}")
        time.sleep(0.3)
    advanced_gex.save_state(iv_state, vex_history)
    snap = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": results}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(snap, indent=2))
    print(f"wrote {OUT_PATH}")
    append_history(results)


if __name__ == "__main__":
    main()
