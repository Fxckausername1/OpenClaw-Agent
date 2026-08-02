#!/usr/bin/env python3
"""unusualwhales_client.py -- thin REST client for the Unusual Whales API.

Reads the bearer token from credentials/unusualwhales_key.txt (same
convention as Alpaca's key/secret files -- see live_gex.py). This is a
bounded, ~2-week validation/data-pull project (trial tier: 30,000 req/day,
90-day historical lookback, personal use only) against gex_quant_engine.py's
own GEX/VEX/CHEX/WSS/P(C) output -- not a permanent dependency.

Endpoint paths confirmed directly from the real OpenAPI spec
(https://api.unusualwhales.com/api/openapi), not guessed from docs prose.
"""
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
TOKEN = (ROOT / "credentials" / "unusualwhales_key.txt").read_text().strip()
BASE = "https://api.unusualwhales.com"
H = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}


def _get(path: str, **params):
    r = requests.get(BASE + path, headers=H, params=params or None, timeout=25)
    try:
        body = r.json()
    except Exception:
        body = r.text
    return r.status_code, body


# --- Component 1: Greek Exposure / GEX cross-check targets ---
def greek_exposure(ticker: str):
    return _get(f"/api/stock/{ticker}/greek-exposure")


def gex_levels(ticker: str):
    return _get(f"/api/stock/{ticker}/gex-levels")


def spot_exposures(ticker: str):
    """Per-minute intraday GEX."""
    return _get(f"/api/stock/{ticker}/spot-exposures")


# --- Component 2: OI ---
def oi_per_strike(ticker: str):
    return _get(f"/api/stock/{ticker}/oi-per-strike")


def oi_change(ticker: str):
    return _get(f"/api/stock/{ticker}/oi-change")


# --- Component 3: Flow with aggressor-side tagging (the missing piece) ---
def flow_per_strike(ticker: str):
    return _get(f"/api/stock/{ticker}/flow-per-strike")


def flow_per_strike_intraday(ticker: str):
    return _get(f"/api/stock/{ticker}/flow-per-strike-intraday")


def flow_per_expiry(ticker: str):
    """Option flow per expiry for the last trading day -- the expiry==date
    row is that day's 0DTE flow (SPY/QQQ/IWM have daily expiries)."""
    return _get(f"/api/stock/{ticker}/flow-per-expiry")


# --- Component 4: Dark pool ---
def darkpool_ticker(ticker: str):
    return _get(f"/api/darkpool/{ticker}")


# --- Component 5: Volatility (VIX/VXV replacement + VRP, never built before) ---
def vix_term_structure():
    return _get("/api/volatility/vix-term-structure")


def vol_term_structure(ticker: str):
    return _get(f"/api/stock/{ticker}/volatility/term-structure")


def variance_risk_premium(ticker: str):
    return _get(f"/api/stock/{ticker}/volatility/variance-risk-premium")


def iv_rank(ticker: str):
    return _get(f"/api/stock/{ticker}/iv-rank")


# --- Component 6: sweep/unusual alerts + short interest (2026-07-04) ---
def option_trades_flow_alerts(**params):
    """Market-wide, rule-based option trade alerts (e.g. is_sweep=True).
    Unlike the other functions here, this hits a market-wide endpoint, not a
    per-ticker one -- pass ticker_symbol=... to scope it."""
    return _get("/api/option-trades/flow-alerts", **params)


def short_interest_float(ticker: str):
    return _get(f"/api/shorts/{ticker}/interest-float/v2")


def short_volume_and_ratio(ticker: str):
    return _get(f"/api/shorts/{ticker}/volume-and-ratio")


if __name__ == "__main__":
    import json

    checks = [
        ("greek_exposure(SPY)", lambda: greek_exposure("SPY")),
        ("gex_levels(SPY)", lambda: gex_levels("SPY")),
        ("oi_per_strike(SPY)", lambda: oi_per_strike("SPY")),
        ("flow_per_strike(SPY)", lambda: flow_per_strike("SPY")),
        ("darkpool_ticker(SPY)", lambda: darkpool_ticker("SPY")),
        ("vix_term_structure()", lambda: vix_term_structure()),
        ("variance_risk_premium(SPY)", lambda: variance_risk_premium("SPY")),
    ]
    for name, fn in checks:
        try:
            code, body = fn()
            preview = json.dumps(body)[:400] if not isinstance(body, str) else body[:400]
            print(f"[{code}] {name}\n  {preview}\n")
        except Exception as e:
            print(f"[ERROR] {name}: {e}\n")
        time.sleep(0.3)
