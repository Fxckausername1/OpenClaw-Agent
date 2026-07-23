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

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
SOURCE_METHODS = {
    "monthly_gex": "Alpaca options chain -> bot GEX engine, approximately 30 DTE",
    "zero_dte_gex": "Alpaca same-day options chain -> bot GEX engine",
    "alpaca_bars": "Alpaca IEX one-minute equity bars",
    "sector_rotation": "Alpaca daily bars; sector ETF / SPY relative rotation",
    "catalyst": "SEC EDGAR 8-K and openFDA primary-source event archive",
    "calendar": "U.S. BLS official release calendar (ICS); coverage intentionally disclosed",
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
    start = session_dt(day, 9, 30)
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


def build_question(label: str, spy: dict[str, Any], qqq: dict[str, Any]) -> str:
    if label == "CONFIRMED EXPANSION":
        return "Can SPY and QQQ sustain acceptance outside their opening ranges, or will nearby gamma walls force the move back into balance?"
    if label == "CONFIRMED PINNING":
        return "Will positive-gamma positioning keep SPY and QQQ contained, or will a confirmed opening-range break defeat the expected pin?"
    if label == "CROSS-INDEX DIVERGENCE":
        return f"Will {spy['ticker']} {spy['posture']} or {qqq['ticker']} {qqq['posture']} control the session, and can either index earn confirmation from the other?"
    if label == "CROSS-TENOR CONFLICT":
        return "Will the shared monthly-negative/0DTE-positive conflict resolve into sustained expansion, or will the opening move fail back into balance?"
    return "Do the available SPY and QQQ inputs support any defensible microstructure thesis today?"


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
                "SPY and QQQ close outside their opening ranges in the same direction.",
                "The next completed five-minute candle holds outside rather than immediately reclaiming the range.",
                "Both remain on the confirming side of VWAP.",
            ]},
            {"name": "THESIS WEAKENS", "tone": "warning", "conditions": [
                "Only one index breaks its range while the other remains inside.",
                "The leader breaks but repeatedly crosses VWAP.",
                "Price reaches a major wall without acceptance beyond it.",
            ]},
            {"name": "NOT SUPPORTED", "tone": "negative", "conditions": [
                "Both indices remain inside their opening ranges after 10:15 ET.",
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

    if fixture:
        bars_raw, request_id = fixture_bars(day), "fixture"
    else:
        bar_end = observed.astimezone(ET) if preview else cutoff + timedelta(minutes=1)
        try:
            bars_raw, request_id = fetch_session_bars(day, bar_end)
        except Exception:
            if not preview:
                raise
            bars_raw, request_id = fixture_bars(day), "fixture-fallback"

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
                                 calendar_meta["state"]["fresh"], "BLS-only coverage; Fed, Treasury, ISM, and company earnings are not yet included."))

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

    stale = [s["id"] for s in sources if not s["fresh"] and s["id"] in ("S1", "S2", "S3")]
    quality = "DEGRADED" if stale or request_id.startswith("fixture") else "FRESH"
    if not monthly_usable or not zero_usable:
        quality = "INSUFFICIENT"
        label, compatibility = "INSUFFICIENT EVIDENCE", "STAND ASIDE"
        question = build_question(label, spy, qqq)
        hinge = hinge_text(label, spy, qqq)

    conclusion = {
        "CONFIRMED EXPANSION": "Both indices have aligned expansion-compatible GEX. Direction must be earned through synchronized range acceptance.",
        "CONFIRMED PINNING": "Both indices have aligned pinning-compatible GEX. Mean reversion remains the working expectation until a two-close range break.",
        "CROSS-INDEX DIVERGENCE": "SPY and QQQ do not share the same microstructure posture. Expect lower confidence until one earns confirmation from the other.",
        "CROSS-TENOR CONFLICT": "Both indices show the same monthly-negative/0DTE-positive conflict. Directional conviction is withheld until price resolves the tenor disagreement.",
        "INSUFFICIENT EVIDENCE": "Required evidence is missing or stale. The system abstains instead of manufacturing a session thesis.",
    }[label]

    return {
        "schema_version": "catalyst-brief-1.0",
        "report_id": f"CB-{day.isoformat()}{'-PREVIEW' if preview else ''}",
        "session_date": day.isoformat(),
        "edition": "PREVIEW - AFTER-CLOSE INPUTS" if preview else "09:55 ET SESSION PLAYBOOK",
        "official": not preview,
        "generated_at": iso(observed),
        "evidence_cutoff": iso(cutoff),
        "data_quality": quality,
        "stale_required_sources": stale,
        "central": {
            "label": label,
            "strategy_compatibility": compatibility,
            "question": question,
            "working_conclusion": conclusion,
            "narrative_hinge": hinge,
        },
        "indices": {"SPY": spy, "QQQ": qqq},
        "technology_context": xlks,
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
         Paragraph(f"{ptext(packet['session_date'])}<br/>{ptext(packet['data_quality'])}", styles["white"])],
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
        [Paragraph(f"<b>WORKING CONCLUSION</b><br/>{ptext(central['working_conclusion'])}", styles["body"]), ""],
        [Paragraph(f"<b>NARRATIVE HINGE</b><br/>{ptext(central['narrative_hinge'])}", styles["body"]), ""],
    ], colWidths=[4.9 * inch, 2.0 * inch])
    thesis.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), badge_color),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#D9E2E8")),
        ("SPAN", (0, 1), (1, 1)), ("SPAN", (0, 2), (1, 2)), ("SPAN", (0, 3), (1, 3)),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#CAD4DB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8E0E5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [thesis, Spacer(1, 10)]
    cards = Table([[index_card(packet["indices"]["SPY"], styles),
                    index_card(packet["indices"]["QQQ"], styles)]],
                  colWidths=[3.45 * inch, 3.45 * inch])
    cards.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [cards, Spacer(1, 9)]

    story.append(Paragraph("EVIDENCE BALANCE", styles["h1"]))
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
    story += [ev_table, PageBreak()]

    story.append(Paragraph("THREE FIXED PATHS", styles["h1"]))
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
        story.append(Paragraph("No qualifying BLS release was present for this session, or the BLS feed was unavailable. Fed, Treasury, ISM, and company earnings are not yet covered. [S6]", styles["body"]))

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
