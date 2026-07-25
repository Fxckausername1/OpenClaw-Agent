#!/usr/bin/env python3
"""Build, publish, update, and score the daily BOT_NEXUS Catalyst Brief.

The brief is intentionally deterministic. It turns existing bot observations into a
fixed SPY/QQQ microstructure playbook and never invents facts or citations. Research
output is advisory and cannot place orders or modify strategy settings.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape

from manual_options_brief import build_manual_options_layer


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT_PDF = ROOT / "output" / "pdf"
ARCHIVE = DATA / "catalyst_briefs"
PUBLISH_REPO = Path.home() / "trading-dashboard-snapshot"
PUBLISH_ROOT = PUBLISH_REPO / "briefs"
PUBLISH_LOCK = Path("/tmp/trading_dashboard_snapshot_repo.lock")
ET = ZoneInfo("America/New_York")
UTC = timezone.utc

MONTHLY_GEX_MAX_MIN = 45
ZERO_DTE_GEX_MAX_MIN = 6
CATALYST_MAX_HOURS = 30
CALENDAR_MAX_HOURS = 12
MACRO_NEWS_MAX_MIN = 180

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
CROSS_ASSETS = ("TLT", "UUP", "USO", "IWM", "SOXX", "SPY", "QQQ")
SOURCE_METHODS = {
    "monthly_gex": "Alpaca options chain -> bot GEX engine, approximately 30 DTE",
    "zero_dte_gex": "Alpaca same-day options chain -> bot GEX engine",
    "alpaca_bars": "Alpaca IEX one-minute equity bars",
    "sector_rotation": "Alpaca daily bars; sector ETF / SPY relative rotation",
    "catalyst": "SEC EDGAR 8-K and openFDA primary-source event archive",
    "calendar": "U.S. BLS official release calendar (ICS); coverage intentionally disclosed",
    "macro_news": "Finnhub and Yahoo Finance discovery plus official Federal Reserve RSS",
    "cross_asset": "Alpaca IEX bars for rates, dollar, oil, breadth, and semiconductor proxies",
    "prior_brief": "Prior immutable Catalyst Brief and fixed post-close score",
    "technical_context": "Alpaca live IEX regular-session bars, delayed consolidated SIP premarket bars, and prior-session daily bars; deterministic EMA/SMA and sweep/reclaim proxy",
    "option_chain": "Alpaca free indicative option chain plus T-1 contract open interest; research snapshot, not executable NBBO",
}


class BriefError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def age_minutes(generated_at: Any, now: datetime) -> float | None:
    dt = parse_dt(generated_at)
    if not dt:
        return None
    return max(0.0, (now - dt.astimezone(UTC)).total_seconds() / 60.0)


def freshness(generated_at: Any, max_minutes: float, now: datetime) -> dict[str, Any]:
    age = age_minutes(generated_at, now)
    return {
        "generated_at": generated_at,
        "age_minutes": round(age, 2) if age is not None else None,
        "max_minutes": max_minutes,
        "fresh": age is not None and age <= max_minutes,
    }


def pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def num(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def alpaca_headers() -> dict[str, str]:
    key = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
    secret = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def session_dt(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), ET)


def fetch_session_bars(day: date, end_et: datetime) -> tuple[dict[str, list[dict[str, Any]]], str]:
    start = session_dt(day, 4, 0)
    params = {
        "symbols": "SPY,QQQ",
        "timeframe": "1Min",
        "start": start.astimezone(UTC).isoformat(),
        "end": end_et.astimezone(UTC).isoformat(),
        "feed": "iex",
        "adjustment": "raw",
        "limit": 10000,
        "sort": "asc",
    }
    response = requests.get(ALPACA_DATA_URL, headers=alpaca_headers(), params=params, timeout=25)
    response.raise_for_status()
    payload = response.json()
    return payload.get("bars") or {}, response.headers.get("X-Request-ID", "")


def fetch_cross_asset_snapshot(day: date, end_et: datetime) -> tuple[dict[str, dict[str, Any]], str]:
    """Return previous-close and opening-session moves for macro confirmation ETFs."""
    symbols = ",".join(CROSS_ASSETS)
    daily_params = {
        "symbols": symbols,
        "timeframe": "1Day",
        "start": datetime.combine(day - timedelta(days=10), time(), ET).astimezone(UTC).isoformat(),
        "end": datetime.combine(day, time(), ET).astimezone(UTC).isoformat(),
        "feed": "iex",
        "adjustment": "raw",
        "limit": 1000,
        "sort": "asc",
    }
    minute_params = {
        "symbols": symbols,
        "timeframe": "1Min",
        "start": session_dt(day, 9, 30).astimezone(UTC).isoformat(),
        "end": end_et.astimezone(UTC).isoformat(),
        "feed": "iex",
        "adjustment": "raw",
        "limit": 10000,
        "sort": "asc",
    }
    daily_response = requests.get(ALPACA_DATA_URL, headers=alpaca_headers(), params=daily_params, timeout=25)
    daily_response.raise_for_status()
    minute_response = requests.get(ALPACA_DATA_URL, headers=alpaca_headers(), params=minute_params, timeout=25)
    minute_response.raise_for_status()
    daily = daily_response.json().get("bars") or {}
    minute = minute_response.json().get("bars") or {}
    out = {}
    for symbol in CROSS_ASSETS:
        prior_rows = daily.get(symbol) or []
        today_rows = minute.get(symbol) or []
        previous_close = num(prior_rows[-1].get("c")) if prior_rows else None
        open_price = num(today_rows[0].get("o")) if today_rows else None
        last = num(today_rows[-1].get("c")) if today_rows else None
        change_pct = ((last / previous_close) - 1) * 100 if last and previous_close else None
        session_move_pct = ((last / open_price) - 1) * 100 if last and open_price else None
        out[symbol] = {
            "previous_close": previous_close,
            "open": open_price,
            "last": last,
            "change_pct": round(change_pct, 3) if change_pct is not None else None,
            "session_move_pct": round(session_move_pct, 3) if session_move_pct is not None else None,
            "bars": len(today_rows),
        }
    request_ids = ",".join(filter(None, (
        daily_response.headers.get("X-Request-ID", ""),
        minute_response.headers.get("X-Request-ID", ""),
    )))
    return out, request_ids


def fixture_bars(day: date) -> dict[str, list[dict[str, Any]]]:
    """Synthetic bars used only by tests and visual preview fallback."""
    out: dict[str, list[dict[str, Any]]] = {"SPY": [], "QQQ": []}
    for symbol, base, drift in (("SPY", 600.0, 0.06), ("QQQ", 535.0, 0.10)):
        for i in range(25):
            t = session_dt(day, 9, 30) + timedelta(minutes=i)
            o = base + drift * i
            c = o + drift * 0.6
            out[symbol].append({
                "t": t.astimezone(UTC).isoformat(),
                "o": o,
                "h": c + 0.12,
                "l": o - 0.10,
                "c": c,
                "v": 10000 + i * 400,
                "vw": (o + c) / 2,
            })
    return out


def summarize_bars(rows: list[dict[str, Any]], day: date) -> dict[str, Any]:
    parsed = []
    for row in rows:
        dt = parse_dt(row.get("t"))
        if not dt:
            continue
        parsed.append((dt.astimezone(ET), row))
    opening = [r for dt, r in parsed if session_dt(day, 9, 30) <= dt < session_dt(day, 9, 45)]
    observed = [r for dt, r in parsed if session_dt(day, 9, 30) <= dt < session_dt(day, 9, 50)]
    if len(opening) < 10 or not observed:
        raise BriefError(f"insufficient completed opening bars: opening={len(opening)} observed={len(observed)}")
    or_high = max(num(r.get("h")) or -math.inf for r in opening)
    or_low = min(num(r.get("l")) or math.inf for r in opening)
    last = num(observed[-1].get("c"))
    vol = sum(num(r.get("v")) or 0.0 for r in observed)
    vwap_num = sum((num(r.get("vw")) or num(r.get("c")) or 0.0) * (num(r.get("v")) or 0.0) for r in observed)
    vwap = vwap_num / vol if vol else None
    if last is None or not math.isfinite(or_high) or not math.isfinite(or_low):
        raise BriefError("invalid opening range values")
    location = "above_range" if last > or_high else "below_range" if last < or_low else "inside_range"
    return {
        "last": round(last, 4),
        "or_high": round(or_high, 4),
        "or_low": round(or_low, 4),
        "vwap": round(vwap, 4) if vwap is not None else None,
        "location": location,
        "bars": len(observed),
        "cutoff": session_dt(day, 9, 50).astimezone(UTC).isoformat(),
    }


def find_result(payload: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    for row in payload.get("results") or []:
        if row.get("ticker") == ticker and not row.get("error"):
            return row
    return None


def gex_summary(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"available": False}
    spot = num(row.get("spot"))
    flip = num(row.get("flip"))
    call = num(row.get("call_wall"))
    put = num(row.get("put_wall"))

    def dist(level: float | None) -> float | None:
        return ((level / spot) - 1.0) * 100.0 if level and spot else None

    return {
        "available": True,
        "regime": row.get("regime"),
        "spot": spot,
        "flip": flip,
        "call_wall": call,
        "put_wall": put,
        "call_wall_distance_pct": round(dist(call), 3) if dist(call) is not None else None,
        "put_wall_distance_pct": round(dist(put), 3) if dist(put) is not None else None,
        "flip_distance_pct": round(dist(flip), 3) if dist(flip) is not None else None,
        "net_gex": num(row.get("net_gex")),
        "wss_score": num(row.get("wss_score")),
        "wss_flag": row.get("wss_flag"),
        "wall_confidence": row.get("wall_confidence"),
        "coverage": num(row.get("coverage")),
        "p_c": num(row.get("p_c")),
        "p_c_flow_state_tracked": bool(row.get("p_c_flow_state_tracked")),
        "iv_surface_calibrated": row.get("iv_surface_calibrated"),
    }


def index_posture(monthly: dict[str, Any], zero: dict[str, Any]) -> str:
    regimes = [x.get("regime") for x in (monthly, zero) if x.get("available")]
    if not regimes:
        return "unavailable"
    if all(r == "negative" for r in regimes):
        return "expansion"
    if all(r == "positive" for r in regimes):
        return "pinning"
    return "mixed"


def source_record(source_id: str, kind: str, path_or_url: str, generated_at: Any,
                  observed_at: str, fresh: bool, note: str) -> dict[str, Any]:
    return {
        "id": source_id,
        "kind": kind,
        "location": path_or_url,
        "generated_at": generated_at,
        "observed_at": observed_at,
        "fresh": fresh,
        "method": SOURCE_METHODS.get(kind),
        "note": note,
    }


def evidence(eid: str, side: str, instrument: str, observation: str, role: str,
             source_ids: Iterable[str]) -> dict[str, Any]:
    return {
        "id": eid,
        "side": side,
        "instrument": instrument,
        "observation": observation,
        "role": role,
        "source_ids": list(source_ids),
    }


def sector_latest() -> tuple[dict[str, Any] | None, str | None]:
    path = DATA / "sector_rotation.csv"
    try:
        rows = list(csv.DictReader(path.open()))
    except OSError:
        return None, None
    xlks = [r for r in rows if r.get("sector_etf") == "XLK" and r.get("quadrant")]
    if not xlks:
        return None, None
    row = max(xlks, key=lambda r: r.get("date") or "")
    return {
        "date": row.get("date"),
        "quadrant": row.get("quadrant"),
        "rs_zscore": num(row.get("rs_zscore")),
        "rs_mom": num(row.get("rs_mom")),
    }, str(path.relative_to(ROOT))


def load_calendar(day: date, now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = DATA / "econ_calendar.json"
    payload = read_json(path, {}) or {}
    state = freshness(payload.get("generated_at"), CALENDAR_MAX_HOURS * 60, now)
    items = []
    if state["fresh"]:
        for item in payload.get("items") or []:
            if str(item.get("time") or "").startswith(day.isoformat()):
                items.append(item)
    return items, {"path": path, "state": state}


def optional_focus(day: date, now: datetime) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    catalyst_path = DATA / "catalyst_news.json"
    catalysts = read_json(catalyst_path, {}) or {}
    state = freshness(catalysts.get("generated_at"), CATALYST_MAX_HOURS * 60, now)
    pm_path = DATA / f"orb_premarket_{day.isoformat()}.json"
    pm_rows = read_json(pm_path, []) or []
    if not state["fresh"]:
        return None, {"path": catalyst_path, "state": state, "premarket_path": pm_path}
    by_ticker = catalysts.get("tickers") or {}
    cutoff = (day - timedelta(days=3)).isoformat()
    ranked = []
    for row in pm_rows:
        ticker = row.get("ticker")
        events = [e for e in by_ticker.get(ticker, [])
                  if e.get("source_confidence") == "high" and (e.get("filed") or "") >= cutoff]
        if not events:
            continue
        score = 3 + (2 if (num(row.get("pm_rvol")) or 0) >= 1.5 else 0) + (1 if row.get("sector_hot") else 0)
        ranked.append((score, num(row.get("pm_rvol")) or 0, ticker, row, events))
    if not ranked:
        return None, {"path": catalyst_path, "state": state, "premarket_path": pm_path}
    _, _, ticker, row, events = max(ranked)
    return {
        "ticker": ticker,
        "bias": row.get("bias"),
        "gap_pct": num(row.get("gap_pct")),
        "pm_rvol": num(row.get("pm_rvol")),
        "sector_etf": row.get("sector_etf"),
        "sector_quadrant": row.get("sector_quadrant"),
        "events": events,
    }, {"path": catalyst_path, "state": state, "premarket_path": pm_path}


def macro_confirmation(topic: str, cross_assets: dict[str, dict[str, Any]]) -> str:
    def move(symbol: str) -> float | None:
        return num((cross_assets.get(symbol) or {}).get("change_pct"))

    def shown(symbol: str) -> str:
        value = move(symbol)
        return f"{symbol} {pct(value)}" if value is not None else f"{symbol} unavailable"

    if topic == "energy_geopolitics":
        uso = move("USO")
        verdict = "confirms an active energy channel" if uso is not None and uso >= 0.35 else (
            "does not confirm an oil-price shock" if uso is not None and uso <= -0.20 else
            "shows no decisive oil confirmation"
        )
        return f"{shown('USO')} and {shown('UUP')}; the tape {verdict}."
    if topic in ("rates_inflation", "fed_liquidity"):
        tlt = move("TLT")
        verdict = "signals rising-yield pressure" if tlt is not None and tlt <= -0.25 else (
            "signals easing yield pressure" if tlt is not None and tlt >= 0.25 else
            "shows no decisive rates confirmation"
        )
        return f"{shown('TLT')} and {shown('UUP')}; the tape {verdict}."
    if topic == "growth_labor":
        iwm, spy = move("IWM"), move("SPY")
        spread = iwm - spy if iwm is not None and spy is not None else None
        verdict = f"IWM relative breadth {pct(spread)} versus SPY" if spread is not None else "relative breadth unavailable"
        return f"{shown('IWM')} and {shown('SPY')}; {verdict}."
    if topic == "technology":
        soxx, qqq = move("SOXX"), move("QQQ")
        spread = soxx - qqq if soxx is not None and qqq is not None else None
        verdict = f"SOXX leadership spread {pct(spread)} versus QQQ" if spread is not None else "semiconductor leadership unavailable"
        return f"{shown('SOXX')} and {shown('QQQ')}; {verdict}."
    return "No mapped cross-asset confirmation."


def load_macro_context(now: datetime, cross_assets: dict[str, dict[str, Any]],
                       evidence_cutoff: datetime | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = DATA / "macro_news.json"
    payload = read_json(path, {}) or {}
    state = freshness(payload.get("generated_at"), MACRO_NEWS_MAX_MIN, now)
    if not state["fresh"]:
        return [], {"path": path, "state": state, "source_health": payload.get("source_health") or {}}
    selected, seen_topics = [], set()
    selection_cutoff = (evidence_cutoff or now).astimezone(UTC)
    for item in payload.get("items") or []:
        published = parse_dt(item.get("published_at"))
        if not published or published > selection_cutoff or selection_cutoff - published > timedelta(hours=36):
            continue
        topic = item.get("topic")
        if topic in seen_topics and len(selected) < 3:
            continue
        row = dict(item)
        row["cross_asset_confirmation"] = macro_confirmation(str(topic), cross_assets)
        selected.append(row)
        seen_topics.add(topic)
        if len(selected) == 3:
            break
    return selected, {"path": path, "state": state, "source_health": payload.get("source_health") or {}}


def previous_brief(day: date) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    candidates = []
    for path in ARCHIVE.glob("*/*/*/brief.json"):
        try:
            session = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        if session < day:
            candidates.append((session, path))
    if not candidates:
        return None, None
    _, path = max(candidates)
    return read_json(path, {}) or None, read_json(path.parent / "score.json", {}) or None


def gex_magnitude_change(current: Any, previous: Any) -> float | None:
    current_value, previous_value = num(current), num(previous)
    if current_value is None or previous_value is None or abs(previous_value) < 1:
        return None
    return ((abs(current_value) / abs(previous_value)) - 1) * 100


def build_change_fingerprint(day: date, label: str, indices: dict[str, dict[str, Any]]) -> dict[str, Any]:
    previous, score = previous_brief(day)
    if not previous:
        return {
            "previous_date": None,
            "previous_label": None,
            "same_label": False,
            "previous_result": None,
            "bullets": ["No prior official Catalyst Brief is available for comparison."],
            "metrics": {},
        }
    previous_indices = previous.get("indices") or {}
    metrics, ranked_changes, bullets = {}, [], []
    for ticker in ("SPY", "QQQ"):
        current = indices[ticker]
        prior = previous_indices.get(ticker) or {}
        ticker_metrics = {}
        for tenor_key, tenor_name in (("monthly", "monthly"), ("zero_dte", "0DTE")):
            change = gex_magnitude_change(
                current.get(tenor_key, {}).get("net_gex"),
                prior.get(tenor_key, {}).get("net_gex"),
            )
            ticker_metrics[f"{tenor_key}_gex_magnitude_change_pct"] = round(change, 1) if change is not None else None
            if change is not None:
                current_gex = abs(num(current.get(tenor_key, {}).get("net_gex")) or 0) / 1_000_000_000
                previous_gex = abs(num(prior.get(tenor_key, {}).get("net_gex")) or 0) / 1_000_000_000
                ranked_changes.append((
                    abs(change),
                    f"{ticker} {tenor_name} GEX magnitude {'increased' if change >= 0 else 'decreased'} "
                    f"from {previous_gex:.2f}B to {current_gex:.2f}B ({pct(change, 0)}).",
                ))
        current_location = current.get("bars", {}).get("location")
        previous_location = prior.get("bars", {}).get("location")
        ticker_metrics["location_change"] = f"{previous_location}->{current_location}"
        if current_location and previous_location and current_location != previous_location:
            bullets.append(f"{ticker} moved from {previous_location.replace('_', ' ')} at the prior cutoff to {current_location.replace('_', ' ')} today.")
        current_width = (num(current.get("bars", {}).get("or_high")) or 0) - (num(current.get("bars", {}).get("or_low")) or 0)
        previous_width = (num(prior.get("bars", {}).get("or_high")) or 0) - (num(prior.get("bars", {}).get("or_low")) or 0)
        width_change = ((current_width / previous_width) - 1) * 100 if current_width and previous_width else None
        ticker_metrics["opening_range_width_change_pct"] = round(width_change, 1) if width_change is not None else None
        if width_change is not None and abs(width_change) >= 20:
            bullets.append(f"{ticker}'s opening range is {abs(width_change):.0f}% {'wider' if width_change > 0 else 'narrower'} than the prior session.")
        metrics[ticker] = ticker_metrics
    bullets = [sentence for _, sentence in sorted(ranked_changes, reverse=True)[:2]] + bullets
    previous_label = (previous.get("central") or {}).get("label")
    previous_result = (score or {}).get("result")
    if previous_result:
        bullets.append(f"Yesterday's fixed post-close score was {previous_result}.")
    return {
        "previous_date": previous.get("session_date"),
        "previous_label": previous_label,
        "same_label": previous_label == label,
        "previous_result": previous_result,
        "bullets": bullets[:6],
        "metrics": metrics,
    }


def describe_index(ticker: str, monthly: dict[str, Any], zero: dict[str, Any],
                   bars: dict[str, Any]) -> dict[str, Any]:
    posture = index_posture(monthly, zero)
    regime_text = (
        f"{ticker} monthly GEX is {monthly.get('regime', 'unavailable')} and 0DTE GEX is "
        f"{zero.get('regime', 'unavailable')}."
    )
    location = bars.get("location", "unavailable").replace("_", " ")
    conclusion = {
        "expansion": "Dealer positioning is compatible with directional expansion; direction still requires price confirmation.",
        "pinning": "Dealer positioning is compatible with pinning and mean reversion until price proves acceptance outside the range.",
        "mixed": "Monthly and 0DTE positioning conflict, increasing the risk of false breaks and fast reversals.",
        "unavailable": "Required GEX inputs are unavailable; no microstructure conclusion is permitted.",
    }[posture]
    return {
        "ticker": ticker,
        "posture": posture,
        "monthly": monthly,
        "zero_dte": zero,
        "bars": bars,
        "summary": f"{regime_text} Price is {location} at {money(bars.get('last'))}. {conclusion}",
    }


def central_posture(spy: dict[str, Any], qqq: dict[str, Any]) -> tuple[str, str]:
    a, b = spy["posture"], qqq["posture"]
    if "unavailable" in (a, b):
        return "INSUFFICIENT EVIDENCE", "STAND ASIDE"
    if a == b == "expansion":
        return "CONFIRMED EXPANSION", "ORB-COMPATIBLE"
    if a == b == "pinning":
        return "CONFIRMED PINNING", "MR-COMPATIBLE"
    if a == b == "mixed":
        return "CROSS-TENOR CONFLICT", "SELECTIVE / NO REGIME GATE"
    return "CROSS-INDEX DIVERGENCE", "SELECTIVE / NO REGIME GATE"


def outside_direction(location: str | None) -> str | None:
    if location == "above_range":
        return "up"
    if location == "below_range":
        return "down"
    return None


def build_question(label: str, spy: dict[str, Any], qqq: dict[str, Any]) -> str:
    sb, qb = spy["bars"], qqq["bars"]
    spy_direction = outside_direction(sb.get("location"))
    qqq_direction = outside_direction(qb.get("location"))
    if label == "CONFIRMED EXPANSION":
        if spy_direction and not qqq_direction:
            level = qb.get("or_high") if spy_direction == "up" else qb.get("or_low")
            return (f"Will SPY's early {spy_direction}side acceptance hold and pull QQQ through {money(level)}, "
                    f"or will QQQ non-confirmation force SPY back inside {money(sb.get('or_low'))}-{money(sb.get('or_high'))}?")
        if qqq_direction and not spy_direction:
            level = sb.get("or_high") if qqq_direction == "up" else sb.get("or_low")
            return (f"Will QQQ's early {qqq_direction}side acceptance hold and pull SPY through {money(level)}, "
                    f"or will SPY non-confirmation force QQQ back inside {money(qb.get('or_low'))}-{money(qb.get('or_high'))}?")
        if spy_direction and spy_direction == qqq_direction:
            return (f"Can synchronized {spy_direction}side acceptance survive the first retest of SPY "
                    f"{money(sb.get('or_high') if spy_direction == 'up' else sb.get('or_low'))} and QQQ "
                    f"{money(qb.get('or_high') if spy_direction == 'up' else qb.get('or_low'))}?")
        return (f"Will aligned negative GEX convert into a synchronized break of SPY "
                f"{money(sb.get('or_low'))}-{money(sb.get('or_high'))} and QQQ "
                f"{money(qb.get('or_low'))}-{money(qb.get('or_high'))}, or remain latent inside both ranges?")
    if label == "CONFIRMED PINNING":
        return (f"Will positive-gamma positioning keep SPY inside {money(sb.get('or_low'))}-{money(sb.get('or_high'))} "
                f"and QQQ inside {money(qb.get('or_low'))}-{money(qb.get('or_high'))}, or will a two-close break defeat the pin?")
    if label == "CROSS-INDEX DIVERGENCE":
        return f"Will {spy['ticker']} {spy['posture']} or {qqq['ticker']} {qqq['posture']} control the session, and can either index earn confirmation from the other?"
    if label == "CROSS-TENOR CONFLICT":
        return (f"Will SPY and QQQ break the same side of their opening ranges and overpower the 0DTE walls, "
                "or will the monthly/0DTE conflict produce another failed move?")
    return "Do the available SPY and QQQ inputs support any defensible microstructure thesis today?"


def build_daily_read(label: str, spy: dict[str, Any], qqq: dict[str, Any],
                     changes: dict[str, Any], macro_context: list[dict[str, Any]],
                     technology: dict[str, Any] | None) -> dict[str, str]:
    sb, qb = spy["bars"], qqq["bars"]
    spy_direction = outside_direction(sb.get("location"))
    qqq_direction = outside_direction(qb.get("location"))
    if spy_direction and spy_direction == qqq_direction:
        trigger = f"SPY and QQQ already show synchronized {spy_direction}side opening-range acceptance."
    elif spy_direction:
        trigger = f"SPY is the early leader with {spy_direction}side acceptance beyond {money(sb.get('or_high') if spy_direction == 'up' else sb.get('or_low'))}."
    elif qqq_direction:
        trigger = f"QQQ is the early leader with {qqq_direction}side acceptance beyond {money(qb.get('or_high') if qqq_direction == 'up' else qb.get('or_low'))}."
    else:
        trigger = f"Both indices retain a {label.lower().replace('confirmed ', '')} setup, but neither has earned opening-range acceptance."

    constraints = []
    if spy_direction != qqq_direction:
        if spy_direction and not qqq_direction:
            constraints.append("QQQ remains inside its range and has not confirmed SPY")
        elif qqq_direction and not spy_direction:
            constraints.append("SPY remains inside its range and has not confirmed QQQ")
        elif spy_direction and qqq_direction:
            constraints.append("SPY and QQQ are accepting opposite directions")
    if macro_context:
        top = macro_context[0]
        constraints.append(
            f"{top.get('topic', 'macro').replace('_', ' ')} risk: {top.get('headline')} "
            f"({top.get('cross_asset_confirmation')})"
        )
    if technology and technology.get("quadrant") in ("Lagging", "Weakening"):
        momentum = num(technology.get("rs_mom"))
        momentum_text = f"{momentum:+.2f}" if momentum is not None else "unavailable"
        constraints.append(f"XLK is {technology.get('quadrant')} with momentum {momentum_text}")
    constraint = "; ".join(constraints[:2]) + "." if constraints else "No material cross-index or macro constraint is confirmed yet."

    if label == "CONFIRMED EXPANSION":
        if spy_direction and spy_direction == qqq_direction:
            conclusion = f"Expansion is active {spy_direction}; favor only held retests because the regime and tape agree."
        elif spy_direction or qqq_direction:
            leader = "SPY" if spy_direction else "QQQ"
            follower = "QQQ" if spy_direction else "SPY"
            conclusion = f"Expansion pressure is present, but it is a one-index lead: {leader} triggered while {follower} withheld confirmation. Treat continuation as lower confidence until the follower clears its range."
        else:
            conclusion = "Negative GEX creates expansion potential, not a direction. With both indices inside their ranges, the correct read is latent volatility and no ORB confirmation yet."
    elif label == "CONFIRMED PINNING":
        conclusion = "Positive GEX supports rotation and mean reversion, but only while both ranges and nearby walls continue to reject price."
    elif label == "CROSS-TENOR CONFLICT":
        conclusion = "Monthly and 0DTE positioning disagree. The opening tape must resolve the conflict before either ORB or MR receives a regime advantage."
    elif label == "CROSS-INDEX DIVERGENCE":
        conclusion = "The indices disagree on microstructure. Directional exposure is lower quality until leadership becomes synchronized."
    else:
        conclusion = "Required evidence is missing or stale; abstention is the only defensible conclusion."

    difference = (changes.get("bullets") or ["No prior official comparison is available."])[0]
    return {
        "trigger": trigger,
        "constraint": constraint,
        "working_conclusion": conclusion,
        "what_changed": difference,
    }


def hinge_text(label: str, spy: dict[str, Any], qqq: dict[str, Any]) -> str:
    sb, qb = spy["bars"], qqq["bars"]
    if label == "CONFIRMED EXPANSION":
        return (f"A synchronized completed five-minute close above SPY {money(sb.get('or_high'))} and QQQ "
                f"{money(qb.get('or_high'))}, or below SPY {money(sb.get('or_low'))} and QQQ "
                f"{money(qb.get('or_low'))}, followed by one hold candle.")
    if label == "CONFIRMED PINNING":
        return (f"The thesis fails only after a completed five-minute close outside SPY "
                f"{money(sb.get('or_low'))}-{money(sb.get('or_high'))} and QQQ "
                f"{money(qb.get('or_low'))}-{money(qb.get('or_high'))}, followed by a second close that holds outside.")
    if label == "CROSS-INDEX DIVERGENCE":
        return (f"One index must break and hold its opening range while the other confirms on the same side of VWAP "
                f"(SPY {money(sb.get('vwap'))}; QQQ {money(qb.get('vwap'))}).")
    if label == "CROSS-TENOR CONFLICT":
        return (f"Both indices must break and hold the same side of their opening ranges while 0DTE walls yield; otherwise the monthly/0DTE conflict remains unresolved "
                f"(SPY {money(sb.get('or_low'))}-{money(sb.get('or_high'))}; QQQ {money(qb.get('or_low'))}-{money(qb.get('or_high'))}).")
    return "No hinge is valid until fresh SPY and QQQ GEX plus completed opening bars are available."


def build_scenarios(label: str, spy: dict[str, Any], qqq: dict[str, Any]) -> list[dict[str, Any]]:
    sb, qb = spy["bars"], qqq["bars"]
    if label == "CONFIRMED EXPANSION":
        return [
            {"name": "THESIS STRENGTHENS", "tone": "positive", "conditions": [
                f"SPY closes beyond {money(sb.get('or_low'))}-{money(sb.get('or_high'))} and QQQ beyond {money(qb.get('or_low'))}-{money(qb.get('or_high'))} in the same direction.",
                f"The next completed five-minute candle holds outside while SPY stays on the confirming side of VWAP {money(sb.get('vwap'))} and QQQ of {money(qb.get('vwap'))}.",
                f"The move accepts beyond the nearest 0DTE boundary rather than rejecting at SPY {money(spy['zero_dte'].get('put_wall'))}/{money(spy['zero_dte'].get('call_wall'))} or QQQ {money(qqq['zero_dte'].get('put_wall'))}/{money(qqq['zero_dte'].get('call_wall'))}.",
            ]},
            {"name": "THESIS WEAKENS", "tone": "warning", "conditions": [
                f"Only one index holds outside its range; current locations are SPY {sb.get('location', 'unavailable').replace('_', ' ')} and QQQ {qb.get('location', 'unavailable').replace('_', ' ')}.",
                f"The leader repeatedly crosses its VWAP (SPY {money(sb.get('vwap'))}; QQQ {money(qb.get('vwap'))}).",
                "A macro headline is not confirmed by its mapped cross-asset channel, or the nearest 0DTE wall rejects price.",
            ]},
            {"name": "NOT SUPPORTED", "tone": "negative", "conditions": [
                f"Both indices are back inside their opening ranges after 10:15 ET (SPY {money(sb.get('or_low'))}-{money(sb.get('or_high'))}; QQQ {money(qb.get('or_low'))}-{money(qb.get('or_high'))}).",
                "An attempted break closes back inside on the next candle.",
                "SPY and QQQ resolve in opposite directions.",
            ]},
        ]
    if label == "CONFIRMED PINNING":
        return [
            {"name": "THESIS STRENGTHENS", "tone": "positive", "conditions": [
                "SPY and QQQ remain inside their opening ranges and rotate around VWAP.",
                "Tests of the range edge or nearby gamma wall reject back toward balance.",
                "Neither index produces two consecutive closes outside the range.",
            ]},
            {"name": "THESIS WEAKENS", "tone": "warning", "conditions": [
                "One index holds outside its range while the other remains pinned.",
                "VWAP stops acting as a magnet for the leading index.",
                "The nearest wall begins yielding instead of rejecting price.",
            ]},
            {"name": "NOT SUPPORTED", "tone": "negative", "conditions": [
                f"Both indices hold outside their ranges (SPY {money(sb.get('or_low'))}-{money(sb.get('or_high'))}; QQQ {money(qb.get('or_low'))}-{money(qb.get('or_high'))}).",
                "The break persists for two completed five-minute candles.",
                "Both indices remain directionally aligned away from VWAP.",
            ]},
        ]
    if label == "CROSS-TENOR CONFLICT":
        return [
            {"name": "THESIS STRENGTHENS", "tone": "positive", "conditions": [
                "SPY and QQQ break and hold the same side of their opening ranges.",
                "The relevant 0DTE walls yield instead of rejecting price.",
                "Both indices remain on the confirming side of VWAP.",
            ]},
            {"name": "THESIS WEAKENS", "tone": "warning", "conditions": [
                "Only one index earns opening-range acceptance.",
                "Price oscillates between the monthly and 0DTE structural levels.",
                "The leader repeatedly crosses VWAP.",
            ]},
            {"name": "NOT SUPPORTED", "tone": "negative", "conditions": [
                "Both opening-range attempts fail back into balance.",
                "SPY and QQQ resolve in opposite directions.",
                "The 0DTE walls reject price and the ranges remain intact.",
            ]},
        ]
    return [
        {"name": "THESIS STRENGTHENS", "tone": "positive", "conditions": [
            "The leading index breaks and holds its opening range.",
            "The other index confirms on the same side of VWAP.",
            "Cross-index direction becomes aligned rather than conflicting.",
        ]},
        {"name": "THESIS WEAKENS", "tone": "warning", "conditions": [
            "The leader breaks while the other index remains inside its range.",
            "Both indices continue to cross VWAP without acceptance.",
            "The leadership relationship changes more than once.",
        ]},
        {"name": "NOT SUPPORTED", "tone": "negative", "conditions": [
            "SPY and QQQ resolve in opposite directions.",
            "Both attempted range breaks fail immediately.",
            "Required live inputs become stale or unavailable.",
        ]},
    ]


def build_packet(day: date, preview: bool = False, fixture: bool = False) -> dict[str, Any]:
    observed = utcnow()
    cutoff = session_dt(day, 9, 50)
    if not preview:
        local_now = observed.astimezone(ET)
        if local_now.date() != day or not (time(9, 50) <= local_now.time() <= time(10, 0)):
            raise BriefError("official build is allowed only 09:50-10:00 ET on the session date")

    monthly_payload = read_json(DATA / "live_gex_snapshot.json", {}) or {}
    zero_payload = read_json(DATA / "live_gex_0dte_snapshot.json", {}) or {}
    monthly_state = freshness(monthly_payload.get("generated_at"), MONTHLY_GEX_MAX_MIN, observed)
    zero_state = freshness(zero_payload.get("generated_at"), ZERO_DTE_GEX_MAX_MIN, observed)
    if preview:
        # Preview is explicitly non-official; retain age facts but allow layout testing.
        monthly_usable = bool(monthly_payload.get("results"))
        zero_usable = bool(zero_payload.get("results"))
    else:
        monthly_usable = monthly_state["fresh"]
        zero_usable = zero_state["fresh"]

    bar_end = cutoff + timedelta(minutes=1)
    if fixture:
        bars_raw, request_id = fixture_bars(day), "fixture"
        cross_assets = {
            symbol: {"previous_close": 100.0, "open": 100.0, "last": 100.0,
                     "change_pct": 0.0, "session_move_pct": 0.0, "bars": 20}
            for symbol in CROSS_ASSETS
        }
        cross_request_id = "fixture"
    else:
        try:
            bars_raw, request_id = fetch_session_bars(day, bar_end)
        except Exception:
            if not preview:
                raise
            bars_raw, request_id = fixture_bars(day), "fixture-fallback"
        try:
            cross_assets, cross_request_id = fetch_cross_asset_snapshot(day, bar_end)
        except Exception as exc:
            cross_assets, cross_request_id = {}, f"unavailable:{type(exc).__name__}"

    bars = {ticker: summarize_bars(bars_raw.get(ticker) or [], day) for ticker in ("SPY", "QQQ")}
    monthly = {
        t: gex_summary(find_result(monthly_payload, t)) if monthly_usable else {"available": False}
        for t in ("SPY", "QQQ")
    }
    zero = {
        t: gex_summary(find_result(zero_payload, f"{t}-0DTE")) if zero_usable else {"available": False}
        for t in ("SPY", "QQQ")
    }
    spy = describe_index("SPY", monthly["SPY"], zero["SPY"], bars["SPY"])
    qqq = describe_index("QQQ", monthly["QQQ"], zero["QQQ"], bars["QQQ"])
    label, compatibility = central_posture(spy, qqq)
    question = build_question(label, spy, qqq)
    hinge = hinge_text(label, spy, qqq)
    xlks, sector_path = sector_latest()
    calendar, calendar_meta = load_calendar(day, observed)
    focus, catalyst_meta = optional_focus(day, observed)
    macro_context, macro_meta = load_macro_context(observed, cross_assets, cutoff)
    indices = {"SPY": spy, "QQQ": qqq}
    changes = build_change_fingerprint(day, label, indices)

    sources = [
        source_record("S1", "monthly_gex", "data/live_gex_snapshot.json",
                      monthly_payload.get("generated_at"), iso(observed), monthly_state["fresh"],
                      f"Freshness limit {MONTHLY_GEX_MAX_MIN} minutes."),
        source_record("S2", "zero_dte_gex", "data/live_gex_0dte_snapshot.json",
                      zero_payload.get("generated_at"), iso(observed), zero_state["fresh"],
                      f"Freshness limit {ZERO_DTE_GEX_MAX_MIN} minutes."),
        source_record("S3", "alpaca_bars", f"{ALPACA_DATA_URL}?{urlencode({'symbols':'SPY,QQQ','timeframe':'1Min','feed':'iex'})}",
                      bars["SPY"].get("cutoff"), iso(observed), not request_id.startswith("fixture"),
                      f"Alpaca request ID: {request_id or 'not returned'}"),
    ]
    if sector_path:
        sources.append(source_record("S4", "sector_rotation", sector_path,
                                     xlks.get("date") if xlks else None, iso(observed), bool(xlks),
                                     "XLK is context for QQQ; RRG is descriptive and not a validated execution gate for this brief."))
    sources.append(source_record("S5", "catalyst", str(catalyst_meta["path"].relative_to(ROOT)),
                                 catalyst_meta["state"].get("generated_at"), iso(observed),
                                 catalyst_meta["state"]["fresh"], "Only high-confidence EDGAR/openFDA events may enter the thesis."))
    sources.append(source_record("S6", "calendar", str(calendar_meta["path"].relative_to(ROOT)),
                                 calendar_meta["state"].get("generated_at"), iso(observed),
                                 calendar_meta["state"]["fresh"], "BLS scheduled releases; macro-news coverage is reported separately."))
    sources.append(source_record("S7", "macro_news", str(macro_meta["path"].relative_to(ROOT)),
                                 macro_meta["state"].get("generated_at"), iso(observed),
                                 macro_meta["state"]["fresh"],
                                 f"Source health: {json.dumps(macro_meta.get('source_health') or {}, sort_keys=True)}"))
    cross_fresh = bool(cross_assets) and all((cross_assets.get(symbol) or {}).get("bars", 0) > 0 for symbol in CROSS_ASSETS)
    sources.append(source_record("S8", "cross_asset",
                                 f"{ALPACA_DATA_URL}?{urlencode({'symbols': ','.join(CROSS_ASSETS), 'feed': 'iex'})}",
                                 bars["SPY"].get("cutoff"), iso(observed), cross_fresh,
                                 f"Alpaca request IDs: {cross_request_id or 'not returned'}"))
    if changes.get("previous_date"):
        previous_date = date.fromisoformat(changes["previous_date"])
        prior_path = ARCHIVE / f"{previous_date.year:04d}" / f"{previous_date.month:02d}" / previous_date.isoformat() / "brief.json"
        previous_packet = read_json(prior_path, {}) or {}
        sources.append(source_record("S9", "prior_brief", str(prior_path.relative_to(ROOT)),
                                     previous_packet.get("generated_at"), iso(observed), bool(previous_packet),
                                     f"Prior label {changes.get('previous_label')}; fixed score {changes.get('previous_result') or 'not available'}."))

    evidence_rows = []
    for ticker, idx in (("SPY", spy), ("QQQ", qqq)):
        m, z, b = idx["monthly"], idx["zero_dte"], idx["bars"]
        side = "supports" if idx["posture"] in ("expansion", "pinning") else "pushes_back"
        evidence_rows.append(evidence(
            f"E{len(evidence_rows)+1}", side, ticker,
            f"Monthly GEX {m.get('regime', 'unavailable')}; 0DTE GEX {z.get('regime', 'unavailable')}; price {b.get('location', 'unavailable').replace('_', ' ')}.",
            f"Defines the {idx['posture']} microstructure posture.", ["S1", "S2", "S3"]
        ))
        if m.get("available"):
            evidence_rows.append(evidence(
                f"E{len(evidence_rows)+1}", "supports" if idx["posture"] != "mixed" else "pushes_back", ticker,
                f"Call wall {money(m.get('call_wall'))} ({pct(m.get('call_wall_distance_pct'))}); put wall {money(m.get('put_wall'))} ({pct(m.get('put_wall_distance_pct'))}).",
                "Provides the nearest structural boundaries and concrete invalidation area.", ["S1"]
            ))
    if xlks:
        q_side = "supports" if xlks.get("quadrant") in ("Leading", "Improving") else "pushes_back"
        evidence_rows.append(evidence(
            f"E{len(evidence_rows)+1}", q_side, "XLK",
            f"XLK RRG quadrant {xlks.get('quadrant')}; RS z-score {xlks.get('rs_zscore'):.2f}; momentum {xlks.get('rs_mom'):+.2f}.",
            "Tests whether technology-sector context supports or constrains QQQ.", ["S4"]
        ))
    if focus:
        event = focus["events"][0]
        evidence_rows.append(evidence(
            f"E{len(evidence_rows)+1}", "supports", focus["ticker"],
            f"{event.get('source')} filed {event.get('filed')}: {event.get('headline')}",
            "Primary-source catalyst for the optional single-name focus; it is not a directional vote by itself.", ["S5"]
        ))
    if changes.get("previous_date"):
        evidence_rows.append(evidence(
            f"E{len(evidence_rows)+1}", "supports" if label == changes.get("previous_label") else "pushes_back", "DAY/OVER/DAY",
            (changes.get("bullets") or ["No material change calculated."])[0],
            "Separates a repeated headline regime from a repeated internal setup.", ["S1", "S2", "S3", "S9"]
        ))
    for item in macro_context[:2]:
        evidence_rows.append(evidence(
            f"E{len(evidence_rows)+1}", "pushes_back", f"MACRO/{str(item.get('topic')).upper()}",
            f"{item.get('source')} at {item.get('published_at')}: {item.get('headline')} Cross-asset check: {item.get('cross_asset_confirmation')}",
            f"{item.get('mechanism')} This is context, not a directional vote by itself.", ["S7", "S8"]
        ))

    stale = [s["id"] for s in sources if not s["fresh"] and s["id"] in ("S1", "S2", "S3")]
    quality = "DEGRADED" if stale or request_id.startswith("fixture") else "FRESH"
    if not monthly_usable or not zero_usable:
        quality = "INSUFFICIENT"
        label, compatibility = "INSUFFICIENT EVIDENCE", "STAND ASIDE"
        question = build_question(label, spy, qqq)
        hinge = hinge_text(label, spy, qqq)

    macro_quality = (
        "FRESH" if macro_meta["state"]["fresh"] and macro_context else
        "NO QUALIFYING ITEMS" if macro_meta["state"]["fresh"] else
        "UNAVAILABLE"
    )
    daily_read = build_daily_read(label, spy, qqq, changes, macro_context, xlks)

    manual_options = build_manual_options_layer(
        ROOT, day, observed, indices, bars_raw, quality, fixture=fixture
    )
    technical_fresh = manual_options.get("technical_quality") == "FRESH"
    option_quality = manual_options.get("option_quote_quality")
    sources.append(source_record(
        "S10", "technical_context",
        f"{ALPACA_DATA_URL}?{urlencode({'symbols':'SPY,QQQ','timeframe':'1Min,1Day','feed':'iex'})}",
        manual_options.get("as_of"), iso(observed), technical_fresh,
        "EMA9/20 uses completed live IEX 1m bars. SMA20/50/200 uses prior completed daily sessions. "
        "Premarket levels use completed consolidated SIP history with a 10-bar coverage gate. "
        "Sweep/reclaim is a fixed bar-based proxy around prior-day, premarket, and opening-range levels.",
    ))
    sources.append(source_record(
        "S11", "option_chain",
        "https://data.alpaca.markets/v1beta1/options/snapshots/{SPY|QQQ}",
        manual_options.get("as_of"), iso(observed), option_quality == "AVAILABLE - INDICATIVE",
        "Free indicative quotes may differ from executable NBBO. Candidate expires as a research snapshot "
        "and must be checked at the broker immediately before a manual order.",
    ))
    for ticker in ("SPY", "QQQ"):
        card = (manual_options.get("cards") or {}).get(ticker) or {}
        technical = card.get("technical") or {}
        latest_sweep = technical.get("latest_sweep")
        sweep_text = (
            f"{latest_sweep.get('direction')} {latest_sweep.get('level_name')} "
            f"at {money(latest_sweep.get('level'))}"
            if latest_sweep else "no confirmed sweep/reclaim proxy"
        )
        direction = technical.get("base_direction", "unavailable")
        evidence_rows.append(evidence(
            f"E{len(evidence_rows)+1}",
            "supports" if direction in ("bullish", "bearish") else "pushes_back",
            f"{ticker}/MANUAL",
            f"Underlying vote {direction}: {technical.get('bullish_votes', 0)} bullish, "
            f"{technical.get('bearish_votes', 0)} bearish; {sweep_text}.",
            "Adds moving-average and defined sweep/reclaim confluence to the manual options watch only.",
            ["S3", "S10"],
        ))

    return {
        "schema_version": "catalyst-brief-3.0",
        "report_id": f"CB-{day.isoformat()}{'-PREVIEW' if preview else ''}",
        "session_date": day.isoformat(),
        "edition": "PREVIEW - AFTER-CLOSE INPUTS" if preview else "09:55 ET SESSION PLAYBOOK",
        "official": not preview,
        "generated_at": iso(observed),
        "evidence_cutoff": iso(cutoff),
        "data_quality": quality,
        "macro_quality": macro_quality,
        "stale_required_sources": stale,
        "central": {
            "label": label,
            "strategy_compatibility": compatibility,
            "question": question,
            "trigger": daily_read["trigger"],
            "constraint": daily_read["constraint"],
            "working_conclusion": daily_read["working_conclusion"],
            "what_changed": daily_read["what_changed"],
            "narrative_hinge": hinge,
        },
        "indices": indices,
        "change_fingerprint": changes,
        "macro_context": macro_context,
        "cross_assets": cross_assets,
        "technology_context": xlks,
        "manual_options": manual_options,
        "optional_focus": focus,
        "calendar": calendar,
        "scenarios": build_scenarios(label, spy, qqq),
        "evidence": evidence_rows,
        "sources": sources,
        "methodology": {
            "advisory_only": True,
            "uw_dependency": False,
            "claims_policy": "Only values present in the evidence packet may appear in the PDF.",
            "validation_status": "Observation-only until at least 20 forward sessions are scored.",
        },
    }


NAVY = colors.HexColor("#07111F")
PANEL = colors.HexColor("#102033")
INK = colors.HexColor("#172235")
MUTED = colors.HexColor("#5C6B7A")
TEAL = colors.HexColor("#11B8A6")
GOLD = colors.HexColor("#E5B94F")
RED = colors.HexColor("#D75D67")
PALE = colors.HexColor("#F3F6F8")
WHITE = colors.white


def ptext(value: Any) -> str:
    return escape(str(value if value is not None else "n/a"))


def report_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("Title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=22,
                                leading=25, textColor=WHITE, alignment=TA_LEFT, spaceAfter=6),
        "eyebrow": ParagraphStyle("Eyebrow", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8,
                                  leading=10, textColor=TEAL, tracking=1.2),
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=15,
                             leading=18, textColor=INK, spaceBefore=8, spaceAfter=7),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=11,
                             leading=13, textColor=INK, spaceBefore=5, spaceAfter=4),
        "body": ParagraphStyle("Body", parent=base["BodyText"], fontName="Helvetica", fontSize=8.6,
                               leading=12, textColor=INK, spaceAfter=4),
        "small": ParagraphStyle("Small", parent=base["BodyText"], fontName="Helvetica", fontSize=7.2,
                                leading=9.3, textColor=MUTED),
        "white": ParagraphStyle("White", parent=base["BodyText"], fontName="Helvetica", fontSize=8.7,
                                leading=12, textColor=WHITE),
        "card_title": ParagraphStyle("CardTitle", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12,
                                     leading=14, textColor=WHITE, spaceAfter=4),
        "center": ParagraphStyle("Center", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=8,
                                 leading=10, textColor=INK, alignment=TA_CENTER),
        "center_white": ParagraphStyle("CenterWhite", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=8,
                                       leading=10, textColor=WHITE, alignment=TA_CENTER),
    }


def page_decor(canvas, doc) -> None:
    canvas.saveState()
    width, height = letter
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 0.30 * inch, width, 0.30 * inch, fill=1, stroke=0)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.55 * inch, 0.32 * inch, "BOT_NEXUS - Catalyst Brief - Advisory research only")
    canvas.drawRightString(width - 0.55 * inch, 0.32 * inch, f"Page {doc.page}")
    canvas.restoreState()


def index_card(idx: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    m, z, b = idx["monthly"], idx["zero_dte"], idx["bars"]
    rows = [
        [Paragraph(ptext(idx["ticker"]), styles["card_title"]),
         Paragraph(ptext(idx["posture"].upper()), styles["card_title"])],
        [Paragraph("Monthly / 0DTE", styles["small"]),
         Paragraph(f"{ptext(m.get('regime'))} / {ptext(z.get('regime'))}", styles["white"])],
        [Paragraph("Opening range", styles["small"]),
         Paragraph(f"{money(b.get('or_low'))} - {money(b.get('or_high'))}", styles["white"])],
        [Paragraph("VWAP / last", styles["small"]),
         Paragraph(f"{money(b.get('vwap'))} / {money(b.get('last'))}", styles["white"])],
        [Paragraph("Call / put wall", styles["small"]),
         Paragraph(f"{money(m.get('call_wall'))} / {money(m.get('put_wall'))}", styles["white"])],
        [Paragraph("Read", styles["small"]), Paragraph(ptext(idx["summary"]), styles["white"])],
    ]
    table = Table(rows, colWidths=[1.15 * inch, 2.25 * inch], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#26415C")),
        ("INNERGRID", (0, 1), (-1, -1), 0.25, colors.HexColor("#26415C")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("SPAN", (0, 0), (0, 0)),
    ]))
    return table


def render_pdf(packet: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    styles = report_styles()
    doc = SimpleDocTemplate(str(out_path), pagesize=letter, rightMargin=0.52 * inch,
                            leftMargin=0.52 * inch, topMargin=0.47 * inch,
                            bottomMargin=0.52 * inch, title=f"Catalyst Brief {packet['session_date']}")
    story = []

    header = Table([
        [Paragraph("BOT_NEXUS / DAILY RESEARCH", styles["eyebrow"]),
         Paragraph(ptext(packet["edition"]), styles["eyebrow"])],
        [Paragraph("CATALYST BRIEF", styles["title"]),
         Paragraph(f"{ptext(packet['session_date'])}<br/>CORE {ptext(packet['data_quality'])}<br/>MACRO {ptext(packet.get('macro_quality', 'UNKNOWN'))}", styles["white"])],
    ], colWidths=[4.8 * inch, 2.1 * inch])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [header, Spacer(1, 10)]

    central = packet["central"]
    badge_color = TEAL if central["label"] in ("CONFIRMED EXPANSION", "CONFIRMED PINNING") else GOLD
    if central["label"] == "INSUFFICIENT EVIDENCE":
        badge_color = RED
    thesis = Table([
        [Paragraph(ptext(central["label"]), styles["center"]),
         Paragraph(ptext(central["strategy_compatibility"]), styles["center"])],
        [Paragraph(f"<b>SESSION QUESTION</b><br/>{ptext(central['question'])}", styles["body"]), ""],
        [Paragraph(f"<b>TRIGGER</b><br/>{ptext(central.get('trigger'))}", styles["body"]), ""],
        [Paragraph(f"<b>CONSTRAINT</b><br/>{ptext(central.get('constraint'))}", styles["body"]), ""],
        [Paragraph(f"<b>WORKING CONCLUSION</b><br/>{ptext(central['working_conclusion'])}", styles["body"]), ""],
        [Paragraph(f"<b>WHAT CHANGED</b><br/>{ptext(central.get('what_changed'))}", styles["body"]), ""],
        [Paragraph(f"<b>NARRATIVE HINGE</b><br/>{ptext(central['narrative_hinge'])}", styles["body"]), ""],
    ], colWidths=[4.9 * inch, 2.0 * inch])
    thesis_commands = [
        ("BACKGROUND", (0, 0), (0, 0), badge_color),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#D9E2E8")),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#CAD4DB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    thesis_commands.extend(("SPAN", (0, row), (1, row)) for row in range(1, 7))
    thesis.setStyle(TableStyle(thesis_commands))
    story += [thesis, Spacer(1, 10)]
    cards = Table([[index_card(packet["indices"]["SPY"], styles),
                    index_card(packet["indices"]["QQQ"], styles)]],
                  colWidths=[3.45 * inch, 3.45 * inch])
    cards.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [cards, Spacer(1, 9)]

    manual = packet.get("manual_options")
    if manual:
        story += [PageBreak(), Paragraph("MANUAL SPY/QQQ OPTIONS DECISION CARD", styles["h1"])]
        story.append(Paragraph(
            "<b>Separate from the bot.</b> This section is for discretionary SPY/QQQ options research. "
            "It cannot alter MR/ORB, place an order, or treat news, moving averages, or a sweep proxy as "
            "a standalone entry. Both indices must align before a call or put watch is shown.",
            styles["body"],
        ))
        premium_band = manual.get("premium_band") or [None, None]
        summary = Table([
            [Paragraph("MARKET STATE", styles["center_white"]),
             Paragraph("BUDGET / PREMIUM", styles["center_white"]),
             Paragraph("TARGET", styles["center_white"]),
             Paragraph("DATA", styles["center_white"])],
            [Paragraph(ptext(manual.get("market_state")), styles["body"]),
             Paragraph(f"${ptext(manual.get('budget'))} / "
                       f"${ptext(premium_band[0])}-${ptext(premium_band[1])}", styles["body"]),
             Paragraph(f"{ptext(manual.get('target_return_pct'))}% (rounded up to a whole cent)", styles["body"]),
             Paragraph(f"TECH {ptext(manual.get('technical_quality'))}<br/>"
                       f"PM {ptext(manual.get('premarket_quality'))}<br/>"
                       f"OPTIONS {ptext(manual.get('option_quote_quality'))}", styles["body"])],
        ], colWidths=[1.85 * inch, 1.75 * inch, 1.7 * inch, 1.6 * inch])
        summary.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5DC")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [summary, Spacer(1, 8)]

        for ticker in ("SPY", "QQQ"):
            card = (manual.get("cards") or {}).get(ticker) or {}
            technical = card.get("technical") or {}
            candidate = card.get("candidate")
            vote_text = ", ".join(
                f"{vote.get('signal')}: {vote.get('direction')}"
                for vote in technical.get("votes") or []
            )
            sweep = technical.get("latest_sweep")
            sweep_text = (
                f"{ptext(sweep.get('direction')).upper()} {ptext(sweep.get('level_name'))} "
                f"{money(sweep.get('level'))}; swept {ptext(sweep.get('swept_at'))}, "
                f"confirmed {ptext(sweep.get('confirmed_at'))}"
                if sweep else "None confirmed under the penetration + reclaim + next-bar-hold rule."
            )
            rows = [
                [Paragraph(f"{ptext(ticker)} / {ptext(card.get('state'))}", styles["center_white"]), ""],
                [Paragraph("Underlying votes", styles["small"]),
                 Paragraph(ptext(vote_text), styles["body"])],
                [Paragraph("Moving averages", styles["small"]),
                 Paragraph(
                     f"1m EMA9 {money(technical.get('ema9_1m'))}; EMA20 {money(technical.get('ema20_1m'))}. "
                     f"Prior-session SMA20 {money(technical.get('sma20_prior'))}; "
                     f"SMA50 {money(technical.get('sma50_prior'))}; SMA200 {money(technical.get('sma200_prior'))}.",
                     styles["body"],
                 )],
                [Paragraph("Reference levels", styles["small"]),
                 Paragraph(
                     f"Prior H/L {money(technical.get('prior_day_high'))}/{money(technical.get('prior_day_low'))}; "
                     f"premarket H/L {money(technical.get('premarket_high'))}/{money(technical.get('premarket_low'))} "
                     f"({ptext(technical.get('premarket_quality'))}); opening-range H/L "
                     f"{money(technical.get('opening_range_high'))}/{money(technical.get('opening_range_low'))}.",
                     styles["body"],
                 )],
                [Paragraph("Liquidity sweep proxy", styles["small"]),
                 Paragraph(sweep_text, styles["body"])],
                [Paragraph("Activation / invalidation", styles["small"]),
                 Paragraph(
                     f"Call watch above {money(technical.get('call_activation'))}; "
                     f"put watch below {money(technical.get('put_activation'))}. "
                     f"Current thesis invalidation: {ptext(technical.get('invalidation'))}.",
                     styles["body"],
                 )],
            ]
            if candidate:
                reasons = "; ".join(candidate.get("reject_reasons") or ["none under the fixed research screen"])
                rows.extend([
                    [Paragraph("Contract snapshot", styles["small"]),
                     Paragraph(
                         f"<b>{ptext(candidate.get('contract'))}</b> | "
                         f"{ptext(candidate.get('type')).upper()} {money(candidate.get('strike'))} | "
                         f"bid/ask {money(candidate.get('bid'))}/{money(candidate.get('ask'))} | "
                         f"spread {money(candidate.get('spread'))} ({ptext(candidate.get('spread_pct'))}%) | "
                         f"delta {ptext(candidate.get('delta'))} | theta/day {ptext(candidate.get('theta_per_day'))} | "
                         f"T-1 OI {ptext(candidate.get('open_interest_t1'))} | "
                         f"quote {ptext(candidate.get('quote_timestamp'))}.",
                         styles["body"],
                     )],
                    [Paragraph("$400 / +25% math", styles["small"]),
                     Paragraph(
                         f"{ptext(candidate.get('contracts'))} contracts = "
                         f"${ptext(candidate.get('estimated_debit'))}; target "
                         f"{money(candidate.get('target_premium'))}; gross target "
                         f"${ptext(candidate.get('gross_target_profit'))}. Estimated full-spread cost "
                         f"${ptext(candidate.get('estimated_full_spread_cost'))} = "
                         f"{ptext(candidate.get('friction_ratio_pct'))}% of gross target. "
                         f"Constant-IV model requires underlying move "
                         f"{ptext(candidate.get('estimated_required_underlying_move_pct'))}%.",
                         styles["body"],
                     )],
                    [Paragraph("Screen", styles["small"]),
                     Paragraph(
                         f"<b>{ptext(candidate.get('screen'))}</b>. Reasons: {ptext(reasons)}. "
                         "Indicative feed is not executable NBBO; verify the live broker quote immediately before entry.",
                         styles["body"],
                     )],
                ])
            else:
                rows.append([
                    Paragraph("Contract snapshot", styles["small"]),
                    Paragraph(ptext(card.get("candidate_note")), styles["body"]),
                ])
            table = Table(rows, colWidths=[1.3 * inch, 5.6 * inch])
            table.setStyle(TableStyle([
                ("SPAN", (0, 0), (1, 0)),
                ("BACKGROUND", (0, 0), (1, 0), PANEL),
                ("TEXTCOLOR", (0, 0), (1, 0), WHITE),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5DC")),
                ("INNERGRID", (0, 1), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story += [KeepTogether(table), Spacer(1, 8)]

        story.append(Paragraph(
            "<b>Risk boundary.</b> The report assumes no stop-loss rule and therefore does not claim positive "
            "expectancy. Cheap premium is not an edge. The displayed required move holds IV constant and becomes "
            "less reliable as expiration approaches. [S10, S11]",
            styles["small"],
        ))

    story += [PageBreak(), Paragraph("EVIDENCE BALANCE", styles["h1"])]
    ev_rows = [[Paragraph("SIDE", styles["center"]), Paragraph("OBSERVED DATA", styles["center"]),
                Paragraph("ROLE IN THESIS", styles["center"]), Paragraph("SOURCE", styles["center"])]]
    for row in packet["evidence"]:
        ev_rows.append([
            Paragraph("SUPPORTS" if row["side"] == "supports" else "PUSHES BACK", styles["small"]),
            Paragraph(f"<b>{ptext(row['instrument'])}</b> - {ptext(row['observation'])}", styles["small"]),
            Paragraph(ptext(row["role"]), styles["small"]),
            Paragraph(", ".join(f"[{ptext(x)}]" for x in row["source_ids"]), styles["small"]),
        ])
    ev_table = Table(ev_rows, colWidths=[0.82 * inch, 2.45 * inch, 2.75 * inch, 0.88 * inch], repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#C8D2DA")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i, row in enumerate(packet["evidence"], start=1):
        style_cmds.append(("BACKGROUND", (0, i), (0, i), colors.HexColor("#DDF5F1") if row["side"] == "supports" else colors.HexColor("#FBE7E9")))
    ev_table.setStyle(TableStyle(style_cmds))
    story += [ev_table, Spacer(1, 8)]

    story.append(Paragraph("WHAT CHANGED SINCE THE PRIOR REPORT", styles["h1"]))
    fingerprint = packet.get("change_fingerprint") or {}
    for bullet in fingerprint.get("bullets") or ["No prior official comparison is available."]:
        story.append(Paragraph(f"- {ptext(bullet)}", styles["body"]))
    if fingerprint.get("same_label"):
        story.append(Paragraph(
            "<b>Repeated label does not mean repeated setup.</b> Compare GEX magnitude, opening-range location, "
            "range width, wall migration, and the prior fixed score before treating the sessions as equivalent. [S9]",
            styles["small"],
        ))
    story.append(Paragraph("THREE DAILY PATHS", styles["h1"]))
    tone_colors = {"positive": TEAL, "warning": GOLD, "negative": RED}
    for scenario in packet["scenarios"]:
        lines = "<br/>".join(f"- {ptext(c)}" for c in scenario["conditions"])
        box = Table([[Paragraph(ptext(scenario["name"]), styles["center"]),
                      Paragraph(lines, styles["body"])]], colWidths=[1.48 * inch, 5.42 * inch])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), tone_colors[scenario["tone"]]),
            ("BACKGROUND", (1, 0), (1, 0), PALE),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5DC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story += [KeepTogether(box), Spacer(1, 7)]

    story.append(Paragraph("MACRO NEWS + TAPE CONFIRMATION", styles["h1"]))
    macro_rows = packet.get("macro_context") or []
    if macro_rows:
        rows = [[Paragraph("TIME / TOPIC", styles["center"]), Paragraph("HEADLINE + MECHANISM", styles["center"]),
                 Paragraph("CROSS-ASSET CHECK", styles["center"])]]
        for item in macro_rows:
            source_link = (
                f'<link href="{ptext(item.get("url"))}" color="#0B7F75">{ptext(item.get("source"))}</link>'
                if item.get("url") else ptext(item.get("source"))
            )
            rows.append([
                Paragraph(f"{ptext(item.get('published_at'))}<br/><b>{ptext(str(item.get('topic')).replace('_', ' ').upper())}</b><br/>{ptext(item.get('impact'))}", styles["small"]),
                Paragraph(f"<b>{ptext(item.get('headline'))}</b><br/>{ptext(item.get('mechanism'))}<br/>{source_link} [S7]", styles["small"]),
                Paragraph(f"{ptext(item.get('cross_asset_confirmation'))} [S8]", styles["small"]),
            ])
        macro_table = Table(rows, colWidths=[1.35 * inch, 3.55 * inch, 2.0 * inch], repeatRows=1)
        macro_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5DC")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(macro_table)
    else:
        story.append(Paragraph(
            "No qualifying fresh macro item entered the packet. Check the MACRO status in the header and [S7] "
            "source health; absence is not represented as complete macro coverage.",
            styles["body"],
        ))

    story.append(Paragraph("KNOWN CALENDAR", styles["h1"]))
    if packet["calendar"]:
        cal_rows = [[Paragraph("TIME", styles["center"]), Paragraph("EVENT", styles["center"]),
                     Paragraph("THESIS EFFECT", styles["center"])]]
        for item in packet["calendar"]:
            cal_rows.append([
                Paragraph(ptext(item.get("time")), styles["small"]),
                Paragraph(ptext(item.get("event")), styles["small"]),
                Paragraph("Pending releases can invalidate pre-event range acceptance; wait for the first completed post-event candle.", styles["small"]),
            ])
        cal = Table(cal_rows, colWidths=[1.25 * inch, 2.45 * inch, 3.2 * inch], repeatRows=1)
        cal.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5DC")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story += [cal]
    else:
        story.append(Paragraph("No qualifying BLS release was scheduled for this session, or the BLS calendar was unavailable. This calendar row is separate from the macro-news section above. [S6]", styles["body"]))

    story.append(Paragraph("OPTIONAL SINGLE-NAME FOCUS", styles["h1"]))
    focus = packet.get("optional_focus")
    if focus:
        event = focus["events"][0]
        story.append(Paragraph(
            f"<b>{ptext(focus['ticker'])} {ptext(focus.get('bias'))}</b> - gap {pct(focus.get('gap_pct'))}, "
            f"premarket relative volume {ptext(focus.get('pm_rvol'))}x, sector {ptext(focus.get('sector_etf'))} "
            f"{ptext(focus.get('sector_quadrant'))}. Catalyst: {ptext(event.get('headline'))} [{ptext('S5')}]",
            styles["body"]
        ))
    else:
        story.append(Paragraph("No fresh high-confidence EDGAR/openFDA catalyst overlapped a current premarket candidate. The report does not force a stock idea.", styles["body"]))

    story += [PageBreak(), Paragraph("SOURCES, FRESHNESS, AND CLAIM BOUNDARIES", styles["h1"])]
    src_rows = [[Paragraph("ID", styles["center"]), Paragraph("SOURCE / METHOD", styles["center"]),
                 Paragraph("OBSERVED AT", styles["center"]), Paragraph("STATUS", styles["center"])]]
    for src in packet["sources"]:
        src_rows.append([
            Paragraph(ptext(src["id"]), styles["small"]),
            Paragraph(f"<b>{ptext(src['kind'])}</b><br/>{ptext(src['location'])}<br/>{ptext(src.get('method'))}<br/>{ptext(src.get('note'))}", styles["small"]),
            Paragraph(ptext(src.get("observed_at")), styles["small"]),
            Paragraph("FRESH" if src["fresh"] else "STALE / UNAVAILABLE", styles["small"]),
        ])
    src_table = Table(src_rows, colWidths=[0.42 * inch, 4.08 * inch, 1.55 * inch, 0.85 * inch], repeatRows=1)
    src_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5DC")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [src_table, Spacer(1, 10)]
    story.append(Paragraph(
        "<b>Interpretation boundary.</b> GEX, walls, WSS, sector rotation, opening range, and catalysts are descriptive evidence. "
        "This document does not place orders, change strategy code, or authorize real-money execution. The ORB/MR compatibility label remains observation-only until at least 20 forward sessions are scored.",
        styles["body"]
    ))
    story.append(Paragraph(
        f"Report ID {ptext(packet['report_id'])} | Generated {ptext(packet['generated_at'])} | Evidence cutoff {ptext(packet['evidence_cutoff'])}",
        styles["small"]
    ))
    doc.build(story, onFirstPage=page_decor, onLaterPages=page_decor)


def archive_paths(day: date) -> dict[str, Path]:
    base = ARCHIVE / f"{day.year:04d}" / f"{day.month:02d}" / day.isoformat()
    return {
        "base": base,
        "json": base / "brief.json",
        "pdf": base / "brief.pdf",
        "status": base / "status.json",
        "score": base / "score.json",
        "manifest": base / "manifest.json",
    }


def write_official(packet: dict[str, Any]) -> dict[str, Path]:
    day = date.fromisoformat(packet["session_date"])
    paths = archive_paths(day)
    if paths["json"].exists() or paths["pdf"].exists():
        existing = read_json(paths["json"], {}) or {}
        if existing.get("report_id") == packet.get("report_id"):
            return paths
        raise BriefError(f"official archive already exists and differs: {paths['base']}")
    paths["base"].mkdir(parents=True, exist_ok=True)
    atomic_json(paths["json"], packet)
    render_pdf(packet, paths["pdf"])
    manifest = {
        "report_id": packet["report_id"],
        "sealed_at": iso(utcnow()),
        "brief_json_sha256": sha256(paths["json"]),
        "brief_pdf_sha256": sha256(paths["pdf"]),
        "generator_sha256": sha256(Path(__file__)),
    }
    atomic_json(paths["manifest"], manifest)
    return paths


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=check)


def publish_day(day: date, paths: dict[str, Path]) -> None:
    if not (PUBLISH_REPO / ".git").exists():
        raise BriefError(f"private snapshot repo missing: {PUBLISH_REPO}")
    PUBLISH_LOCK.touch(exist_ok=True)
    with PUBLISH_LOCK.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        git(PUBLISH_REPO, "fetch", "origin", "--quiet")
        git(PUBLISH_REPO, "pull", "--no-rebase", "--quiet")
        rel_dir = Path(f"{day.year:04d}/{day.month:02d}")
        pub_dir = PUBLISH_ROOT / rel_dir
        pub_dir.mkdir(parents=True, exist_ok=True)
        mapping = {
            paths["pdf"]: pub_dir / f"{day.isoformat()}.pdf",
            paths["json"]: pub_dir / f"{day.isoformat()}.json",
            paths["manifest"]: pub_dir / f"{day.isoformat()}.manifest.json",
        }
        for optional in ("status", "score"):
            if paths[optional].exists():
                mapping[paths[optional]] = pub_dir / f"{day.isoformat()}.{optional}.json"
        for src, dst in mapping.items():
            shutil.copy2(src, dst)

        index_path = PUBLISH_ROOT / "index.json"
        index = read_json(index_path, {"schema_version": "catalyst-brief-index-1.0", "reports": []})
        reports = [r for r in index.get("reports", []) if r.get("date") != day.isoformat()]
        brief = read_json(paths["json"], {}) or {}
        score = read_json(paths["score"], None)
        status = read_json(paths["status"], None)
        reports.append({
            "date": day.isoformat(),
            "report_id": brief.get("report_id"),
            "label": (brief.get("central") or {}).get("label"),
            "strategy_compatibility": (brief.get("central") or {}).get("strategy_compatibility"),
            "data_quality": brief.get("data_quality"),
            "pdf": f"briefs/{rel_dir.as_posix()}/{day.isoformat()}.pdf",
            "json": f"briefs/{rel_dir.as_posix()}/{day.isoformat()}.json",
            "score": score,
            "live_status": status,
        })
        index["generated_at"] = iso(utcnow())
        index["reports"] = sorted(reports, key=lambda r: r["date"], reverse=True)
        atomic_json(index_path, index)

        rels = [str(dst.relative_to(PUBLISH_REPO)) for dst in mapping.values()]
        rels.append(str(index_path.relative_to(PUBLISH_REPO)))
        git(PUBLISH_REPO, "add", "--", *rels)
        if git(PUBLISH_REPO, "diff", "--cached", "--quiet", "--", *rels, check=False).returncode == 0:
            return
        git(PUBLISH_REPO, "-c", "user.name=dashboard-bot", "-c", "user.email=dashboard-bot@heff.local",
            "commit", "-m", f"catalyst brief {day.isoformat()}", "--quiet")
        result = git(PUBLISH_REPO, "push", "--quiet", check=False)
        if result.returncode:
            raise BriefError(f"brief publish failed: {result.stderr.strip()}")


def classify_live(packet: dict[str, Any], bars_raw: dict[str, list[dict[str, Any]]], at: datetime) -> dict[str, Any]:
    states = {}
    for ticker in ("SPY", "QQQ"):
        rows = []
        for row in bars_raw.get(ticker) or []:
            dt = parse_dt(row.get("t"))
            if dt and dt.astimezone(ET) < at:
                rows.append(row)
        if not rows:
            states[ticker] = "unavailable"
            continue
        last = num(rows[-1].get("c"))
        b = packet["indices"][ticker]["bars"]
        states[ticker] = "above_range" if last and last > b["or_high"] else "below_range" if last and last < b["or_low"] else "inside_range"
    label = packet["central"]["label"]
    aligned_break = states["SPY"] == states["QQQ"] and states["SPY"] in ("above_range", "below_range")
    both_inside = states["SPY"] == states["QQQ"] == "inside_range"
    if label == "CONFIRMED EXPANSION":
        status = "STRENGTHENING" if aligned_break else "NOT SUPPORTED" if both_inside else "WEAKENING"
    elif label == "CONFIRMED PINNING":
        status = "STRENGTHENING" if both_inside else "NOT SUPPORTED" if aligned_break else "WEAKENING"
    elif label == "CROSS-INDEX DIVERGENCE":
        status = "STRENGTHENING" if states["SPY"] != states["QQQ"] else "NOT SUPPORTED" if aligned_break or both_inside else "WEAKENING"
    elif label == "CROSS-TENOR CONFLICT":
        status = "STRENGTHENING" if aligned_break else "NOT SUPPORTED" if both_inside else "WEAKENING"
    else:
        status = "NO THESIS"
    return {"as_of": iso(at), "status": status, "index_states": states, "report_id": packet["report_id"]}


def update_hinge(day: date, publish: bool) -> dict[str, Any]:
    paths = archive_paths(day)
    packet = read_json(paths["json"], None)
    if not packet:
        raise BriefError(f"no official brief for {day}")
    at = utcnow().astimezone(ET)
    bars, _ = fetch_session_bars(day, at)
    status = classify_live(packet, bars, at)
    atomic_json(paths["status"], status)
    if publish:
        publish_day(day, paths)
    return status


def score_day(day: date, publish: bool, fixture: bool = False) -> dict[str, Any]:
    paths = archive_paths(day)
    packet = read_json(paths["json"], None)
    if not packet:
        raise BriefError(f"no official brief for {day}")
    end = session_dt(day, 16, 1)
    bars_raw = fixture_bars(day) if fixture else fetch_session_bars(day, end)[0]
    index_outcomes = {}
    for ticker in ("SPY", "QQQ"):
        rows = []
        for row in bars_raw.get(ticker) or []:
            dt = parse_dt(row.get("t"))
            if dt and session_dt(day, 10, 0) <= dt.astimezone(ET) < session_dt(day, 16, 0):
                rows.append(row)
        if not rows:
            index_outcomes[ticker] = {"state": "unavailable"}
            continue
        b = packet["indices"][ticker]["bars"]
        closes = [num(r.get("c")) for r in rows]
        closes = [x for x in closes if x is not None]
        inside = sum(1 for x in closes if b["or_low"] <= x <= b["or_high"]) / len(closes)
        close = closes[-1]
        state = "up_expansion" if close > b["or_high"] and inside < 0.5 else "down_expansion" if close < b["or_low"] and inside < 0.5 else "pinning" if inside >= 0.5 else "mixed"
        index_outcomes[ticker] = {
            "state": state,
            "close": round(close, 4),
            "fraction_inside_opening_range": round(inside, 4),
            "max_high": round(max(num(r.get("h")) or -math.inf for r in rows), 4),
            "min_low": round(min(num(r.get("l")) or math.inf for r in rows), 4),
        }
    a, b = index_outcomes["SPY"]["state"], index_outcomes["QQQ"]["state"]
    aligned_expansion = a == b and a in ("up_expansion", "down_expansion")
    both_pin = a == b == "pinning"
    label = packet["central"]["label"]
    if label == "CONFIRMED EXPANSION":
        result = "THESIS STRENGTHENED" if aligned_expansion else "NOT SUPPORTED" if both_pin else "THESIS WEAKENED"
    elif label == "CONFIRMED PINNING":
        result = "THESIS STRENGTHENED" if both_pin else "NOT SUPPORTED" if aligned_expansion else "THESIS WEAKENED"
    elif label == "CROSS-INDEX DIVERGENCE":
        result = "THESIS STRENGTHENED" if a != b else "NOT SUPPORTED"
    elif label == "CROSS-TENOR CONFLICT":
        result = "THESIS STRENGTHENED" if aligned_expansion else "NOT SUPPORTED" if both_pin else "THESIS WEAKENED"
    else:
        result = "NOT SCORED"
    score = {
        "schema_version": "catalyst-brief-score-1.0",
        "report_id": packet["report_id"],
        "session_date": day.isoformat(),
        "scored_at": iso(utcnow()),
        "result": result,
        "index_outcomes": index_outcomes,
        "rule": "Post-10:00 closing-location and fraction-inside-opening-range classification; fixed before forward use.",
    }
    atomic_json(paths["score"], score)
    if publish:
        publish_day(day, paths)
    return score


def verify_archive() -> dict[str, Any]:
    checked = 0
    failures = []
    for manifest_path in ARCHIVE.glob("*/*/*/manifest.json"):
        manifest = read_json(manifest_path, {}) or {}
        base = manifest_path.parent
        for name, key in (("brief.json", "brief_json_sha256"), ("brief.pdf", "brief_pdf_sha256")):
            path = base / name
            if not path.exists() or sha256(path) != manifest.get(key):
                failures.append(str(path))
        checked += 1
    return {"status": "verified" if not failures else "failed", "archives": checked, "failures": failures}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("build", "preview", "hinge", "score", "verify"), default="preview")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, default today ET")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--fixture", action="store_true", help="tests/visual QA only")
    args = ap.parse_args()
    day = date.fromisoformat(args.date) if args.date else datetime.now(ET).date()
    try:
        if args.mode == "verify":
            result = verify_archive()
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "verified" else 1
        if args.mode in ("build", "preview"):
            preview = args.mode == "preview"
            packet = build_packet(day, preview=preview, fixture=args.fixture)
            if preview:
                OUTPUT_PDF.mkdir(parents=True, exist_ok=True)
                pdf_path = OUTPUT_PDF / f"catalyst_brief_preview_{day.isoformat()}.pdf"
                json_path = OUTPUT_PDF / f"catalyst_brief_preview_{day.isoformat()}.json"
                atomic_json(json_path, packet)
                render_pdf(packet, pdf_path)
                print(json.dumps({"mode": "preview", "pdf": str(pdf_path), "json": str(json_path)}, indent=2))
                return 0
            paths = write_official(packet)
            if args.publish:
                publish_day(day, paths)
            print(json.dumps({"mode": "build", "report_id": packet["report_id"], "pdf": str(paths["pdf"]), "published": args.publish}, indent=2))
            return 0
        if args.mode == "hinge":
            print(json.dumps(update_hinge(day, args.publish), indent=2))
            return 0
        if args.mode == "score":
            print(json.dumps(score_day(day, args.publish, fixture=args.fixture), indent=2))
            return 0
    except Exception as exc:
        print(f"catalyst brief failed: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
