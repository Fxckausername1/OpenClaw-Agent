"""Read-only SPY/QQQ confluence and low-premium option research cards.

This module is deliberately isolated from every order, scanner, and strategy path. It
turns completed underlying bars and indicative option-chain snapshots into an auditable
manual research screen. It cannot submit an order or modify MR/ORB settings.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")
UTC = timezone.utc
STOCK_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
OPTION_CHAIN_URL = "https://data.alpaca.markets/v1beta1/options/snapshots"
PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
SYMBOLS = ("SPY", "QQQ")
OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

BUDGET = 400.0
PREMIUM_MIN = 0.20
PREMIUM_MAX = 0.30
TARGET_RETURN = 0.25
QUOTE_MAX_SECONDS = 120


def _num(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _session_dt(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), ET)


def _ema(values: list[float], span: int) -> float | None:
    if len(values) < span:
        return None
    alpha = 2.0 / (span + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _sma(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def _bar_rows(rows: list[dict[str, Any]], day: date) -> list[tuple[datetime, dict[str, Any]]]:
    parsed = []
    for row in rows:
        stamp = _parse_dt(row.get("t"))
        if stamp and stamp.astimezone(ET).date() == day:
            parsed.append((stamp.astimezone(ET), row))
    return sorted(parsed, key=lambda item: item[0])


def detect_liquidity_sweeps(
    regular_rows: list[tuple[datetime, dict[str, Any]]],
    levels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect a penetration, reclaim close, and next-bar confirmation.

    This is intentionally a price-action proxy around predeclared levels. It is not
    order-book liquidity, hidden-size detection, or proof of institutional activity.
    """
    events: list[dict[str, Any]] = []
    for level_row in levels:
        level = _num(level_row.get("level"))
        active_after = level_row.get("active_after")
        if level is None:
            continue
        minimum_penetration = max(0.01, abs(level) * 0.0001)
        level_name = str(level_row.get("name") or "").lower()
        bullish_sweep_allowed = level_name.endswith("low")
        bearish_sweep_allowed = level_name.endswith("high")
        for index in range(len(regular_rows) - 1):
            stamp, row = regular_rows[index]
            next_stamp, next_row = regular_rows[index + 1]
            if active_after and stamp < active_after:
                continue
            if next_stamp - stamp > timedelta(minutes=2):
                continue
            low, high = _num(row.get("l")), _num(row.get("h"))
            close, next_close = _num(row.get("c")), _num(next_row.get("c"))
            if None in (low, high, close, next_close):
                continue
            if bullish_sweep_allowed and low <= level - minimum_penetration and close > level and next_close >= level:
                events.append({
                    "direction": "bullish",
                    "level_name": level_row["name"],
                    "level": round(level, 4),
                    "swept_at": _iso(stamp),
                    "confirmed_at": _iso(next_stamp),
                    "penetration_bps": round((level - low) / level * 10000.0, 2),
                    "definition": "traded below level, reclaimed on close, next completed bar held above",
                })
            if bearish_sweep_allowed and high >= level + minimum_penetration and close < level and next_close <= level:
                events.append({
                    "direction": "bearish",
                    "level_name": level_row["name"],
                    "level": round(level, 4),
                    "swept_at": _iso(stamp),
                    "confirmed_at": _iso(next_stamp),
                    "penetration_bps": round((high - level) / level * 10000.0, 2),
                    "definition": "traded above level, rejected on close, next completed bar held below",
                })
    events.sort(key=lambda row: (row.get("confirmed_at") or "", row.get("level_name") or ""))
    return events


