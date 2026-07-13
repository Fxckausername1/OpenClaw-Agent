#!/usr/bin/env python3
"""Collect immutable point-in-time Alpaca premarket datasets for forward research."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import date, datetime, time as clock, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DEFAULT_OUT = DATA / "research/premarket_forward"
UNIVERSE_PATH = DATA / "wide_universe.json"
KEY_PATH = ROOT / "credentials/alpaca_key.txt"
SECRET_PATH = ROOT / "credentials/alpaca_secret.txt"
PROTOCOL_PATH = ROOT / "PREMARKET_FORWARD_PROTOCOL.md"
BASE_URL = "https://data.alpaca.markets/v2/stocks"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
PROTOCOL_VERSION = "premarket-forward-2026-07-13.4"
FORWARD_GATE = {
    "minimum_sealed_market_days": 120,
    "minimum_eligible_observations": 250,
    "both_chronological_half_means_positive_at_6bp": True,
    "daily_block_bootstrap_lower_95pct_positive_at_6bp": True,
    "mean_nonnegative_at_12bp": True,
    "minimum_quote_coverage": 0.90,
    "maximum_required_field_missing_rate": 0.10,
    "requires_disjoint_future_sample": True,
}
REFERENCE_SYMBOLS = ("SPY", "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def session_bounds(session_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(session_date, clock(4, 0), tzinfo=ET)
    end = datetime.combine(session_date, clock(9, 30), tzinfo=ET)
    return start, end


def load_universe(path: Path = UNIVERSE_PATH) -> tuple[list[str], dict]:
    raw = json.loads(path.read_text())
    source = raw.get("symbols", raw) if isinstance(raw, dict) else raw
    if not isinstance(source, list):
        raise ValueError("universe must be a list or object with a symbols list")
    symbols = sorted({str(symbol).strip().upper() for symbol in source if str(symbol).strip()} | set(REFERENCE_SYMBOLS))
    if not symbols:
        raise ValueError("universe is empty")
    metadata = raw if isinstance(raw, dict) else {"source": str(path)}
    return symbols, metadata


def universe_hash(symbols: list[str]) -> str:
    return sha256_bytes(("\n".join(symbols) + "\n").encode())


def credentials() -> dict[str, str]:
    if not KEY_PATH.exists() or not SECRET_PATH.exists():
        raise RuntimeError("Alpaca credentials are missing")
    return {
        "APCA-API-KEY-ID": KEY_PATH.read_text().strip(),
        "APCA-API-SECRET-KEY": SECRET_PATH.read_text().strip(),
    }


def request_json(session: requests.Session, url: str, headers: dict, params: dict, retries: int = 4) -> tuple[dict, str | None]:
    last_error = None
    for attempt in range(retries):
        try:
            response = session.get(url, headers=headers, params=params, timeout=45)
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Alpaca request failed: {last_error}") from exc
        request_id = response.headers.get("X-Request-ID")
        if response.status_code == 200:
            return response.json(), request_id
        try:
            message = response.json().get("message", response.text[:300])
        except Exception:
            message = response.text[:300]
        if response.status_code == 429 and attempt + 1 < retries:
            wait = int(response.headers.get("Retry-After", 2 ** attempt))
            time.sleep(max(1, min(wait, 30)))
            continue
        raise RuntimeError(f"Alpaca HTTP {response.status_code}: {message}; request_id={request_id}")
    raise RuntimeError(f"Alpaca request failed: {last_error}")


def chunks(items: list[str], size: int):
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def fetch_bars(
    session: requests.Session,
    headers: dict,
    symbols: list[str],
    feed: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict], list[str]]:
    records = []
    request_ids = []
    for batch in chunks(symbols, 100):
        page_token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": timeframe,
                "start": iso_utc(start),
                "end": iso_utc(end),
                "feed": feed,
                "adjustment": "raw",
                "limit": 10000,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            body, request_id = request_json(session, f"{BASE_URL}/bars", headers, params)
            if request_id:
                request_ids.append(request_id)
            for symbol, bars in (body.get("bars") or {}).items():
                for bar in bars:
                    records.append({
                        "record_type": "bar" if timeframe == "1Min" else "daily_bar",
                        "feed": feed,
                        "symbol": symbol,
                        "bar": bar,
                    })
            page_token = body.get("next_page_token")
            if not page_token:
                break
    return records, request_ids


def fetch_snapshots(session: requests.Session, headers: dict, symbols: list[str], feed: str) -> tuple[list[dict], list[str]]:
    records = []
    request_ids = []
    for batch in chunks(symbols, 100):
        body, request_id = request_json(
            session,
            f"{BASE_URL}/snapshots",
            headers,
            {"symbols": ",".join(batch), "feed": feed},
        )
        if request_id:
            request_ids.append(request_id)
        for symbol, snapshot in body.items():
            records.append({"record_type": "snapshot", "feed": feed, "symbol": symbol, "snapshot": snapshot})
    return records, request_ids


def atomic_exclusive_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp.{os.getpid()}"
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    try:
        os.link(temporary, path)
    except FileExistsError as exc:
        raise RuntimeError(f"sealed path already exists: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def verify_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    data_path = path.parent / manifest["data_file"]
    actual = sha256_bytes(data_path.read_bytes())
    if actual != manifest["data_sha256"]:
        raise RuntimeError(f"hash mismatch: {data_path}")
    return manifest


def seal_dataset(directory: Path, stem: str, records: list[dict], manifest: dict) -> dict:
    data_path = directory / f"{stem}.jsonl"
    manifest_path = directory / f"{stem}.manifest.json"
    if data_path.exists() or manifest_path.exists():
        if data_path.exists() and manifest_path.exists():
            existing = verify_manifest(manifest_path)
            return {"status": "already_sealed", "manifest": str(manifest_path), "records": existing["record_count"]}
        raise RuntimeError(f"incomplete sealed pair exists for {stem}; manual audit required")
    payload = "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records).encode()
    manifest = {
        **manifest,
        "data_file": data_path.name,
        "data_sha256": sha256_bytes(payload),
        "record_count": len(records),
        "sealed": True,
    }
    atomic_exclusive_write(data_path, payload)
    atomic_exclusive_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    verify_manifest(manifest_path)
    return {"status": "sealed", "manifest": str(manifest_path), "records": len(records)}


def validate_schedule(mode: str, session_date: date, now: datetime, allow_outside: bool) -> None:
    if allow_outside:
        return
    if session_date != now.date():
        raise RuntimeError("session date must be today unless --allow-outside-window is used")
    if session_date.weekday() >= 5:
        raise RuntimeError("scheduled collection is disabled on weekends")
    current = now.time().replace(tzinfo=None)
    if mode == "capture" and not (clock(9, 10) <= current < clock(9, 20)):
        raise RuntimeError("capture must run from 09:10 through 09:19 ET")
    if mode == "sip-backfill" and not (clock(16, 25) <= current < clock(16, 40)):
        raise RuntimeError("SIP backfill must run from 16:25 through 16:39 ET")


def collect(mode: str, session_date: date, symbols: list[str], universe_meta: dict, out_root: Path, dry_run: bool) -> dict:
    now = datetime.now(ET)
    start, market_open = session_bounds(session_date)
    session = requests.Session()
    headers = credentials()
    collected_at = iso_utc(now)
    request_ids = []
    if mode == "capture":
        feed = "iex"
        end = min(now.replace(second=0, microsecond=0) - timedelta(minutes=1), market_open - timedelta(microseconds=1))
        records, ids = fetch_bars(session, headers, symbols, feed, "1Min", start, end)
        request_ids.extend(ids)
        snapshots, ids = fetch_snapshots(session, headers, symbols, feed)
        records.extend(snapshots)
        request_ids.extend(ids)
        delayed_snapshots, ids = fetch_snapshots(session, headers, symbols, "delayed_sip")
        records.extend(delayed_snapshots)
        request_ids.extend(ids)
        delayed_sip_end = min(now.replace(second=0, microsecond=0) - timedelta(minutes=16), market_open - timedelta(microseconds=1))
        delayed_sip, ids = fetch_bars(session, headers, symbols, "sip", "1Min", start, delayed_sip_end)
        records.extend(delayed_sip)
        request_ids.extend(ids)
        feed = "iex+delayed_sip"
        stem = "iex_capture_0915"
        decision_available_preopen = True
    elif mode == "sip-backfill":
        feed = "sip"
        session_close = datetime.combine(session_date, clock(16, 0), tzinfo=ET)
        end = session_close - timedelta(microseconds=1)
        records, ids = fetch_bars(session, headers, symbols, feed, "1Min", start, end)
        request_ids.extend(ids)
        daily_start = datetime.combine(session_date - timedelta(days=10), clock(0, 0), tzinfo=ET)
        daily_end = start
        daily, ids = fetch_bars(session, headers, symbols, feed, "1Day", daily_start, daily_end)
        records.extend(daily)
        request_ids.extend(ids)
        stem = "sip_session_backfill_1600"
        decision_available_preopen = False
    else:
        raise ValueError(mode)

    records.sort(key=lambda record: (
        record["record_type"],
        record["symbol"],
        (record.get("bar") or {}).get("t", ""),
    ))
    for record in records:
        record["collected_at"] = collected_at
        record["protocol_version"] = PROTOCOL_VERSION
    universe_payload = ("\n".join(symbols) + "\n").encode()
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "mode": mode,
        "feed": feed,
        "feeds": ["iex", "delayed_sip", "sip"] if mode == "capture" else ["sip"],
        "session_date": session_date.isoformat(),
        "collected_at": collected_at,
        "requested_start": iso_utc(start),
        "requested_end": iso_utc(end),
        "delayed_sip_end": iso_utc(delayed_sip_end) if mode == "capture" else None,
        "decision_available_preopen": decision_available_preopen,
        "regular_session_included": mode == "sip-backfill",
        "outcome_available_postclose": mode == "sip-backfill",
        "forward_gate": FORWARD_GATE,
        "universe_source": universe_meta.get("source", str(UNIVERSE_PATH)),
        "universe_built_at": universe_meta.get("built_at"),
        "universe_count": len(symbols),
        "universe_sha256": sha256_bytes(universe_payload),
        "symbols": symbols,
        "alpaca_request_ids": request_ids,
        "collector_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "protocol_document_sha256": sha256_bytes(PROTOCOL_PATH.read_bytes()) if PROTOCOL_PATH.exists() else None,
        "bar_records": sum(record["record_type"] == "bar" for record in records),
        "daily_bar_records": sum(record["record_type"] == "daily_bar" for record in records),
        "snapshot_records": sum(record["record_type"] == "snapshot" for record in records),
        "notes": (
            "IEX snapshots/latest bars plus delayed-SIP snapshots and 16-minute-delayed SIP premarket bars were sealed before the open for forward feature construction."
            if mode == "capture"
            else "SIP premarket and regular-session data was collected after the close for outcome scoring and audit; it cannot rewrite the pre-open capture."
        ),
    }
    if dry_run:
        return {"status": "dry_run", "records": len(records), "manifest_preview": manifest}
    return seal_dataset(out_root / session_date.isoformat(), stem, records, manifest)


def verify_all(out_root: Path) -> dict:
    manifests = sorted(out_root.glob("*/*.manifest.json"))
    verified = [verify_manifest(path) for path in manifests]
    return {"status": "verified", "manifests": len(verified), "records": sum(item["record_count"] for item in verified)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["capture", "sip-backfill", "verify"], required=True)
    parser.add_argument("--session-date")
    parser.add_argument("--symbols", help="comma-separated probe subset")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--allow-outside-window", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.mode == "verify":
        result = verify_all(args.output_root)
    else:
        now = datetime.now(ET)
        session_date = date.fromisoformat(args.session_date) if args.session_date else now.date()
        validate_schedule(args.mode, session_date, now, args.allow_outside_window)
        symbols, metadata = load_universe()
        if args.symbols:
            requested = {symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()}
            symbols = [symbol for symbol in symbols if symbol in requested]
            missing = requested - set(symbols)
            if missing:
                symbols.extend(sorted(missing))
            symbols = sorted(set(symbols))
            metadata = {"source": "explicit_probe_subset", "built_at": None}
        result = collect(args.mode, session_date, symbols, metadata, args.output_root, args.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
