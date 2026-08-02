"""Persistent minute detector for VARIANT_B_NO_SWEEP.

Replaces the */3 detector cron. The cron refetched SIX sessions every tick
(734 ms of network) and re-ran the full replay (406 ms) -- 1.20 s per tick,
failing the p95<1s target purely on redundant I/O, since five of those six
sessions are immutable history.

This keeps history in memory and fetches only the current session's new
bars. `run_replay` itself is UNCHANGED and is still handed a full
6-session continuous series, so the replay computation is identical.

PARITY IS NOT FREE. The replay function is unchanged, but the INPUT ASSEMBLY
and the cache behavior are new: history is now concatenated from a cache
rather than from six fresh fetches, and today's frame is appended
incrementally. `build_continuous_1min_series` reindexes onto a 390-minute
grid and forward-fills gaps, so a different assembly path could in principle
produce a different series. Cache parity is therefore a TESTED property
(see variant_b_detector_parity.py), not an assumed one.

SIGNAL FEED IDENTITY, frozen for the forward window: **alpaca_iex**. Bars
come from Alpaca with feed=iex -- one venue, not the consolidated SIP tape,
and not TradingView's chart feed. The 162-session backtest used the same
fetch path, so backtest and live are internally consistent; that is a
different and weaker claim than parity with a consolidated feed. Every
signal carries signal_feed so no downstream report can lose this.

Exchange time comes from smc/calendar.py (a real cached exchange calendar
with early-close support), never from hard-coded UTC hours -- DST alone
makes fixed UTC offsets wrong twice a year.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from smc import calendar as smc_calendar
from thetadata_pipeline.heff_smc_engine import HeffSmcConfig
from thetadata_pipeline.heff_smc_replay import build_continuous_1min_series, run_replay

logger = logging.getLogger("smc.detector")

ET = ZoneInfo("America/New_York")

SIGNAL_FEED = "alpaca_iex"
BAR_TIMEFRAME = "1Min"
ROLLING_SESSIONS = 6            # unchanged from the cron detector
BAR_COLUMNS = ("t", "o", "h", "l", "c", "v")


def config_checksum(config: HeffSmcConfig) -> str:
    """Stable hash of the detector configuration, recorded on every signal so
    a forward result can never be silently attributed to different params."""
    payload = json.dumps(dataclasses.asdict(config), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def series_checksum(df: pd.DataFrame) -> str:
    """Checksum of the exact bar series a replay consumed."""
    if df is None or df.empty:
        return "empty"
    cols = [c for c in BAR_COLUMNS if c in df.columns]
    blob = pd.util.hash_pandas_object(df[cols], index=False).values.tobytes()
    return hashlib.sha256(blob).hexdigest()[:16]


class BarValidationError(ValueError):
    pass


def validate_bars(df: pd.DataFrame, *, expect_session: Optional[str] = None) -> dict:
    """Ordering, duplicates, gaps and session-boundary checks. Returns a
    report; raises only on structural corruption we must never process."""
    if df is None or df.empty:
        return {"rows": 0, "duplicates": 0, "out_of_order": 0, "sessions": []}
    if "t" not in df.columns:
        raise BarValidationError("bar frame has no 't' column")
    ts = pd.to_datetime(df["t"], utc=True, errors="coerce")
    if ts.isna().any():
        raise BarValidationError(f"{int(ts.isna().sum())} unparseable bar timestamps")
    out_of_order = int((ts.diff().dropna() < pd.Timedelta(0)).sum())
    duplicates = int(ts.duplicated().sum())
    sessions = sorted({d.astimezone(ET).date().isoformat() for d in ts})
    if expect_session and sessions and sessions != [expect_session]:
        raise BarValidationError(
            f"expected only session {expect_session}, got {sessions}")
    return {"rows": len(df), "duplicates": duplicates,
            "out_of_order": out_of_order, "sessions": sessions}


@dataclasses.dataclass(frozen=True)
class DetectedSignal:
    """One emitted signal, carrying every provenance field heff requires."""
    signal_key: str
    session: str
    bar_index: int
    side: str
    trigger: str
    score: float
    price: float
    bar_time_et: str
    # --- provenance ---
    signal_feed: str
    bar_timeframe: str
    bar_timestamp: str            # the confirmed bar this signal closed on
    bar_received_ts: str          # when this process received that bar
    confirmed: bool
    detector_config_checksum: str
    source_series_checksum: str
    revised_after_processing: bool
    detected_ts: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class DetectorCursor:
    """Durable position. `emitted` is what makes restart-dedup work: a signal
    already emitted must never be emitted again, because downstream that
    means a second order."""
    last_processed_bar_ts: Optional[str] = None
    emitted: set = dataclasses.field(default_factory=set)
    revised_bars: list = dataclasses.field(default_factory=list)

    def to_json(self) -> dict:
        return {"last_processed_bar_ts": self.last_processed_bar_ts,
                "emitted": sorted(self.emitted),
                "revised_bars": self.revised_bars[-200:]}

    @classmethod
    def from_json(cls, payload: dict) -> "DetectorCursor":
        return cls(last_processed_bar_ts=payload.get("last_processed_bar_ts"),
                   emitted=set(payload.get("emitted") or []),
                   revised_bars=list(payload.get("revised_bars") or []))


class PersistentDetector:
    """Warm in-memory history + incremental current-session bars.

    `fetch_session_bars(session_date_iso) -> DataFrame` is injected, so the
    same object runs live (Alpaca) or over the cached historical dataset
    (parity testing) with no code differences.
    """

    def __init__(self, fetch_session_bars: Callable, cursor_path: Optional[Path] = None,
                 config: HeffSmcConfig = HeffSmcConfig(),
                 rolling_sessions: int = ROLLING_SESSIONS,
                 clock: Callable = time.monotonic):
        self.fetch = fetch_session_bars
        self.cursor_path = Path(cursor_path) if cursor_path else None
        self.config = config
        self.rolling_sessions = rolling_sessions
        self.clock = clock

        self.config_checksum = config_checksum(config)
        self._history: dict = {}          # session iso -> DataFrame (immutable)
        self._today_session: Optional[str] = None
        self._today: Optional[pd.DataFrame] = None
        self._bar_received: dict = {}     # bar ts iso -> received wall clock
        self.cursor = DetectorCursor()
        self.synchronized = False
        self.last_runtime_ms: Optional[float] = None
        self.last_series_checksum: Optional[str] = None

    # ------------------------------------------------------------- startup
    def load_cursor(self) -> DetectorCursor:
        if self.cursor_path and self.cursor_path.exists():
            try:
                self.cursor = DetectorCursor.from_json(
                    json.loads(self.cursor_path.read_text()))
            except (OSError, ValueError) as e:
                # A corrupt cursor must NOT silently become an empty one:
                # that would re-emit every signal of the session.
                raise BarValidationError(
                    f"detector cursor at {self.cursor_path} is unreadable ({e}); "
                    "refusing to start with an empty cursor, which would re-emit "
                    "already-processed signals") from e
        return self.cursor

    def save_cursor(self) -> None:
        if not self.cursor_path:
            return
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cursor.to_json(), indent=2))
        tmp.replace(self.cursor_path)     # atomic

    def warmup(self, session: str, prior_sessions: list) -> dict:
        """Loads the immutable prior sessions once, plus whatever of today
        already exists. Prior sessions are never refetched afterwards."""
        self.load_cursor()
        report = {"prior": {}, "today": None}
        for s in prior_sessions[-(self.rolling_sessions - 1):]:
            df = self.fetch(s)
            report["prior"][s] = validate_bars(df, expect_session=s)
            self._history[s] = df
        self._today_session = session
        today_df = self.fetch(session)
        report["today"] = validate_bars(today_df, expect_session=session)
        self._today = today_df if today_df is not None else pd.DataFrame()
        self._stamp_received(self._today)
        self.synchronized = True
        return report

    def _stamp_received(self, df: Optional[pd.DataFrame]) -> None:
        if df is None or df.empty:
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for t in pd.to_datetime(df["t"], utc=True):
            self._bar_received.setdefault(t.isoformat(), now)

    # ------------------------------------------------------ session timing
    @staticmethod
    def next_minute_boundary(now_et: dt.datetime) -> dt.datetime:
        """Next wall-clock minute boundary in ET. A bar covering minute M is
        only complete once M+1 has begun."""
        return (now_et.replace(second=0, microsecond=0) + dt.timedelta(minutes=1))

    @staticmethod
    def session_is_open(now_et: dt.datetime) -> bool:
        """Exchange calendar, never hard-coded UTC hours -- DST alone makes a
        fixed offset wrong twice a year, and early closes make it wrong on
        specific dates."""
        try:
            sched = smc_calendar.session_schedule(now_et.date())
        except Exception:  # noqa: BLE001
            return False
        if sched is None or not getattr(sched, "is_trading_day", True):
            return False
        open_dt = getattr(sched, "open_dt", None)
        close_dt = getattr(sched, "close_dt", None)
        if open_dt is None or close_dt is None:
            return False
        return open_dt <= now_et < close_dt

    # ------------------------------------------------------- incremental
    def ingest_today(self, df: pd.DataFrame) -> dict:
        """Appends only genuinely NEW bars for the current session, and flags
        any previously-processed bar whose values changed (a revision).

        Provider quirks handled here rather than downstream: a duplicate
        minute is ignored, an OLDER last bar than we already hold is ignored
        (providers sometimes return a stale tail), and a multi-minute gap
        after downtime simply appends every missing minute at once."""
        result = {"appended": 0, "duplicates": 0, "revised": 0, "stale": 0}
        if df is None or df.empty:
            return result
        validate_bars(df, expect_session=self._today_session)
        incoming = df.copy()
        incoming["t"] = pd.to_datetime(incoming["t"], utc=True)
        incoming = incoming.sort_values("t").drop_duplicates(subset="t", keep="last")

        if self._today is None or self._today.empty:
            self._today = incoming.reset_index(drop=True)
            result["appended"] = len(incoming)
            self._stamp_received(self._today)
            return result

        current = self._today.copy()
        current["t"] = pd.to_datetime(current["t"], utc=True)
        known = set(current["t"])

        # Revision detection on bars we have ALREADY processed.
        cutoff = (pd.Timestamp(self.cursor.last_processed_bar_ts)
                  if self.cursor.last_processed_bar_ts else None)
        merged = incoming.merge(current, on="t", suffixes=("_new", "_old"))
        for _, row in merged.iterrows():
            changed = any(
                pd.notna(row.get(f"{c}_new")) and pd.notna(row.get(f"{c}_old"))
                and float(row[f"{c}_new"]) != float(row[f"{c}_old"])
                for c in ("o", "h", "l", "c", "v"))
            if changed:
                result["revised"] += 1
                was_processed = cutoff is not None and row["t"] <= cutoff
                self.cursor.revised_bars.append(
                    {"bar_ts": row["t"].isoformat(),
                     "after_processing": bool(was_processed),
                     "noticed_ts": dt.datetime.now(dt.timezone.utc).isoformat()})

        new_rows = incoming[~incoming["t"].isin(known)]
        if len(new_rows) == 0:
            result["duplicates"] = len(incoming)
            if len(incoming) and incoming["t"].max() < current["t"].max():
                result["stale"] = 1
            return result

        self._today = (pd.concat([current, new_rows], ignore_index=True)
                       .sort_values("t").drop_duplicates(subset="t", keep="last")
                       .reset_index(drop=True))
        result["appended"] = len(new_rows)
        self._stamp_received(new_rows)
        return result

    def continuous_series(self) -> pd.DataFrame:
        frames = [self._history[s] for s in sorted(self._history) if self._history[s] is not None]
        if self._today is not None and not self._today.empty:
            frames.append(self._today)
        if not frames:
            return pd.DataFrame(columns=list(BAR_COLUMNS))
        raw = pd.concat(frames, ignore_index=True)
        raw["t"] = pd.to_datetime(raw["t"], utc=True)
        return (raw.sort_values("t").drop_duplicates(subset="t", keep="last")
                .reset_index(drop=True))

    # ------------------------------------------------------------- detect
    def detect(self) -> list:
        """Runs the UNCHANGED replay over the assembled series and returns
        only signals not already emitted. Dedup is by deterministic
        signal_key persisted in the cursor, so a restart cannot re-emit."""
        t0 = self.clock()
        raw = self.continuous_series()
        if raw.empty:
            return []
        continuous = build_continuous_1min_series(raw)
        self.last_series_checksum = series_checksum(continuous)
        events, _diag = run_replay(continuous, self.config)
        self.last_runtime_ms = (self.clock() - t0) * 1000.0

        out = []
        for e in events:
            if self._today_session and e["session"] != self._today_session:
                continue
            # Dedup key is BAR TIME, not bar_index. bar_index is relative to
            # whatever series was replayed, so it shifts by 390 per session
            # whenever the rolling window composition changes (a holiday, a
            # short warmup at session start, a different ROLLING_SESSIONS).
            # Keying on it means the SAME signal can get a different key after
            # a restart and be emitted twice -- i.e. a second order. Bar time
            # is stable across every one of those. Found by the cache-parity
            # test; the legacy cron detector has the same latent hazard.
            key = f"{e['session']}:{e['time']}:{e['side']}"
            if key in self.cursor.emitted:
                continue
            bar_ts = self._bar_ts_for(e)
            out.append(DetectedSignal(
                signal_key=key, session=e["session"], bar_index=int(e["bar_index"]),
                side=e["side"], trigger=e["trigger"], score=float(e["score"]),
                price=float(e["price"]), bar_time_et=e["time"],
                signal_feed=SIGNAL_FEED, bar_timeframe=BAR_TIMEFRAME,
                bar_timestamp=bar_ts or "",
                bar_received_ts=self._bar_received.get(bar_ts or "", ""),
                confirmed=True,
                detector_config_checksum=self.config_checksum,
                source_series_checksum=self.last_series_checksum,
                revised_after_processing=any(
                    r["bar_ts"] == bar_ts and r["after_processing"]
                    for r in self.cursor.revised_bars),
                detected_ts=dt.datetime.now(dt.timezone.utc).isoformat()))
            self.cursor.emitted.add(key)
        if self._today is not None and not self._today.empty:
            self.cursor.last_processed_bar_ts = str(
                pd.to_datetime(self._today["t"], utc=True).max())
        self.save_cursor()
        return out

    def _bar_ts_for(self, event: dict) -> Optional[str]:
        try:
            naive = dt.datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S")
            return naive.replace(tzinfo=ET).astimezone(dt.timezone.utc).isoformat()
        except (KeyError, ValueError):
            return None

    def health(self) -> dict:
        return {
            "signal_feed": SIGNAL_FEED,
            "bar_timeframe": BAR_TIMEFRAME,
            "synchronized": self.synchronized,
            "session": self._today_session,
            "history_sessions": sorted(self._history),
            "today_bars": 0 if self._today is None else len(self._today),
            "cursor_last_bar": self.cursor.last_processed_bar_ts,
            "emitted_count": len(self.cursor.emitted),
            "revised_bars": len(self.cursor.revised_bars),
            "detector_config_checksum": self.config_checksum,
            "source_series_checksum": self.last_series_checksum,
            "last_runtime_ms": self.last_runtime_ms,
        }
