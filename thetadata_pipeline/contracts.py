"""SPY/QQQ 'Always on' active-universe builder -- TD-STD Section 4.

Deliberately narrow scope for TD-1..TD-4 (heff's explicit ask + TD-STD's own
"Scope discipline" box: "Do not start with the full 236-symbol GEX universe.
Begin with SPY and QQQ, prove sequence integrity and feature quality, then
expand by measured need."). This module only ever builds a universe for
symbols in ALWAYS_ON_SYMBOLS.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .client import ThetaDataUnavailable, bounded_call, get_client
from .schemas import contract_id, normalize_right

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
DATA.mkdir(parents=True, exist_ok=True)
SUBSCRIPTION_SET_PATH = DATA / "subscription_set.json"

ALWAYS_ON_SYMBOLS = ("SPY", "QQQ")
NUM_FORWARD_EXPIRATIONS = 3  # 0DTE (if listed today) + next 2, per Section 4
STRIKE_WINDOW_PCT = 0.06  # +/- 6% of spot, near-money per Section 4's "Always on" tier

STOCK_TRADES_LATEST_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest"

logger = logging.getLogger("thetadata_pkg.contracts")


def _atomic_write_json(path: Path, payload) -> None:
    """Same pattern as wide_universe.py's _atomic_write_json: temp file in
    the same directory + os.replace, so a concurrent reader never sees a
    partially-written file."""
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def _alpaca_headers() -> dict:
    key = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
    sec = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


def get_spot_price(symbol: str) -> Optional[float]:
    """Latest trade price via Alpaca (same credential/header convention as
    live_gex.py/manual_options_brief.py). Returns None rather than raising
    so a spot-price hiccup degrades to DATA BLOCKED instead of crashing the
    whole collector cycle."""
    try:
        resp = requests.get(
            STOCK_TRADES_LATEST_URL.format(symbol=symbol),
            headers=_alpaca_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["trade"]["p"])
    except Exception as exc:
        logger.warning("spot price fetch failed for %s: %s", symbol, exc)
        return None


def active_expirations(symbol: str, today: dt.date) -> list[dt.date]:
    """0DTE (if today is itself a listed expiration) plus the next
    NUM_FORWARD_EXPIRATIONS-1 future listed expirations, per Section 4's
    'Always on' tier (0DTE, next two expirations)."""
    client = get_client()
    df = bounded_call(client.option_list_expirations, symbol=symbol)
    all_exp = sorted({dt.date.fromisoformat(str(v)[:10]) for v in df["expiration"]})
    future = [d for d in all_exp if d >= today]
    return future[:NUM_FORWARD_EXPIRATIONS]


def strike_window(symbol: str, expiration: dt.date, spot: float, pct: float = STRIKE_WINDOW_PCT) -> list[float]:
    """Listed strikes for `expiration` within +/- pct of spot. Falls back to
    the full listed strike set if spot is unavailable (better to over-pull a
    liquid SPY/QQQ near-dated chain than silently subscribe to nothing)."""
    client = get_client()
    df = bounded_call(client.option_list_strikes, symbol=symbol, expiration=expiration)
    all_strikes = sorted(float(v) for v in df["strike"])
    if spot is None:
        return all_strikes
    lo, hi = spot * (1 - pct), spot * (1 + pct)
    windowed = [s for s in all_strikes if lo <= s <= hi]
    return windowed or all_strikes


def build_active_universe(symbol: str, today: Optional[dt.date] = None) -> dict:
    """Full 'Always on' contract set for one symbol: every (expiration,
    strike, right) triple in the near-money window across the 0DTE+2
    expirations tier. Returns a dict also used as the persisted audit
    record (Section 4: 'Persist the exact subscription set so coverage can
    be audited later')."""
    today = today or dt.datetime.now(ET).date()
    spot = get_spot_price(symbol)
    expirations = active_expirations(symbol, today)
    contracts = []
    for exp in expirations:
        strikes = strike_window(symbol, exp, spot)
        for strike in strikes:
            for right in ("C", "P"):
                contracts.append({
                    "contract_id": contract_id(symbol, exp, strike, right),
                    "underlying": symbol,
                    "expiration": exp.isoformat(),
                    "strike": strike,
                    "right": right,
                })
    return {
        "symbol": symbol,
        "as_of_date": today.isoformat(),
        "built_at": dt.datetime.now(ET).isoformat(),
        "spot": spot,
        "expirations": [e.isoformat() for e in expirations],
        "strike_window_pct": STRIKE_WINDOW_PCT,
        "contract_count": len(contracts),
        "contracts": contracts,
    }


def load_subscription_set() -> dict:
    if not SUBSCRIPTION_SET_PATH.exists():
        return {}
    try:
        return json.loads(SUBSCRIPTION_SET_PATH.read_text())
    except Exception:
        return {}


def ensure_active_universe(symbol: str, now: Optional[dt.datetime] = None, force: bool = False) -> dict:
    """Cached per-symbol universe, rebuilt once/day at/after 09:20 ET (Section
    4: 'Rebuild the active contract set at 09:20...') or when spot has moved
    outside the previously-built window (the 'and when spot moves through a
    strike-window boundary' half of the same rule, approximated once per
    collector cycle rather than continuously since this is a cron-driven
    design, not a persistent stream -- see collector.py's module docstring
    for why)."""
    now = now or dt.datetime.now(ET)
    today = now.date()
    all_sets = load_subscription_set()
    existing = all_sets.get(symbol)

    rebuild_time_ok = now.time() >= dt.time(9, 20)
    needs_rebuild = force or existing is None or existing.get("as_of_date") != today.isoformat()

    if not needs_rebuild and existing is not None:
        spot = get_spot_price(symbol)
        if spot is not None and existing.get("expirations"):
            lo = spot * (1 - STRIKE_WINDOW_PCT)
            hi = spot * (1 + STRIKE_WINDOW_PCT)
            strikes = {c["strike"] for c in existing.get("contracts", [])}
            if strikes and (min(strikes) > lo or max(strikes) < hi):
                needs_rebuild = True

    if needs_rebuild and (rebuild_time_ok or existing is None):
        built = build_active_universe(symbol, today)
        all_sets[symbol] = built
        _atomic_write_json(SUBSCRIPTION_SET_PATH, all_sets)
        return built

    return existing or {"symbol": symbol, "contracts": [], "expirations": [], "contract_count": 0}