def technical_summary(
    symbol: str,
    intraday_rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    day: date,
    opening: dict[str, Any],
) -> dict[str, Any]:
    parsed = _bar_rows(intraday_rows, day)
    premarket = [
        row for stamp, row in parsed
        if _session_dt(day, 4, 0) <= stamp < _session_dt(day, 9, 30)
    ]
    regular = [
        (stamp, row) for stamp, row in parsed
        if _session_dt(day, 9, 30) <= stamp < _session_dt(day, 9, 50)
    ]
    closes = [_num(row.get("c")) for _, row in regular]
    closes = [value for value in closes if value is not None]
    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)
    last = _num(opening.get("last"))
    vwap = _num(opening.get("vwap"))

    prior = []
    for row in daily_rows:
        stamp = _parse_dt(row.get("t"))
        if stamp and stamp.astimezone(ET).date() < day and _num(row.get("c")) is not None:
            prior.append((stamp, row))
    prior.sort(key=lambda item: item[0])
    prior_closes = [_num(row.get("c")) for _, row in prior]
    prior_closes = [value for value in prior_closes if value is not None]
    prior_bar = prior[-1][1] if prior else {}
    sma20, sma50, sma200 = (
        _sma(prior_closes, 20),
        _sma(prior_closes, 50),
        _sma(prior_closes, 200),
    )
    prior_high, prior_low = _num(prior_bar.get("h")), _num(prior_bar.get("l"))
    premarket_usable = len(premarket) >= 10
    premarket_high = (
        max((_num(row.get("h")) for row in premarket if _num(row.get("h")) is not None), default=None)
        if premarket_usable else None
    )
    premarket_low = (
        min((_num(row.get("l")) for row in premarket if _num(row.get("l")) is not None), default=None)
        if premarket_usable else None
    )

    levels = [
        {"name": "prior-day high", "level": prior_high, "active_after": _session_dt(day, 9, 30)},
        {"name": "prior-day low", "level": prior_low, "active_after": _session_dt(day, 9, 30)},
        {"name": "premarket high", "level": premarket_high, "active_after": _session_dt(day, 9, 30)},
        {"name": "premarket low", "level": premarket_low, "active_after": _session_dt(day, 9, 30)},
        {"name": "opening-range high", "level": opening.get("or_high"), "active_after": _session_dt(day, 9, 45)},
        {"name": "opening-range low", "level": opening.get("or_low"), "active_after": _session_dt(day, 9, 45)},
    ]
    sweeps = detect_liquidity_sweeps(regular, levels)
    latest_sweep = sweeps[-1] if sweeps else None

    votes: list[dict[str, str]] = []
    if last is not None and vwap is not None:
        direction = "bullish" if last > vwap else "bearish" if last < vwap else "neutral"
        votes.append({"signal": "price vs VWAP", "direction": direction})
    else:
        votes.append({"signal": "price vs VWAP", "direction": "unavailable"})

    if last is not None and ema9 is not None and ema20 is not None:
        direction = (
            "bullish" if last > ema9 > ema20
            else "bearish" if last < ema9 < ema20
            else "neutral"
        )
        votes.append({"signal": "1m EMA 9/20", "direction": direction})
    else:
        votes.append({"signal": "1m EMA 9/20", "direction": "unavailable"})

    if last is not None and sma20 is not None and sma50 is not None:
        direction = (
            "bullish" if last > sma20 > sma50
            else "bearish" if last < sma20 < sma50
            else "neutral"
        )
        votes.append({"signal": "prior-session SMA 20/50", "direction": direction})
    else:
        votes.append({"signal": "prior-session SMA 20/50", "direction": "unavailable"})

    votes.append({
        "signal": "confirmed sweep/reclaim proxy",
        "direction": latest_sweep["direction"] if latest_sweep else "neutral",
    })
    location = opening.get("location")
    votes.append({
        "signal": "opening-range location",
        "direction": "bullish" if location == "above_range" else "bearish" if location == "below_range" else "neutral",
    })

    bullish = sum(1 for vote in votes if vote["direction"] == "bullish")
    bearish = sum(1 for vote in votes if vote["direction"] == "bearish")
    if bullish >= 3 and bullish >= bearish + 2:
        base_direction = "bullish"
    elif bearish >= 3 and bearish >= bullish + 2:
        base_direction = "bearish"
    else:
        base_direction = "mixed"

    call_levels = [opening.get("or_high"), vwap, ema9]
    put_levels = [opening.get("or_low"), vwap, ema9]
    call_values = [_num(value) for value in call_levels if _num(value) is not None]
    put_values = [_num(value) for value in put_levels if _num(value) is not None]
    call_trigger = max(call_values) if call_values else None
    put_trigger = min(put_values) if put_values else None
    if base_direction == "bullish":
        invalidation = f"completed 1m close below both VWAP {vwap:.2f} and EMA20 {ema20:.2f}" if vwap is not None and ema20 is not None else "technical levels unavailable"
    elif base_direction == "bearish":
        invalidation = f"completed 1m close above both VWAP {vwap:.2f} and EMA20 {ema20:.2f}" if vwap is not None and ema20 is not None else "technical levels unavailable"
    else:
        invalidation = "no directional thesis exists to invalidate"

    return {
        "symbol": symbol,
        "as_of": opening.get("cutoff"),
        "last": last,
        "ema9_1m": round(ema9, 4) if ema9 is not None else None,
        "ema20_1m": round(ema20, 4) if ema20 is not None else None,
        "sma20_prior": round(sma20, 4) if sma20 is not None else None,
        "sma50_prior": round(sma50, 4) if sma50 is not None else None,
        "sma200_prior": round(sma200, 4) if sma200 is not None else None,
        "prior_day_high": prior_high,
        "prior_day_low": prior_low,
        "premarket_high": premarket_high,
        "premarket_low": premarket_low,
        "premarket_quality": "USABLE" if premarket_usable else "INSUFFICIENT",
        "opening_range_high": _num(opening.get("or_high")),
        "opening_range_low": _num(opening.get("or_low")),
        "sweeps": sweeps,
        "latest_sweep": latest_sweep,
        "votes": votes,
        "bullish_votes": bullish,
        "bearish_votes": bearish,
        "base_direction": base_direction,
        "call_activation": round(call_trigger, 4) if call_trigger is not None else None,
        "put_activation": round(put_trigger, 4) if put_trigger is not None else None,
        "invalidation": invalidation,
        "daily_history_sessions": len(prior_closes),
        "premarket_bars": len(premarket),
        "regular_bars": len(regular),
    }


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _bs_price(spot: float, strike: float, years: float, rate: float, sigma: float, is_call: bool) -> float:
    if years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    discount = math.exp(-rate * years)
    if is_call:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _implied_vol(price: float, spot: float, strike: float, years: float, rate: float, is_call: bool) -> float | None:
    intrinsic = max(spot - strike * math.exp(-rate * years), 0.0) if is_call else max(strike * math.exp(-rate * years) - spot, 0.0)
    if price <= 0 or price < intrinsic - 1e-6 or years <= 0:
        return None
    low, high = 0.0001, 5.0
    for _ in range(70):
        middle = (low + high) / 2.0
        if _bs_price(spot, strike, years, rate, middle, is_call) < price:
            low = middle
        else:
            high = middle
    result = (low + high) / 2.0
    return result if 0.0002 < result < 4.99 else None


