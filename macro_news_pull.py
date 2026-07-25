#!/usr/bin/env python3
"""Build a cited macro-news packet for the Catalyst Brief.

Finnhub and Yahoo are discovery sources. Federal Reserve RSS is a primary source.
Headlines are filtered and mapped to market channels deterministically; they never
become a directional trade call without observed cross-asset confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "macro_news.json"
FINNHUB_CREDENTIALS = ROOT / "trading_py" / "credentials.json"
UTC = timezone.utc
LOOKBACK_HOURS = 36

TOPICS = {
    "rates_inflation": {
        "terms": ("inflation", "cpi", "pce", "ppi", "treasury yield", "bond yield", "interest rate", "rate cut", "rate hike"),
        "mechanism": "Rates/inflation can change duration pressure on QQQ and the discount rate for SPY.",
        "channels": ("TLT", "UUP", "QQQ"),
    },
    "fed_liquidity": {
        "terms": ("federal reserve", "fomc", "powell", "monetary policy", "balance sheet", "liquidity", "treasury auction", "debt issuance"),
        "mechanism": "Policy and liquidity expectations can reprice yields, the dollar, and equity risk appetite.",
        "channels": ("TLT", "UUP", "SPY", "QQQ"),
    },
    "energy_geopolitics": {
        "terms": ("oil", "crude", "opec", "iran", "war", "conflict", "sanction", "strait of hormuz", "middle east"),
        "mechanism": "Energy or geopolitical stress can lift inflation risk and reduce broad equity risk appetite.",
        "channels": ("USO", "UUP", "SPY"),
    },
    "growth_labor": {
        "terms": ("payroll", "jobs report", "unemployment", "gdp", "recession", "consumer spending", "retail sales", "pmi", "ism"),
        "mechanism": "Growth and labor news can change earnings expectations and cyclical breadth.",
        "channels": ("IWM", "SPY", "QQQ"),
    },
    "technology": {
        "terms": ("semiconductor", "chip", "artificial intelligence", " ai ", "nvidia", "export control", "megacap", "technology stocks"),
        "mechanism": "Mega-cap and semiconductor news can create QQQ-specific leadership or concentration risk.",
        "channels": ("SOXX", "QQQ"),
    },
}

HIGH_IMPACT_TERMS = (
    "federal reserve", "fomc", "powell", "inflation", "treasury yield", "rate cut",
    "rate hike", "payroll", "jobs report", "gdp", "recession", "war", "iran",
    "oil", "opec", "sanction", "semiconductor", "export control",
)

YAHOO_QUERIES = (
    "Federal Reserve Treasury yields inflation",
    "US economy markets oil geopolitical risk",
    "SPY QQQ market news",
)

FED_FEEDS = (
    ("Federal Reserve monetary policy", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Federal Reserve speeches", "https://www.federalreserve.gov/feeds/speeches.xml"),
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        try:
            return parsedate_to_datetime(text).astimezone(UTC)
        except (TypeError, ValueError):
            return None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def classify(text: str) -> tuple[str | None, int]:
    haystack = f" {clean_text(text).lower()} "
    best_topic, best_hits = None, 0
    for topic, spec in TOPICS.items():
        hits = sum(1 for term in spec["terms"] if term in haystack)
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic, best_hits


def normalize_item(row: dict[str, Any], provider: str, observed: datetime) -> dict[str, Any] | None:
    headline = clean_text(row.get("headline") or row.get("title"))
    summary = clean_text(row.get("summary"))
    topic, hits = classify(f"{headline} {summary}")
    published = parse_time(row.get("published_at") or row.get("datetime") or row.get("providerPublishTime") or row.get("pubDate"))
    if not headline or not topic or not published:
        return None
    age_hours = (observed - published).total_seconds() / 3600
    if age_hours < -0.25 or age_hours > LOOKBACK_HOURS:
        return None
    source = clean_text(row.get("source") or row.get("publisher") or provider)
    url = clean_text(row.get("url") or row.get("link"))
    primary = provider == "Federal Reserve"
    high_hits = sum(1 for term in HIGH_IMPACT_TERMS if term in f" {headline.lower()} {summary.lower()} ")
    score = hits * 3 + min(high_hits, 3) * 2 + (5 if primary else 0) + (2 if source.lower() in {"reuters", "associated press"} else 0)
    return {
        "id": hashlib.sha256(f"{headline}|{url}".encode()).hexdigest()[:12],
        "headline": headline,
        "summary": summary[:500],
        "provider": provider,
        "source": source,
        "source_type": "primary" if primary else "secondary",
        "url": url,
        "published_at": published.isoformat(),
        "observed_at": observed.isoformat(),
        "age_hours": round(max(age_hours, 0), 2),
        "topic": topic,
        "impact": "high" if score >= 9 else "medium",
        "relevance_score": score,
        "mechanism": TOPICS[topic]["mechanism"],
        "market_channels": list(TOPICS[topic]["channels"]),
    }


def finnhub_key() -> str | None:
    try:
        return json.loads(FINNHUB_CREDENTIALS.read_text()).get("finnhub_api_key")
    except (OSError, json.JSONDecodeError):
        return None


def fetch_finnhub() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = finnhub_key()
    if not key:
        return [], {"status": "missing_key", "count": 0}
    response = requests.get(
        "https://finnhub.io/api/v1/news",
        params={"category": "general", "minId": 0},
        headers={"X-Finnhub-Token": key},
        timeout=25,
    )
    response.raise_for_status()
    rows = response.json()
    return (rows if isinstance(rows, list) else []), {"status": "ok", "count": len(rows) if isinstance(rows, list) else 0}


def fetch_yahoo() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, failures = [], 0
    for query in YAHOO_QUERIES:
        try:
            response = requests.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params={"q": query, "quotesCount": 0, "newsCount": 20},
                headers={"User-Agent": "Mozilla/5.0 BOT_NEXUS research"},
                timeout=20,
            )
            response.raise_for_status()
            rows.extend(response.json().get("news") or [])
        except (requests.RequestException, ValueError):
            failures += 1
    status = "ok" if failures == 0 else ("partial" if rows else "unavailable")
    return rows, {"status": status, "count": len(rows), "queries": len(YAHOO_QUERIES), "failures": failures}


def fetch_fed() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, failures = [], 0
    for feed_name, url in FED_FEEDS:
        try:
            response = requests.get(url, headers={"User-Agent": "BOT_NEXUS research"}, timeout=20)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                rows.append({
                    "title": item.findtext("title"),
                    "summary": item.findtext("description"),
                    "link": item.findtext("link"),
                    "pubDate": item.findtext("pubDate"),
                    "source": feed_name,
                })
        except (requests.RequestException, ET.ParseError):
            failures += 1
    status = "ok" if failures == 0 else ("partial" if rows else "unavailable")
    return rows, {"status": status, "count": len(rows), "feeds": len(FED_FEEDS), "failures": failures}


def deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = {}
    for item in sorted(items, key=lambda row: (row["relevance_score"], row["published_at"]), reverse=True):
        key = re.sub(r"[^a-z0-9]+", " ", item["headline"].lower()).strip()
        key = " ".join(key.split()[:14])
        if key not in kept:
            kept[key] = item
    return sorted(kept.values(), key=lambda row: (row["relevance_score"], row["published_at"]), reverse=True)


def main() -> int:
    observed = utcnow()
    all_items, health = [], {}
    for name, provider, fetcher in (
        ("finnhub", "Finnhub", fetch_finnhub),
        ("yahoo", "Yahoo Finance", fetch_yahoo),
        ("federal_reserve", "Federal Reserve", fetch_fed),
    ):
        try:
            rows, state = fetcher()
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            rows, state = [], {"status": "error", "count": 0, "error": type(exc).__name__}
        health[name] = state
        all_items.extend(item for row in rows if (item := normalize_item(row, provider, observed)))
    items = deduplicate(all_items)[:20]
    payload = {
        "schema_version": "macro-news-1.0",
        "generated_at": observed.isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "coverage": "Finnhub general market news, Yahoo Finance news discovery, and official Federal Reserve monetary-policy/speech RSS.",
        "source_health": health,
        "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(OUT)
    print(f"wrote {len(items)} ranked macro items -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
