#!/usr/bin/env python3
"""Pull the keyless official BLS release calendar into the Catalyst Brief schema."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "econ_calendar.json"
ET = ZoneInfo("America/New_York")
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
HIGH_TERMS = (
    "consumer price index", "employment situation", "producer price index",
    "job openings and labor turnover", "employment cost index",
    "productivity and costs", "import and export price indexes",
)


def unfold(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    for line in lines:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_dt(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(value.rstrip("Z"), fmt)
            if value.endswith("Z"):
                return dt.replace(tzinfo=timezone.utc).astimezone(ET)
            return dt.replace(tzinfo=ET)
        except ValueError:
            continue
    return None


def clean(value: str) -> str:
    return value.replace("\\,", ",").replace("\\n", " ").replace("\\;", ";").strip()


def parse_ics(text: str, now: datetime) -> list[dict]:
    events = []
    current = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                start = parse_dt(current.get("DTSTART", ""))
                title = clean(current.get("SUMMARY", "BLS release"))
                if start and now.date() - timedelta(days=1) <= start.date() <= now.date() + timedelta(days=45):
                    events.append({
                        "event": title,
                        "time": start.isoformat(),
                        "impact": "high" if any(term in title.lower() for term in HIGH_TERMS) else "medium",
                        "actual": None,
                        "estimate": None,
                        "prev": None,
                        "unit": None,
                        "source": "U.S. Bureau of Labor Statistics",
                        "source_url": current.get("URL") or BLS_ICS,
                    })
            current = None
            continue
        if current is None or ":" not in line:
            continue
        raw_key, value = line.split(":", 1)
        key = raw_key.split(";", 1)[0]
        if key in ("DTSTART", "SUMMARY", "URL"):
            current[key] = value
    events.sort(key=lambda row: row["time"])
    return events


def main() -> int:
    response = requests.get(BLS_ICS, headers={"User-Agent": "BOT_NEXUS Catalyst Brief contact@heff-tradingbot.local"}, timeout=25)
    response.raise_for_status()
    now = datetime.now(ET)
    items = parse_ics(response.text, now)
    payload = {
        "schema_version": "official-calendar-1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": "BLS scheduled releases only; Fed, Treasury, ISM, and company earnings require separate sources.",
        "source_url": BLS_ICS,
        "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(OUT)
    print(f"wrote {len(items)} official BLS events -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