def _greeks(spot: float, strike: float, years: float, rate: float, sigma: float, is_call: bool) -> dict[str, float]:
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    pdf = _norm_pdf(d1)
    discount = math.exp(-rate * years)
    call_delta = _norm_cdf(d1)
    delta = call_delta if is_call else call_delta - 1.0
    gamma = pdf / (spot * sigma * root_t)
    theta_year = (
        -spot * pdf * sigma / (2.0 * root_t) - rate * strike * discount * _norm_cdf(d2)
        if is_call
        else -spot * pdf * sigma / (2.0 * root_t) + rate * strike * discount * _norm_cdf(-d2)
    )
    return {"delta": delta, "gamma": gamma, "theta": theta_year / 365.0}


def _parse_option_symbol(symbol: str) -> dict[str, Any] | None:
    match = OPTION_SYMBOL_RE.match(symbol)
    if not match:
        return None
    root, expiry, cp, strike = match.groups()
    return {
        "root": root,
        "expiration": datetime.strptime(expiry, "%y%m%d").date(),
        "is_call": cp == "C",
        "strike": int(strike) / 1000.0,
    }


def _target_underlying(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    sigma: float,
    is_call: bool,
    target_price: float,
) -> tuple[float | None, float | None]:
    if is_call:
        low, high = spot, spot * 1.08
        if _bs_price(high, strike, years, rate, sigma, True) < target_price:
            return None, None
        for _ in range(60):
            middle = (low + high) / 2.0
            if _bs_price(middle, strike, years, rate, sigma, True) < target_price:
                low = middle
            else:
                high = middle
    else:
        low, high = spot * 0.92, spot
        if _bs_price(low, strike, years, rate, sigma, False) < target_price:
            return None, None
        for _ in range(60):
            middle = (low + high) / 2.0
            if _bs_price(middle, strike, years, rate, sigma, False) >= target_price:
                low = middle
            else:
                high = middle
    target_spot = (low + high) / 2.0
    return target_spot, (target_spot / spot - 1.0) * 100.0


