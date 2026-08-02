"""Dashboard lifecycle panel for the PAPER daemon.

Writes one atomic JSON snapshot the BOT_NEXUS dashboard reads. Deliberately
a pull-model file rather than a push: a dashboard write must never be able to
block or fail the trading path, and a file the frontend polls cannot.

Everything that could mislead an operator is stated explicitly rather than
implied by absence:

  * mode is ALPACA PAPER, always, in the payload -- never inferred
  * the candidate is named VARIANT_B_NO_SWEEP, not "Variant B"
  * SIGNAL_FEED=alpaca_iex is shown, because the forward result is NOT
    TradingView/SIP parity and the panel should not let anyone forget it
  * degraded and unmanaged-risk states are top-level booleans, not buried

`write_snapshot` never raises: a dashboard failure is reported in the return
value and swallowed, because the alternative is a panel outage taking down
order management.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("smc.dashboard")

MODE_LABEL = "ALPACA PAPER"
CANDIDATE = "VARIANT_B_NO_SWEEP"
SIGNAL_FEED = "alpaca_iex"
SCHEMA = "smc-dashboard-v1"


def build_snapshot(*, runner_health: dict, detector=None, exit_monitor=None,
                   exit_liveness=None, notifier=None, positions=None,
                   recent_signals=None, recent_orders=None,
                   entry_attempts=None) -> dict:
    """Assembles the panel payload. Pure -- no I/O, so it is safe to call
    from anywhere and trivially testable."""
    readiness = (runner_health or {}).get("readiness", {}) or {}
    bus = (runner_health or {}).get("bus", {}) or {}
    theta = (runner_health or {}).get("theta_stream") or {}
    alpaca = (runner_health or {}).get("trade_updates") or {}

    det = detector.health() if detector is not None else {}
    exits = exit_monitor.health() if exit_monitor is not None else {}
    live = exit_liveness.health() if exit_liveness is not None else {}
    notif = notifier.health() if notifier is not None else {}

    unmanaged = int(live.get("unmanaged_risk", 0) or 0)

    return {
        "schema": SCHEMA,
        "generated_ts": dt.datetime.now(dt.timezone.utc).isoformat(),

        # --- identity: never inferred, always stated ---
        "mode": MODE_LABEL,
        "is_live_money": False,
        "candidate": CANDIDATE,
        "signal_feed": SIGNAL_FEED,
        "feed_caveat": ("Alpaca IEX bars -- single venue, NOT consolidated SIP "
                        "and NOT TradingView chart data. Pine-vs-Python and "
                        "TradingView-feed parity remain open limitations."),

        # --- readiness ---
        "entries_permitted": bool(readiness.get("entries_permitted", False)),
        "degraded": bool(readiness.get("degraded", True)),
        "gates": readiness.get("statuses", {}),
        "gate_detail": readiness.get("gates", {}),
        "market_closed_gates": readiness.get("market_closed_gates", []),
        "not_testable_gates": readiness.get("not_testable_gates", []),
        "live_data_unverified": readiness.get("live_data_unverified", []),
        "blocking_gates": readiness.get("blocking", []),
        "gate_reasons": readiness.get("reasons", {}),

        # --- detector ---
        "detector": {
            "cursor_last_bar": det.get("cursor_last_bar"),
            "session": det.get("session"),
            "today_bars": det.get("today_bars"),
            "runtime_ms": det.get("last_runtime_ms"),
            "emitted_count": det.get("emitted_count"),
            "revised_bars": det.get("revised_bars"),
            "config_checksum": det.get("detector_config_checksum"),
            "series_checksum": det.get("source_series_checksum"),
            "synchronized": det.get("synchronized"),
        },

        # --- event loop ---
        "event_loop": {
            "depth": bus.get("depth"),
            "depth_high_water": bus.get("depth_high_water"),
            "max_queue_delay_ms": bus.get("max_queue_delay_ms"),
            "max_handler_ms": bus.get("max_handler_ms"),
            "max_loop_lag_ms": bus.get("max_loop_lag_ms"),
            "dropped_informational": bus.get("dropped_informational"),
            "coalesced_informational": bus.get("coalesced_informational"),
            "scheduled_deadlines": bus.get("scheduled_deadlines"),
        },

        # --- market data ---
        "thetadata": {
            "connected": theta.get("connected"),
            "generation": theta.get("generation"),
            "n_cached_quotes": theta.get("n_cached_quotes"),
            "n_subscribed": theta.get("n_subscribed"),
            "seconds_since_last_message": theta.get("seconds_since_last_message"),
            "reconnect_count": theta.get("reconnect_count"),
            "reader_callback_ms": theta.get("reader_callback_ms"),
            "reader_callback_errors": theta.get("reader_callback_errors"),
        },
        "alpaca_stream": {
            "connected": alpaca.get("connected"),
            "authenticated": alpaca.get("authenticated"),
            "generation": alpaca.get("generation"),
            "event_count": alpaca.get("event_count"),
            "unknown_event_count": alpaca.get("unknown_event_count"),
            "needs_reconcile": alpaca.get("needs_reconcile"),
            "seconds_since_last_message": alpaca.get("seconds_since_last_message"),
        },

        # --- trading lifecycle ---
        "positions": positions or [],
        "recent_signals": (recent_signals or [])[-20:],
        "recent_orders": (recent_orders or [])[-20:],
        "entry_attempts": [a.summary() if hasattr(a, "summary") else a
                           for a in (entry_attempts or [])][-20:],
        "exits": {
            "decisions": exits.get("decisions"),
            "submitted": exits.get("submitted"),
            "inflight": exits.get("inflight"),
            "suppressed_stale_quote": exits.get("suppressed_stale_quote"),
            "suppressed_inflight": exits.get("suppressed_inflight"),
            "max_decide_latency_ms": exits.get("max_decide_latency_ms"),
            "max_submit_latency_ms": exits.get("max_submit_latency_ms"),
            "by_reason": exits.get("by_reason", {}),
        },
        "exit_liveness": {
            "tracked": live.get("tracked"),
            "in_flight": live.get("in_flight"),
            "partial": live.get("partial"),
            "lost_event_reconciles": live.get("lost_event_reconciles"),
            "oldest_deadline_age_s": live.get("oldest_deadline_age_s"),
            "entries_blocked": live.get("entries_blocked"),
        },

        # --- notification ---
        "notifications": {
            "state": notif.get("state"),
            "sent": notif.get("sent"),
            "failed": notif.get("failed"),
            "outbox": notif.get("outbox"),
            "undelivered_critical": notif.get("undelivered_critical"),
            "hint_deferred": notif.get("hint_deferred"),
        },

        # --- the two states that must never be subtle ---
        "unmanaged_risk_count": unmanaged,
        "unmanaged_risk": unmanaged > 0,
        "alert_banner": _banner(readiness, unmanaged, notif),
    }


def _banner(readiness: dict, unmanaged: int, notif: dict) -> Optional[str]:
    if unmanaged:
        return (f"UNMANAGED RISK: {unmanaged} position(s) with unresolved exit "
                "fate. NOT confirmed closed. Manual attribution required.")
    if int(notif.get("undelivered_critical") or 0):
        return (f"{notif['undelivered_critical']} critical notification(s) "
                "undelivered -- trading unaffected, but you are not being told.")
    if readiness.get("degraded", True):
        return f"DEGRADED -- entries blocked by: {readiness.get('blocking', [])}"
    return None


def write_snapshot(path, payload: dict) -> dict:
    """Atomic write (tmp + rename), so the dashboard can never read a
    half-written file. NEVER raises."""
    result = {"ok": False, "path": str(path), "error": None}
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(p)
        result["ok"] = True
    except Exception as e:  # noqa: BLE001 -- a panel outage must not stop trading
        result["error"] = repr(e)
        logger.warning("dashboard snapshot write failed (ignored): %s", e)
    return result
