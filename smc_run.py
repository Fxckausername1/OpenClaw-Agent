"""Collapsed SMC pipeline entry point (Phase 4) -- detection, selection, gating,
submission and supervision in ONE process, replacing three chained */3 cron hops.

Measured problem this replaces (2026-07-31): detector, selector and executor were
three separate `*/3` cron entries, so a signal waited for up to three independent
cron boundaries before an order was sent. Real signal-to-submit latency was
227.5-407.3s (median 352.85s) against a backtest that assumes 3.0s
(bt2_fills.FillConfig.reaction_latency_seconds). This runs the stages back-to-back
with no wait, records every timestamp in the chain, and refuses any signal that is
still too old at submission time.

DISARMED BY DEFAULT. `--arm` is required to place any order, and the P0 repair
ships with the cron wrapper NOT passing it. Re-arming is a deliberate, approved
act -- see the canary plan.

Usage:
  ./venv/bin/python smc_run.py --once                 # one dry-run signal cycle
  ./venv/bin/python smc_run.py --supervise 60         # 60s of dry-run supervision
  ./venv/bin/python smc_run.py --once --supervise 60 --arm   # ARMED (needs approval)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import live_heff_smc_detector as detector
import live_heff_smc_selector as selector
import options_orchestrator as oo
from smc.broker import AlpacaBroker
from smc.calendar import refresh_calendar
from smc.config import STRATEGY_ID, load_config
from smc.pipeline import SmcPipeline
from smc.state import SmcStateError, SmcStateStore
from thetadata_pipeline.bt2_exits import ExitConfig
from thetadata_pipeline.bt2_schemas import DIRECTION_TO_RIGHT

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "logs" / "smc_run.log"
DASHBOARD_DB = ROOT / "data" / "options_eval.db"

logger = logging.getLogger("smc_run")


def _occ_symbol(underlying: str, expiration: str, strike: float, right: str) -> str:
    exp = dt.date.fromisoformat(expiration)
    return f"{underlying}{exp:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def _signal_ts_utc(trigger: dict) -> str:
    """The trigger's `time` is the 1-minute bar's ET timestamp (naive). Latency and
    staleness are measured from the bar CLOSE, so add one minute -- the bar is not
    actionable until it has closed."""
    naive = dt.datetime.strptime(trigger["time"], "%Y-%m-%d %H:%M:%S")
    bar_close_et = naive.replace(tzinfo=ET) + dt.timedelta(minutes=1)
    return bar_close_et.astimezone(dt.timezone.utc).isoformat()


def make_detect_fn(store: SmcStateStore):
    """Runs the SAME validated replay the standalone detector uses, then filters to
    signals this process has not already created a position for. Dedup is the state
    store's UNIQUE(signal_key), not a JSON cursor -- so a restart cannot re-trade."""
    def detect():
        result = detector.run_detection_tick()
        if result.get("status") != "ok":
            return []
        triggers = []
        if not detector.TRIGGERS_PATH.exists():
            return []
        today = dt.datetime.now(ET).date().isoformat()
        for line in detector.TRIGGERS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("session") != today:
                continue
            key = f"{rec['session']}:{rec['bar_index']}:{rec['side']}"
            if store.get_position_by_signal(key) is not None:
                continue  # already handled -- never re-submit
            rec["signal_key"] = key
            rec["signal_ts"] = _signal_ts_utc(rec)
            triggers.append(rec)
        return triggers
    return detect


def select_fn(trigger: dict):
    """Real live chain -> the SAME bt2_selector.select_contract + live config the
    standalone selector uses. Returns the shape SmcPipeline expects, or None."""
    direction = selector.SIDE_TO_DIRECTION[trigger["side"]]
    right = DIRECTION_TO_RIGHT[direction]
    book = selector.fetch_live_book(trigger["ticker"], right)
    if book is None or book.empty:
        return None
    from thetadata_pipeline.bt2_selector import select_contract
    result = select_contract(book, right, dt.datetime.now(ET), selector.LIVE_SELECTOR_CONFIG)
    if not result.found:
        logger.info("no contract for %s: %s (%d candidates)", trigger["signal_key"],
                    result.reason, result.candidates_checked)
        return None
    c = result.contract
    return {
        "occ": _occ_symbol(trigger["ticker"], c["expiration"], c["strike"], c["right"]),
        "ask": c["ask"], "right": c["right"], "strike": c["strike"],
        "expiration": c["expiration"], "qty": 1,
    }


def dashboard_sync_fn(*, position_id, outcome, occ):
    """REPORTING ONLY. Mirrors the entry into options_eval.db's trades_ledger so the
    existing S1-S10 dashboard panel shows SMC_TRIANGLE with no frontend change.
    SmcPipeline wraps this in try/except -- a failure here is recorded and ignored,
    never allowed to affect the trading path."""
    from options_eval import TradeStatus, connect, init_db, record_open
    legs = {
        "legs": [{"occ": occ, "side": "BUY", "type": occ[-9], "strike": int(occ[-8:]) / 1000.0}],
        "entry_debit": outcome.fill_price, "qty": outcome.filled_qty,
        "max_loss": round((outcome.fill_price or 0) * (outcome.filled_qty or 0) * 100, 2),
        "kind": "debit", "structure": "single_leg_long",
    }
    conn = connect(DASHBOARD_DB)
    try:
        init_db(conn)
        record_open(conn, position_id, STRATEGY_ID, legs, "UNKNOWN",
                    initial_risk=max(legs["max_loss"], 0.01), status=TradeStatus.OPEN)
    finally:
        conn.close()


def _refresh_calendar(broker) -> None:
    """Keep the real session schedule (including early closes) current. Best-effort:
    smc.calendar fails CLOSED on a stale/absent schedule, so a refresh failure makes
    the supervisor flatten earlier, never later."""
    try:
        today = dt.datetime.now(ET).date()
        refresh_calendar(broker.get_calendar, today - dt.timedelta(days=3),
                         today + dt.timedelta(days=21))
    except Exception as e:  # noqa: BLE001
        logger.warning("calendar refresh failed (schedule will fail closed): %s", e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true",
                    help="place real (paper) orders; omitted = DRY RUN")
    ap.add_argument("--once", action="store_true", help="run one signal cycle")
    ap.add_argument("--supervise", type=float, default=0.0,
                    help="seconds of continuous position supervision after the cycle")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    LOG_PATH.parent.mkdir(exist_ok=True)

    config = load_config()
    broker = AlpacaBroker()
    _refresh_calendar(broker)

    try:
        store = SmcStateStore(config.db_path)
    except SmcStateError as e:
        # Loud, halting, and explicitly NOT "there are no positions".
        logger.critical("SMC STATE UNAVAILABLE -- refusing to trade: %s", e)
        print(json.dumps({"status": "STATE_UNAVAILABLE", "error": str(e)}))
        return 3

    try:
        pipeline = SmcPipeline(
            store, broker, config, detect_fn=make_detect_fn(store), select_fn=select_fn,
            exit_config=ExitConfig(), dashboard_sync_fn=dashboard_sync_fn,
            dashboard_db=DASHBOARD_DB,
        )
        out = {"mode": "ARMED" if args.arm else "DRY_RUN",
               "opra": broker.opra_available()}

        if args.once:
            out["signal_cycle"] = pipeline.run_signal_cycle(arm=args.arm).as_dict()
        if args.supervise > 0:
            out["supervision"] = pipeline.supervise_for(args.supervise, arm=args.arm).as_dict()
        if not args.once and args.supervise <= 0:
            out["startup_only"] = pipeline.startup().as_dict()

        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