def option_candidate(
    symbol: str,
    snapshot: dict[str, Any],
    metadata: dict[str, Any],
    spot: float,
    observed: datetime,
) -> dict[str, Any] | None:
    parsed = _parse_option_symbol(symbol)
    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    bid, ask = _num(quote.get("bp")), _num(quote.get("ap"))
    if parsed is None or bid is None or ask is None or bid <= 0 or ask <= bid:
        return None
    if not (PREMIUM_MIN <= ask <= PREMIUM_MAX):
        return None

    mid = (bid + ask) / 2.0
    spread = ask - bid
    spread_pct = spread / mid if mid else None
    quote_time = _parse_dt(quote.get("t"))
    quote_age = (observed.astimezone(UTC) - quote_time.astimezone(UTC)).total_seconds() if quote_time else None
    expiry_at = datetime.combine(parsed["expiration"], time(16, 0), ET)
    seconds_left = max((expiry_at - observed.astimezone(ET)).total_seconds(), 60.0)
    years = seconds_left / (365.0 * 24.0 * 3600.0)
    rate = 0.05

    iv = _num(snapshot.get("impliedVolatility") or snapshot.get("implied_volatility"))
    greeks = snapshot.get("greeks") or {}
    delta = _num(greeks.get("delta"))
    gamma = _num(greeks.get("gamma"))
    theta = _num(greeks.get("theta"))
    greek_source = "Alpaca snapshot"
    if iv is None:
        iv = _implied_vol(mid, spot, parsed["strike"], years, rate, parsed["is_call"])
    if iv is not None and None in (delta, gamma, theta):
        calculated = _greeks(spot, parsed["strike"], years, rate, iv, parsed["is_call"])
        delta = calculated["delta"]
        gamma = calculated["gamma"]
        theta = calculated["theta"]
        greek_source = "Black-Scholes from indicative midpoint; constant-rate/IV approximation"

    target_premium = math.ceil((ask * (1.0 + TARGET_RETURN) - 1e-9) * 100.0) / 100.0
    contracts = math.floor(BUDGET / (ask * 100.0))
    estimated_cost = contracts * ask * 100.0
    gross_target = contracts * (target_premium - ask) * 100.0
    round_trip_spread = contracts * spread * 100.0
    friction_ratio = round_trip_spread / gross_target if gross_target > 0 else None
    target_spot, move_pct = (None, None)
    if iv is not None:
        target_spot, move_pct = _target_underlying(
            spot, parsed["strike"], years, rate, iv, parsed["is_call"], target_premium
        )

    reject_reasons = []
    if quote_age is None or quote_age < -5 or quote_age > QUOTE_MAX_SECONDS:
        reject_reasons.append("quote stale or timestamp unavailable")
    if spread > 0.02 + 1e-9:
        reject_reasons.append("spread wider than $0.02 research ceiling")
    if spread_pct is None or spread_pct > 0.10 + 1e-9:
        reject_reasons.append("spread exceeds 10% of midpoint")
    if delta is None:
        reject_reasons.append("delta unavailable")
    elif abs(delta) < 0.10:
        reject_reasons.append("absolute delta below 0.10")
    if friction_ratio is None or friction_ratio > 0.35:
        reject_reasons.append("estimated full-spread friction exceeds 35% of gross target")
    if contracts < 1:
        reject_reasons.append("no contract fits inside budget")

    return {
        "contract": symbol,
        "type": "call" if parsed["is_call"] else "put",
        "expiration": parsed["expiration"].isoformat(),
        "strike": parsed["strike"],
        "quote_timestamp": _iso(quote_time),
        "quote_age_seconds": round(quote_age, 1) if quote_age is not None else None,
        "feed": "indicative",
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "mid": round(mid, 4),
        "bid_size": _num(quote.get("bs")),
        "ask_size": _num(quote.get("as")),
        "spread": round(spread, 4),
        "spread_pct": round(spread_pct * 100.0, 2) if spread_pct is not None else None,
        "open_interest_t1": _num(metadata.get("open_interest")),
        "iv": round(iv, 5) if iv is not None else None,
        "delta": round(delta, 5) if delta is not None else None,
        "gamma": round(gamma, 6) if gamma is not None else None,
        "theta_per_day": round(theta, 5) if theta is not None else None,
        "greek_source": greek_source,
        "budget": BUDGET,
        "contracts": contracts,
        "estimated_debit": round(estimated_cost, 2),
        "target_premium": round(target_premium, 2),
        "gross_target_profit": round(gross_target, 2),
        "estimated_full_spread_cost": round(round_trip_spread, 2),
        "friction_ratio_pct": round(friction_ratio * 100.0, 1) if friction_ratio is not None else None,
        "estimated_target_underlying": round(target_spot, 4) if target_spot is not None else None,
        "estimated_required_underlying_move_pct": round(move_pct, 3) if move_pct is not None else None,
        "screen": "SCREEN PASS - VERIFY LIVE" if not reject_reasons else "REJECT",
        "reject_reasons": reject_reasons,
        "warning": "Indicative quote is not executable NBBO. Recheck the live broker quote immediately before any manual order.",
    }


