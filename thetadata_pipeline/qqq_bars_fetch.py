"""QQQ 1-minute RTH bar puller for the HEFF-SMC B1 indicator replay (BT-3).

Pulls real historical 1-min QQQ bars from Alpaca's stocks/bars endpoint (same
auth pattern/headers as greeks.py and wide_universe.py: credentials/
alpaca_key.txt + credentials/alpaca_secret.txt, feed=iex) for exactly the 62
QQQ session dates already present in the real backfill_60session manifest
(read-only reference, never written to here) -- this keeps the HEFF-SMC
replay on the SAME session calendar as the options data BT-2 needs to
fill/exit these trades against.

RTH only (09:30-16:00 ET). Extended-hours bars are NOT pulled: the .pine
file's own docstring documents ONH/ONL staying empty on an RTH-only chart as
an expected, non-bug configuration ("Extended-hours chart recommended...on
an RTH-only chart ONH/ONL simply stay empty (documented, not a bug)").
Pulling/reconciling overnight bars across the 16:00->09:30 boundary would add
real complexity for one liquidity-sweep source out of many; the resulting gap
is small and disclosed, not silently absorbed -- see heff_smc_engine.py's own
module 5 docstring and the B1 report's data-limitations section.

Isolation: writes only under data/thetadata/qqq_1min_bars/ -- never touches
backfill_60session/raw (read-only reference here, used only for its date
list).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "thetadata"
BACKFILL_MANIFEST_PATH = DATA / "backfill_60session" / "backfill_60session_manifest.json"

BARS_DIR = DATA / "qqq_1min_bars"
BARS_RAW_DIR = BARS_DIR / "raw"
BARS_MANIFEST_PATH = BARS_DIR / "qqq_1min_bars_manifest.json"

CREDENTIALS_DIR = ROOT / "credentials"
ALPACA_KEY_PATH = CREDENTIALS_DIR / "alpaca_key.txt"
ALPACA_SECRET_PATH = CREDENTIALS_DIR / "alpaca_secret.txt"

ET = ZoneInfo("America/New_York")

SYMBOL = "QQQ"
EXPECTED_RTH_BARS = 390  # 09:30-16:00 ET, full regular session, no early closes in the 62-session set

logger = logging.getLogger("thetadata_pkg.qqq_bars_fetch")


def _creds() -> tuple[str, str]:
    return ALPACA_KEY_PATH.read_text().strip(), ALPACA_SECRET_PATH.read_text().strip()


def _headers() -> dict:
    k, s = _creds()
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def list_target_sessions(manifest_path: Path = BACKFILL_MANIFEST_PATH, symbol: str = SYMBOL) -> list[str]:
    """Every date the real options backfill has for `symbol`, sorted -- the
    replay's session calendar is defined by this, not independently chosen,
    so the indicator-triggered signals and the BT-2 options data they feed
    into always cover the exact same days."""
    payload = json.loads(Path(manifest_path).read_text())
    dates = sorted({s["date"] for s in payload.get("sessions", []) if s["symbol"] == symbol})
    return dates


def fetch_day_bars(symbol: str, date: str, headers: dict, timeout: float = 30.0, max_retries: int = 3) -> pd.DataFrame:
    """One calendar day, 1-min bars, paginated, filtered to RTH 09:30-16:00
    ET. Pulls the full UTC day (00:00Z -> next 00:00Z) rather than a
    hand-computed ET->UTC offset, then filters in ET local time -- avoids a
    DST-offset bug, same approach as wide_universe.fetch_bars_batch."""
    start = f"{date}T00:00:00Z"
    end_date = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    end = f"{end_date}T00:00:00Z"
    rows: list = []
    page_token = None
    while True:
        params = {
            "symbols": symbol, "timeframe": "1Min", "start": start, "end": end,
            "feed": "iex", "adjustment": "raw", "limit": 10000, "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        resp = None
        for attempt in range(max_retries):
            try:
                resp = requests.get(
                    "https://data.alpaca.markets/v2/stocks/bars", headers=headers, params=params, timeout=timeout,
                )
                if resp.status_code == 200:
                    break
            except requests.RequestException:
                resp = None
            time.sleep(1.5 * (attempt + 1))
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "no_response"
            raise RuntimeError(f"Alpaca bars fetch failed for {symbol} {date}: status={code}")
        payload = resp.json()
        rows.extend(payload.get("bars", {}).get(symbol, []))
        page_token = payload.get("next_page_token")
        if not page_token:
            break

    if not rows:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v", "n", "vw"])

    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
    df = df.set_index("t").between_time("09:30", "16:00").reset_index()
    df = df.sort_values("t").drop_duplicates(subset="t").reset_index(drop=True)
    return df


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def fetch_and_persist_all(
    dates: list = None, symbol: str = SYMBOL, raw_dir: Path = BARS_RAW_DIR,
    manifest_path: Path = BARS_MANIFEST_PATH, sleep_between: float = 0.3,
) -> dict:
    """Fetches every session in `dates` (default: the full 62-session QQQ
    list from the real options backfill manifest) and persists one parquet
    file per session under raw_dir/<symbol>/<date>/bars.parquet, plus a
    manifest recording real row counts and any gap from the expected 390
    RTH minutes -- never silently absorbed."""
    dates = dates if dates is not None else list_target_sessions()
    headers = _headers()
    sessions = []
    for date in dates:
        df = fetch_day_bars(symbol, date, headers)
        out_dir = raw_dir / symbol / date
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "bars.parquet"
        df.to_parquet(out_path, index=False)
        n = len(df)
        gap = EXPECTED_RTH_BARS - n
        sessions.append({
            "symbol": symbol, "date": date, "rows": n,
            "expected_rows": EXPECTED_RTH_BARS, "gap_from_expected": gap,
            "first_ts": df["t"].iloc[0].isoformat() if n else None,
            "last_ts": df["t"].iloc[-1].isoformat() if n else None,
        })
        logger.info("QQQ 1m bars %s: %d rows (expected ~%d, gap %d)", date, n, EXPECTED_RTH_BARS, gap)
        time.sleep(sleep_between)

    manifest = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbol": symbol, "source": "alpaca_v2_stocks_bars_1min_iex_rth_only",
        "extended_hours": False,
        "sessions": sessions,
        "overall": {
            "sessions_total": len(sessions),
            "sessions_with_gap": sum(1 for s in sessions if s["gap_from_expected"] != 0),
            "total_rows": sum(s["rows"] for s in sessions),
        },
    }
    _atomic_write_json(manifest_path, manifest)
    return manifest


def load_session_bars(symbol: str, date: str, raw_dir: Path = BARS_RAW_DIR) -> pd.DataFrame:
    path = raw_dir / symbol / date / "bars.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v", "n", "vw"])
    return pd.read_parquet(path)


def load_all_bars(symbol: str = SYMBOL, dates: list = None, raw_dir: Path = BARS_RAW_DIR) -> pd.DataFrame:
    """Concatenates every session's bars, in date order, into one continuous
    frame -- the shape heff_smc_replay.py needs to run one continuous
    stateful replay across all 62 sessions (structure/FVG/OB/liquidity/HTF
    state persists across session boundaries, matching the live indicator's
    own `var`-scoped Pine state, which never resets intraday-to-intraday)."""
    dates = dates if dates is not None else list_target_sessions()
    frames = [load_session_bars(symbol, d, raw_dir) for d in dates]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["t", "o", "h", "l", "c", "v", "n", "vw"])
    out = pd.concat(frames, ignore_index=True).sort_values("t").reset_index(drop=True)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = fetch_and_persist_all()
    print(json.dumps(result["overall"], indent=2))