def select_candidate(
    snapshots: dict[str, Any],
    metadata: dict[str, dict[str, Any]],
    spot: float,
    observed: datetime,
) -> dict[str, Any] | None:
    candidates = []
    for symbol, snapshot in snapshots.items():
        candidate = option_candidate(symbol, snapshot, metadata.get(symbol, {}), spot, observed)
        if candidate:
            candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates, key=lambda row: (
        row["screen"] != "SCREEN PASS - VERIFY LIVE",
        row.get("friction_ratio_pct") if row.get("friction_ratio_pct") is not None else math.inf,
        -(abs(row.get("delta")) if row.get("delta") is not None else 0.0),
        row.get("spread", math.inf),
    ))


def _fetch_daily(headers: dict[str, str], day: date) -> tuple[dict[str, list[dict[str, Any]]], str]:
    response = requests.get(
        STOCK_BARS_URL,
        headers=headers,
        params={
            "symbols": ",".join(SYMBOLS),
            "timeframe": "1Day",
            "start": (day - timedelta(days=420)).isoformat(),
            "end": day.isoformat(),
            "feed": "iex",
            "adjustment": "raw",
            "limit": 10000,
            "sort": "asc",
        },
        timeout=25,
    )
    response.raise_for_status()
    return response.json().get("bars") or {}, response.headers.get("X-Request-ID", "")


def _fetch_premarket(
    headers: dict[str, str],
    day: date,
) -> tuple[dict[str, list[dict[str, Any]]], str]:
    """Fetch the completed premarket window from consolidated SIP.

    At the 09:52 build, the 09:30 end of this range is more than 15 minutes old,
    which keeps it inside the free historical-data delay while avoiding sparse IEX
    extrema as liquidity references.
    """
    response = requests.get(
        STOCK_BARS_URL,
        headers=headers,
        params={
            "symbols": ",".join(SYMBOLS),
            "timeframe": "1Min",
            "start": _session_dt(day, 4, 0).astimezone(UTC).isoformat(),
            "end": _session_dt(day, 9, 30).astimezone(UTC).isoformat(),
            "feed": "sip",
            "adjustment": "raw",
            "limit": 10000,
            "sort": "asc",
        },
        timeout=25,
    )
    response.raise_for_status()
    return response.json().get("bars") or {}, response.headers.get("X-Request-ID", "")


def _fetch_option_chain(
    headers: dict[str, str],
    symbol: str,
    day: date,
    option_type: str,
    spot: float,
) -> tuple[dict[str, Any], str]:
    response = requests.get(
        f"{OPTION_CHAIN_URL}/{symbol}",
        headers=headers,
        params={
            "feed": "indicative",
            "expiration_date": day.isoformat(),
            "type": option_type,
            "strike_price_gte": f"{spot * 0.97:.2f}",
            "strike_price_lte": f"{spot * 1.03:.2f}",
            "limit": 1000,
        },
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("snapshots") or {}, response.headers.get("X-Request-ID", "")


def _fetch_contract_metadata(
    headers: dict[str, str],
    symbol: str,
    day: date,
    spot: float,
) -> dict[str, dict[str, Any]]:
    response = requests.get(
        f"{PAPER_TRADING_URL}/v2/options/contracts",
        headers=headers,
        params={
            "underlying_symbols": symbol,
            "expiration_date": day.isoformat(),
            "strike_price_gte": f"{spot * 0.97:.2f}",
            "strike_price_lte": f"{spot * 1.03:.2f}",
            "status": "active",
            "limit": 10000,
        },
        timeout=25,
    )
    response.raise_for_status()
    return {
        row["symbol"]: row
        for row in response.json().get("option_contracts") or []
        if row.get("symbol")
    }


def _fixture_daily(day: date, base: float) -> list[dict[str, Any]]:
    rows = []
    for index in range(230):
        stamp = datetime.combine(day - timedelta(days=330 - index), time(), UTC)
        close = base * 0.84 + index * base * 0.0007
        rows.append({
            "t": stamp.isoformat(),
            "o": close - 0.3,
            "h": close + 0.7,
            "l": close - 0.7,
            "c": close,
            "v": 1_000_000,
        })
    return rows


def _fixture_chain(symbol: str, day: date, option_type: str, spot: float, observed: datetime) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    strike = round(spot + (2.0 if option_type == "call" else -2.0))
    cp = "C" if option_type == "call" else "P"
    contract = f"{symbol}{day.strftime('%y%m%d')}{cp}{int(strike * 1000):08d}"
    snapshot = {
        "latestQuote": {"bp": 0.23, "ap": 0.24, "bs": 220, "as": 180, "t": _iso(observed)},
        "impliedVolatility": 0.22,
        "greeks": {
            "delta": 0.22 if option_type == "call" else -0.22,
            "gamma": 0.045,
            "theta": -0.035,
        },
    }
    return {contract: snapshot}, {contract: {"open_interest": 2500}}


def build_manual_options_layer(
    root: Any,
    day: date,
    observed: datetime,
    indices: dict[str, dict[str, Any]],
    intraday: dict[str, list[dict[str, Any]]],
    core_quality: str,
    fixture: bool = False,
) -> dict[str, Any]:
    headers = {
        "APCA-API-KEY-ID": (root / "credentials" / "alpaca_key.txt").read_text().strip(),
        "APCA-API-SECRET-KEY": (root / "credentials" / "alpaca_secret.txt").read_text().strip(),
    } if not fixture else {}
    errors: list[str] = []
    if fixture:
        daily = {"SPY": _fixture_daily(day, 600.0), "QQQ": _fixture_daily(day, 535.0)}
        daily_request_id = "fixture"
        context_intraday = intraday
        premarket_request_id = "fixture"
    else:
        try:
            daily, daily_request_id = _fetch_daily(headers, day)
        except Exception as exc:
            daily, daily_request_id = {}, f"unavailable:{type(exc).__name__}"
            errors.append(f"daily technical context unavailable: {type(exc).__name__}")
        try:
            sip_premarket, premarket_request_id = _fetch_premarket(headers, day)
        except Exception as exc:
            sip_premarket, premarket_request_id = {}, f"unavailable:{type(exc).__name__}"
            errors.append(f"consolidated premarket unavailable: {type(exc).__name__}")
        context_intraday = {}
        for symbol in SYMBOLS:
            regular_only = []
            for row in intraday.get(symbol) or []:
                stamp = _parse_dt(row.get("t"))
                if stamp and stamp.astimezone(ET) >= _session_dt(day, 9, 30):
                    regular_only.append(row)
            context_intraday[symbol] = list(sip_premarket.get(symbol) or []) + regular_only

    technicals = {
        symbol: technical_summary(
            symbol,
            context_intraday.get(symbol) or [],
            daily.get(symbol) or [],
            day,
            indices[symbol]["bars"],
        )
        for symbol in SYMBOLS
    }
    directions = {technicals[symbol]["base_direction"] for symbol in SYMBOLS}
    aligned = next(iter(directions)) if len(directions) == 1 else "mixed"
    if core_quality != "FRESH":
        state, option_type = "WAIT - CORE DATA NOT FRESH", None
    elif aligned == "bullish":
        state, option_type = "CALL WATCH", "call"
    elif aligned == "bearish":
        state, option_type = "PUT WATCH", "put"
    else:
        state, option_type = "WAIT - SPY/QQQ NOT ALIGNED", None

    cards = {}
    option_request_ids = []
    for symbol in SYMBOLS:
        technical = technicals[symbol]
        candidate = None
        candidate_note = "No contract is surfaced while the underlying state is WAIT."
        if option_type:
            spot = _num(indices[symbol]["bars"].get("last"))
            if spot is None:
                errors.append(f"{symbol} option screen skipped: underlying spot unavailable")
                candidate_note = "Underlying spot unavailable."
            else:
                try:
                    if fixture:
                        snapshots, metadata = _fixture_chain(symbol, day, option_type, spot, observed)
                        request_id = "fixture"
                    else:
                        snapshots, request_id = _fetch_option_chain(headers, symbol, day, option_type, spot)
                        metadata = _fetch_contract_metadata(headers, symbol, day, spot)
                    option_request_ids.append(f"{symbol}:{request_id or 'not returned'}")
                    candidate = select_candidate(snapshots, metadata, spot, observed)
                    candidate_note = (
                        "Best $0.20-$0.30 ask candidate under fixed research filters."
                        if candidate else
                        "No valid $0.20-$0.30 ask candidate was present in the indicative snapshot."
                    )
                except Exception as exc:
                    errors.append(f"{symbol} option screen unavailable: {type(exc).__name__}")
                    candidate_note = f"Option screen unavailable: {type(exc).__name__}."
        cards[symbol] = {
            "symbol": symbol,
            "state": state,
            "technical": technical,
            "candidate": candidate,
            "candidate_note": candidate_note,
        }

    technical_complete = all(
        technicals[symbol]["daily_history_sessions"] >= 200
        and technicals[symbol]["regular_bars"] >= 20
        for symbol in SYMBOLS
    )
    technical_quality = "FRESH" if technical_complete and not daily_request_id.startswith("unavailable") else "DEGRADED"
    premarket_quality = (
        "USABLE" if all(technicals[symbol]["premarket_quality"] == "USABLE" for symbol in SYMBOLS)
        else "INSUFFICIENT"
    )
    option_quality = (
        "NOT REQUESTED - WAIT"
        if not option_type
        else "AVAILABLE - INDICATIVE"
        if all(cards[symbol]["candidate"] for symbol in SYMBOLS)
        else "PARTIAL / UNAVAILABLE"
    )
    return {
        "schema_version": "manual-options-card-1.0",
        "purpose": "Manual SPY/QQQ options research only; isolated from MR/ORB and all order paths.",
        "as_of": _iso(observed),
        "budget": BUDGET,
        "premium_band": [PREMIUM_MIN, PREMIUM_MAX],
        "target_return_pct": TARGET_RETURN * 100.0,
        "aligned_underlying_direction": aligned,
        "market_state": state,
        "technical_quality": technical_quality,
        "premarket_quality": premarket_quality,
        "option_quote_quality": option_quality,
        "cards": cards,
        "source_health": {
            "daily_bars_request_id": daily_request_id,
            "premarket_sip_request_id": premarket_request_id,
            "option_request_ids": option_request_ids,
            "errors": errors,
        },
        "rules": {
            "underlying_watch": "At least 3 of 5 votes in one direction, a two-vote margin, and SPY/QQQ alignment.",
            "sweep_proxy": "Low: penetrate then reclaim and next-bar hold for bullish sell-side sweep. High: penetrate then reject and next-bar hold for bearish buy-side sweep.",
            "premarket_gate": "Premarket levels use completed consolidated SIP history and require at least 10 one-minute bars per index; otherwise unavailable.",
            "contract_screen": "Ask $0.20-$0.30; quote <=120s; spread <=$0.02 and <=10% of mid; |delta| >=0.10; estimated full-spread friction <=35% of gross target.",
            "target_rounding": "25% target rounded up to the next whole premium cent.",
            "quote_boundary": "Alpaca indicative feed is not executable NBBO; verify live at broker immediately before entry.",
            "risk_boundary": "No stop is assumed. A +25% target alone does not define expectancy or maximum loss.",
        },
    }
