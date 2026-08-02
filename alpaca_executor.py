#!/usr/bin/env python3
"""alpaca_executor.py — AUTONOMOUS executor on Alpaca. Phase 1 = PAPER real-fill validation.

The manual path stages triggers to Robinhood for one-tap confirm. This is the unattended
replacement: it reads today's pending triggers, runs them through the SAME safety stack as
the manual path, sizes to the REAL book, and submits orders to Alpaca — no human in the loop.

SAFETY STACK (all enforced before any submit):
  1. KILL-SWITCH (data/kill_switch.flag / env HALT_TRADING) — master off-switch, halts BOTH
     dry-run-arm and live. --arm REFUSES while it is engaged.
  2. DAILY-LOSS HALT + POSITION CAP (guardrails.py), fed real Alpaca values.
  3. PORTFOLIO MANAGER GATE (portfolio_gate.py) — concurrency + per-side de-correlation + rank.
  4. Sizing via mrs.size_trade -> whole shares for TOTAL_CAPITAL ($800/$1000 book), NOT Alpaca's
     $100k paper balance (faithful to the real account).

MODES:
  (default)  DRY-RUN — compute + log intended orders, submit NOTHING. Safe any time.
  --arm      submit to Alpaca PAPER (paper-api). Requires kill-switch CLEAR.
  --arm --live   submit to the LIVE account (api.alpaca.markets). Requires --i-understand-live too.

ORDER MECHANICS (whole shares; Alpaca supports native stops, unlike RH fractional):
  mean-rev (has T1): BRACKET = limit entry + take_profit(T1) + stop_loss(stop).
  ORB (EOD exit):    OTO     = limit entry + stop_loss(stop); flatten at EOD via --flatten.

Every submission is logged to data/alpaca_orders.jsonl (idempotent: a trade_id already there
is skipped). Run: ./venv/bin/python alpaca_executor.py            # dry-run today
                  ./venv/bin/python alpaca_executor.py --arm      # paper-submit (kill-switch off)
                  ./venv/bin/python alpaca_executor.py --flatten  # EOD: close ORB positions
"""
import sys
import json
import time
import fcntl
import argparse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import mean_reversion_scanner as mrs
import guardrails
import portfolio_gate
import cross_book_registry

from log_setup import get_logger
log = get_logger("executor")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ET = ZoneInfo("America/New_York")
ORDERS_LOG = DATA / "alpaca_orders.jsonl"
LOCK_PATH = DATA / ".executor.lock"
KEY_PATH = ROOT / "credentials" / "alpaca_key.txt"
SECRET_PATH = ROOT / "credentials" / "alpaca_secret.txt"
PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
SOURCES = ("mr_triggers", "orb_triggers")
# Prove-It-Or-Lose-It threshold (2026-06-29, heff's spec): a real loss, beyond normal
# bid-ask spread friction, that justifies cutting a held position to seat a fresh trigger.
LOSER_PLPC = -0.005
# ROTATION KILLED 2026-07-10 (heff's explicit call): rotation_backtest.py holdout-tested
# evaluate_replacement()'s live behavior (CUT LOSER + CHOKE WINNER together, real trigger
# history, locked 75/25 split) and found it net NEGATIVE vs a no-rotation baseline
# (-6.46R / -$6.25 on the holdout, win% down ~20pts). CUT LOSER itself barely ever fired
# (3/420 triggers) -- the actual driver was CHOKE WINNER's side effect of moving stops to
# breakeven, closing positions early, and freeing slots for MORE, LOWER-QUALITY trades.
# Kept evaluate_replacement()'s code intact (undo = flip this back to True) rather than
# deleting it, same pattern as USE_SECTOR_GATE.
ROTATION_ENABLED = False
PENDING_ASSIGNMENT_PATH = DATA / "options_assignment_pending.json"
PENDING_ASSIGNMENT_TTL_S = 900  # must match options_eval.py's PENDING_ASSIGNMENT_TTL_S


def creds():
    return KEY_PATH.read_text().strip(), SECRET_PATH.read_text().strip()


def _assignment_pending_symbols():
    """Symbols options_eval.py's opasn_liquidate() has marked as under options-assignment
    recovery (2026-07-02) -- a freshly-assigned stock position has a <=6-char symbol, the exact
    same shape as any equity-book ticker, so without this it could get silently miscounted into
    THIS book's committed-slots count or even selected by evaluate_replacement's "cut worst
    position" rotation below, corrupting both books' P&L attribution (same bug class as the
    OCC-symbol-length equity/options miscount already fixed 2026-07-01, just the reverse
    direction). Self-expiring: entries older than PENDING_ASSIGNMENT_TTL_S are ignored, so a
    stale/crashed marker can't permanently block a symbol from this book."""
    if not PENDING_ASSIGNMENT_PATH.exists():
        return set()
    try:
        data = json.loads(PENDING_ASSIGNMENT_PATH.read_text())
        now = time.time()
        return {sym for sym, ts in data.items() if now - ts < PENDING_ASSIGNMENT_TTL_S}
    except Exception:
        return set()


class Alpaca:
    def __init__(self, live=False):
        k, s = creds()
        self.base = LIVE_BASE if live else PAPER_BASE
        self.h = {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}

    def _get(self, path):
        r = requests.get(self.base + path, headers=self.h, timeout=20)
        r.raise_for_status()
        return r.json()

    def account(self):
        return self._get("/v2/account")

    def positions(self):
        return self._get("/v2/positions")

    def clock(self):
        return self._get("/v2/clock")

    def submit(self, payload):
        r = requests.post(self.base + "/v2/orders", headers=self.h, json=payload, timeout=20)
        if not r.ok:
            return {"error": r.status_code, "body": r.text[:300], "payload": payload}
        return r.json()

    def replace_order(self, order_id, stop_price=None, limit_price=None):
        """PATCH /v2/orders/{id} -- used to tighten a stop (or limit) on an existing leg
        without cancel+resubmit. Only sends the fields actually passed."""
        payload = {}
        if stop_price is not None:
            payload["stop_price"] = f"{float(stop_price):.2f}"
        if limit_price is not None:
            payload["limit_price"] = f"{float(limit_price):.2f}"
        r = requests.patch(self.base + f"/v2/orders/{order_id}", headers=self.h, json=payload, timeout=20)
        if not r.ok:
            return {"error": r.status_code, "body": r.text[:300], "order_id": order_id}
        return r.json()

    def close_position(self, symbol):
        r = requests.delete(self.base + f"/v2/positions/{symbol}", headers=self.h, timeout=20)
        return {"status": r.status_code, "body": (r.json() if r.ok else r.text[:200])}

    def open_orders_for(self, symbol):
        try:
            return self._get(f"/v2/orders?status=open&nested=true&symbols={symbol}")
        except Exception as e:
            log.error(f"open_orders_for({symbol}): {e}")
            return []

    def cancel_order(self, order_id):
        r = requests.delete(self.base + f"/v2/orders/{order_id}", headers=self.h, timeout=20)
        return r.status_code

    def cancel_orders_for(self, symbol):
        """Cancel every open order on `symbol` so its shares are released from held_for_orders.
        Without this, DELETE /v2/positions 403s ("insufficient qty available ... held_for_orders")
        whenever a resting protective stop is still reserving the shares -- the bug that left
        DXCM/KMI/KHC stuck open + naked for days (the EOD flatten failed silently every time)."""
        out = []
        for o in self.open_orders_for(symbol):
            if o.get("symbol") == symbol:
                out.append({o["id"]: self.cancel_order(o["id"])})
        return out


def load_triggers(date_str):
    rows = []
    for prefix in SOURCES:
        p = DATA / f"{prefix}_{date_str}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def already_submitted():
    done = set()
    if ORDERS_LOG.exists():
        for line in ORDERS_LOG.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    done.add(json.loads(line)["trade_id"])
                except Exception:
                    pass
    return done


def build_order(t):
    """Map a trigger to an Alpaca order payload. mean-rev -> bracket (TP=t1, SL=stop);
    ORB -> OTO (entry + stop, EOD-flattened). Whole shares from the real-book sizer."""
    side_long = str(t.get("side", "")).upper() == "LONG"
    al_side = "buy" if side_long else "sell"
    entry = round(float(t["entry"]), 2); stop = round(float(t["stop"]), 2)
    # Alpaca rejects the WHOLE atomic bracket/OTO order if stop == entry after rounding to
    # cents (seen live: AES SHORT 2026-07-01, both rounded to 14.64 -> "stop_price must be
    # >= base_price + 0.01", zero position opened that tick). Enforce the minimum 1-cent gap
    # in the stop's required direction here; only touches the rare rounding-collision case --
    # min()/max() leave an already-valid gap untouched.
    stop = min(stop, entry - 0.01) if side_long else max(stop, entry + 0.01)
    shares, notional, risk_dollars = mrs.size_trade(entry, stop)
    if shares <= 0:
        return None, "0 shares (1 whole share exceeds risk cap)"
    base = {"symbol": t["ticker"], "qty": str(int(shares)), "side": al_side,
            "type": "limit", "limit_price": f"{entry:.2f}", "time_in_force": "day"}
    t1 = t.get("t1")
    if t1 not in (None, ""):  # mean-rev: full bracket
        base.update(order_class="bracket",
                    take_profit={"limit_price": f"{float(t1):.2f}"},
                    stop_loss={"stop_price": f"{stop:.2f}"})
    else:                      # ORB: entry + protective stop only (EOD exit handled separately)
        base.update(order_class="oto", stop_loss={"stop_price": f"{stop:.2f}"})
    return base, None


def find_stop_order(api, symbol):
    """Locate the open protective stop-loss leg for `symbol` (bracket or oto order_class --
    nested=true rolls the take_profit/stop_loss children under the parent's `legs`). Returns
    the leg, or None if no open stop is found (caller must handle that -- it means
    there is nothing to choke)."""
    try:
        orders = api._get(f"/v2/orders?status=open&nested=true&symbols={symbol}")
    except Exception as e:
        log.error(f"find_stop_order_id({symbol}): {e}")
        return None
    for o in orders:
        if o.get("symbol") != symbol:
            continue
        if o.get("type") == "stop":
            return o
        for leg in (o.get("legs") or []):
            if leg.get("type") == "stop":
                return leg
    return None


def find_stop_order_id(api, symbol):
    """Backward-compatible id-only wrapper for callers that do not need stop details."""
    order = find_stop_order(api, symbol)
    return order.get("id") if order else None


def _wait_for_fill(api, order_id, timeout=6.0, interval=0.5):
    """Best-effort poll for a just-submitted closing market order to fill before the freed
    slot is handed to a new entry. Bounded short (this runs inside a 2-min cron tick) --
    proceeds either way once the deadline passes, the caller logs whether it actually
    confirmed the fill. Wrapped in try/except since a transient Alpaca timeout here must
    not crash the run."""
    if not order_id:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            o = api._get(f"/v2/orders/{order_id}")
            if o.get("status") == "filled":
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def evaluate_replacement(api, rejected_trigger, open_positions, armed):
    """Prove-It-Or-Lose-It (2026-06-29, widened 2026-06-30 to also cover the per-side cap --
    with MAX_CONCURRENT=3/MAX_PER_SIDE=2 the side cap binds BEFORE the total cap in the common
    case, e.g. 2 LONGs open + 1 free slot: a new LONG signal used to get silently discarded with
    no rotation attempt at all. Now: if rejected_trigger was bumped by the side cap, the ranking
    pool is restricted to open positions on THAT side only (cutting a SHORT loser doesn't free a
    LONG slot); if bumped by the total concurrency cap, any side is eligible as before). Ranks
    the REAL open Alpaca positions (in the eligible pool) by live unrealized_plpc and applies:

      1. CUT THE LOSER -- the worst position's unrealized loss is worse than LOSER_PLPC
         (-0.5%, beyond normal bid-ask spread friction): market-close it (api.close_position,
         same mechanism --flatten already uses), best-effort confirm the fill, then free the
         slot for `rejected_trigger`.
      2. CHOKE THE WINNER -- every open position is profitable, so the "worst" one is really
         just the smallest winner: move ITS stop to breakeven (its own avg_entry_price) via
         api.replace_order on the open stop leg. The new trigger stays unexecuted -- this only
         de-risks the existing book, it never seats a new trade.
      3. otherwise -- the worst position is a small loss inside the spread buffer (between
         -0.5% and 0%) -- neither rule fires; hold, the trigger stays rejected.

    Returns "cut" / "choke" / "hold" / "error" (each also logged) so the caller knows whether
    to promote `rejected_trigger` into this run's approved list. In dry-run (armed=False) this
    previews the decision without calling any mutating Alpaca endpoint."""
    if not open_positions:
        return "hold"
    pool = open_positions
    if "side cap" in str(rejected_trigger.get("gate_reason", "")):
        want_side = str(rejected_trigger.get("side", "")).upper()
        pool = [p for p in open_positions
                if ("LONG" if float(p.get("qty", 0) or 0) > 0 else "SHORT") == want_side]
        if not pool:
            log.info(f"  evaluate_replacement: side-cap rejection for {want_side} but no "
                     f"matching open position found to rank; holding.")
            return "hold"
    try:
        ranked = sorted(pool, key=lambda p: float(p.get("unrealized_plpc", 0) or 0))
    except Exception as e:
        log.error(f"evaluate_replacement: could not rank positions by P&L: {e}")
        return "error"
    worst = ranked[0]
    sym = worst.get("symbol")
    try:
        plpc = float(worst.get("unrealized_plpc", 0) or 0)
    except Exception:
        log.error(f"evaluate_replacement: bad unrealized_plpc on {sym}: {worst.get('unrealized_plpc')!r}")
        return "error"

    if plpc < LOSER_PLPC:
        if not armed:
            log.info(f"  [dry-run] would CUT LOSER {sym} ({plpc:+.2%}) to seat {rejected_trigger.get('ticker')}")
            return "cut"
        try:
            # Mirror the --flatten path exactly: cancel the resting protective stop FIRST so
            # its shares are released from held_for_orders, THEN close. A bare close_position()
            # 403s with held_for_orders whenever that stop is still resting (this is the same
            # DXCM/KMI/KHC-class bug the flatten path was fixed for; see cancel_orders_for()
            # docstring). Live-confirmed here too: 2026-07-10 14:10:21 CUT LOSER MRNA got a 403
            # (held_for_orders=3) but the old code had no status check and treated it as success
            # anyway, freeing MRNA's accounting slot and seating FDXF while MRNA never actually
            # closed on the broker side.
            cancels = api.cancel_orders_for(sym)
            resp = api.close_position(sym)
            status = resp.get("status") if isinstance(resp, dict) else None
            body = resp.get("body") if isinstance(resp, dict) else None
            if not (isinstance(status, int) and 200 <= status < 300):
                # Real failure -- do NOT report this as a successful cut. The caller in main()
                # only promotes rejected_trigger when this returns "cut"; returning "error" here
                # keeps the new signal correctly withheld instead of seating a phantom extra
                # position on top of a loser that never actually closed.
                log.error(f"  evaluate_replacement: close_position({sym}) failed "
                          f"(status={status}, cancels={cancels}): {resp}")
                return "error"
            # close_position() returns {"status": <int>, "body": <json_or_text>} -- there is no
            # top-level "id" even on genuine success, so pull it from body (guard against body
            # being a plain string, which happens on non-JSON error responses).
            close_id = body.get("id") if isinstance(body, dict) else None
            filled = _wait_for_fill(api, close_id)
            tag = "[fill confirmed]" if filled else "[fill NOT confirmed within timeout, proceeding anyway]"
            log.warning(f"  🔻 CUT LOSER {sym} ({plpc:+.2%}) {tag} -> seating "
                        f"{rejected_trigger.get('ticker')}: {resp}")
            return "cut"
        except Exception as e:
            log.error(f"  evaluate_replacement: close_position({sym}) failed: {e}")
            return "error"

    if plpc > 0:
        try:
            entry_price = float(worst.get("avg_entry_price"))
        except Exception as e:
            log.error(f"  evaluate_replacement: bad avg_entry_price on {sym}: {e}")
            return "error"
        if not armed:
            log.info(f"  [dry-run] would CHOKE WINNER {sym} ({plpc:+.2%}) stop -> breakeven ${entry_price:.2f}")
            return "choke"
        try:
            stop_order = find_stop_order(api, sym)
        except Exception as e:
            log.error(f"  evaluate_replacement: find_stop_order_id({sym}) failed: {e}")
            return "error"
        if stop_order is None:
            log.warning(f"  evaluate_replacement: no open stop leg found for {sym}; cannot choke, holding.")
            return "hold"
        order_id = stop_order.get("id")
        try:
            current_stop = float(stop_order.get("stop_price"))
        except (TypeError, ValueError):
            current_stop = None
        # Alpaca rejects no-op replacements with HTTP 422. Treat a stop already at
        # breakeven (to the same cent sent by replace_order) as an idempotent success.
        if current_stop is not None and round(current_stop, 2) == round(entry_price, 2):
            log.info(f"  CHOKE WINNER {sym}: stop already at breakeven ${entry_price:.2f}; no PATCH needed.")
            return "choke"
        try:
            resp = api.replace_order(order_id, stop_price=entry_price)
            # replace_order() does NOT raise on a failed PATCH -- on a non-2xx response it
            # returns {"error": status, "body": ..., "order_id": ...} instead (see
            # Alpaca.replace_order). The stop leg can fill or get canceled between when
            # open_positions was read for ranking and this call landing -- a real race on the
            # 2-minute cron tick. Without this check a failed PATCH fell through as if the
            # breakeven-stop move succeeded, silently leaving the position riding on its
            # original, wider stop while the log claimed success.
            if isinstance(resp, dict) and "error" in resp:
                log.error(f"  evaluate_replacement: replace_order({sym}, {order_id}) failed: {resp}")
                return "error"
            log.info(f"  🔒 CHOKE WINNER {sym} ({plpc:+.2%}) stop -> breakeven ${entry_price:.2f}: {resp}")
            return "choke"
        except Exception as e:
            log.error(f"  evaluate_replacement: replace_order({sym}, {order_id}) failed: {e}")
            return "error"

    log.info(f"  evaluate_replacement: worst position {sym} ({plpc:+.2%}) inside spread buffer; holding.")
    return "hold"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(ET).date().isoformat())
    ap.add_argument("--arm", action="store_true", help="actually submit orders (else dry-run)")
    ap.add_argument("--live", action="store_true", help="use the LIVE account instead of paper")
    ap.add_argument("--i-understand-live", action="store_true", help="required co-flag for --live")
    ap.add_argument("--flatten", action="store_true", help="EOD: market-close all open ORB positions")
    ap.add_argument("--max", type=int, default=None, help="cap submissions this run (smoke-test: --max 1)")
    a = ap.parse_args()

    # Non-blocking lock against overlapping cron runs (2026-06-28): the scanners already
    # have this (mean_reversion_scanner.py, orb_scanner.py); the executor never did. At
    # */2 cadence a slow Alpaca API call could let two invocations overlap and both walk
    # the same triggers/portfolio-gate state concurrently -- skip cleanly instead.
    DATA.mkdir(parents=True, exist_ok=True)
    lock_fp = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.debug("previous alpaca_executor run still in progress; skipping this tick.")
        return

    armed = a.arm
    live = a.live
    if live and not (armed and a.i_understand_live):
        log.error("LIVE requires --arm AND --i-understand-live; refusing.")
        sys.exit(2)

    # master kill-switch — halts armed execution in BOTH modes
    killed, ksrc = guardrails.kill_switch_engaged()
    if armed and killed:
        log.warning(f"ABORT — kill-switch engaged ({ksrc}); no orders submitted.")
        sys.exit(1)

    api = Alpaca(live=live)
    try:
        acct = api.account()
        positions = api.positions()
    except Exception as e:
        log.error(f"Alpaca API error: {e}")
        sys.exit(1)

    # FIXED 2026-07-01: api.positions() returns the WHOLE Alpaca account, equity book and
    # options-tournament legs together (same account, same endpoint) -- OCC option symbols
    # are 16 chars (e.g. O260717C00065000) vs equity's <=6. Without this filter, option legs
    # were being counted against this book's 4-slot concurrency cap (silently running the
    # book at less capacity than configured) AND could be selected by evaluate_replacement's
    # "cut the worst position" logic below -- which, if armed, would call api.close_position()
    # on an options-tournament leg to seat an EQUITY trigger, corrupting that system's own
    # position lifecycle. Confirmed live right now: positions included AES/HRL (equity) plus
    # two option legs, and evaluate_replacement had picked one of the option legs as "worst."
    # ALSO EXCLUDED (2026-07-02): symbols under active options-assignment recovery -- a freshly
    # assigned stock position is <=6 chars too (same shape as any equity ticker), so without this
    # it could get caught by this same filter and miscounted as OURS. See
    # _assignment_pending_symbols() / options_eval.py's opasn_liquidate().
    pending_assignment = _assignment_pending_symbols()
    equity_positions = [p for p in positions
                        if len(p.get("symbol", "")) <= 6 and p.get("symbol") not in pending_assignment]

    # armed intraday entries only fire while the market is open (else Alpaca queues to next open)
    if armed and not a.flatten:
        try:
            if not api.clock().get("is_open"):
                log.info("market closed; armed executor skipping (use --flatten at EOD).")
                return
        except Exception:
            pass

    mode = ("LIVE" if live else "PAPER") + (" / ARMED" if armed else " / DRY-RUN")
    log.info(f"=== alpaca_executor {a.date} [{mode}] === book=${mrs.TOTAL_CAPITAL:.0f} "
             f"alpaca_equity=${float(acct['equity']):.0f} open_positions={len(equity_positions)}")

    if a.flatten:
        # EOD flatten = EQUITY positions only (ORB rides to the close). Options legs are SKIPPED --
        # the options tournament owns their lifecycle (its own TP/SL exit sweep intraday + the EOD
        # expiry reconcile), and they are multi-day holds, not same-day rides. For each equity
        # position we CANCEL its resting orders FIRST (releases shares held_for_orders), THEN close
        # -- otherwise the close 403s and the position becomes a naked multi-day zombie (the bug
        # that stranded DXCM/KMI/KHC; see logs/alpaca_flatten.log + cancel_orders_for()).
        closed = []
        for p in equity_positions:  # already options-filtered, see equity_positions above
            sym = p["symbol"]
            if armed:
                cancels = api.cancel_orders_for(sym)
                res = api.close_position(sym)
                closed.append({sym: {"canceled": cancels, "close": res.get("status")}})
            else:
                closed.append({sym: "DRY-RUN (would cancel open orders then close)"})
        log.info(f"flatten ({'ARMED' if armed else 'DRY-RUN'}): {json.dumps(closed)}")
        return

    # committed slots = open POSITIONS + resting (unfilled) ENTRY orders. Counting resting
    # orders matters: a 2-min loop must not stack >MAX limits that later all fill -> over-commit.
    # nested=true keeps child stop/TP legs nested under parents (no double-count).
    try:
        open_orders = api._get("/v2/orders?status=open&nested=true&limit=200")
    except Exception:
        open_orders = []
    committed = {}
    for p in equity_positions:
        committed[p["symbol"]] = {"ticker": p["symbol"],
                                  "side": "LONG" if float(p["qty"]) > 0 else "SHORT",
                                  "notional": abs(float(p["market_value"]))}
    for o in open_orders:
        sym = o.get("symbol")
        if not sym or sym in committed or not o.get("side"):
            continue
        committed[sym] = {"ticker": sym, "side": "LONG" if o["side"] == "buy" else "SHORT",
                          "notional": float(o.get("limit_price") or 0) * float(o.get("qty") or 0)}
    open_pos = list(committed.values())
    log.info(f"committed slots (positions+resting orders): {len(open_pos)}")

    done = already_submitted()
    raw = [t for t in load_triggers(a.date) if t.get("trade_id") not in done]

    # day-level guardrail halt (kill-switch already checked). FIXED 2026-07-01: this used to
    # override with day_pnl = equity - last_equity (whole-ACCOUNT move), on the assumption
    # that "positions are sized to the $800 book (tiny vs the $100k idle paper cash)" so
    # account-wide equity tracked this book's own P&L. That assumption broke once the options
    # tournament started trading real size on the SAME Alpaca paper account: a -$3,891
    # options-only session (2026-06-30) tripped THIS book's -$24 halt via pure cross-account
    # contamination, even though the mean-rev/ORB book itself hadn't lost anywhere near that.
    # No override now -- guardrails.check_guardrails() falls back to its own
    # today_realized_pnl(), which sums dollar_pnl from paper_trades.csv/orb_paper_trades.csv,
    # this book's OWN closed-trade ledger (paper_eval.py/orb_paper_eval.py refresh it every
    # 1-2min during RTH, so it's not stale) -- can't be moved by anything the options
    # tournament does.
    day = guardrails.check_guardrails(open_position_count=len(open_pos), new_entries=0)
    if day["halted_for_day"]:
        log.warning("DAY HALT: " + "; ".join(day["reasons"]))
        if armed:
            return
        log.info("  (dry-run previews anyway; ARMED execution would stop here)")

    approved, rejected = portfolio_gate.select_portfolio(raw, open_positions=open_pos,
                                                         log_context="alpaca_executor")
    log.info(f"{len(raw)} pending -> {len(approved)} approved, {len(rejected)} withheld by gate")

    # Prove-It-Or-Lose-It (2026-06-29): the old behavior just left a capacity-rejected trigger
    # withheld. Now, when we are AT the concurrency cap, evaluate replacing the worst held
    # position instead of silently passing on the new signal. Only the single best-ranked
    # capacity-rejected trigger is evaluated -- one cap firing can free at most one slot, and
    # choking should only happen once per tick regardless of how many triggers got bumped.
    # 2026-06-30: widened from "concurrency cap" only to also include "side cap" rejections
    # (see evaluate_replacement docstring) -- otherwise a fresh signal blocked purely by the
    # 2-per-side correlated-risk limit was silently dropped with zero rotation attempt, even
    # with a free total slot and a stale same-side loser sitting in the book.
    rotation_eligible = [r for r in rejected if
                         "concurrency cap" in str(r.get("gate_reason", "")) or
                         "side cap" in str(r.get("gate_reason", ""))]
    if ROTATION_ENABLED and rotation_eligible:
        action = evaluate_replacement(api, rotation_eligible[0], equity_positions, armed=armed)
        if action == "cut":
            won = rotation_eligible[0]
            rejected.remove(won)
            approved.append(won)
            log.info(f"  ♻️ {won['ticker']} {won['side']} promoted into this run's approved list "
                     f"after cutting the worst loser")

    # CROSS-BOOK CORRELATION CAP (2026-07-05, heff): hard block on same-ticker overlap with the
    # options tournament book. Fetched once per run (not per ticker) -- cheap, but no need to
    # repeat it. Fail-open by construction (cross_book_registry returns {} on any read error),
    # so a broken read here can never itself block an otherwise-valid entry.
    options_tickers = cross_book_registry.options_open_tickers()

    submitted = []
    for t in approved:
        if a.max is not None and len(submitted) >= a.max:
            log.info(f"  (--max {a.max} reached; {len(approved) - len(submitted)} more approved left unsubmitted)")
            break
        if str(t.get("ticker", "")).upper() in options_tickers:
            log.info(f"  SKIP {t['ticker']}: already open in options tournament book")
            continue
        payload, skip = build_order(t)
        if skip:
            log.info(f"  SKIP {t['ticker']} {t['side']}: {skip}")
            continue
        if armed:
            resp = api.submit(payload)
            ok = "error" not in resp
            rec = {"trade_id": t["trade_id"], "ts": datetime.now(ET).isoformat(timespec="seconds"),
                   "mode": mode, "payload": payload, "alpaca": resp.get("id") if ok else resp}
            with ORDERS_LOG.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            line = (f"  {'✅ SUBMITTED' if ok else '❌ ERR'} {t['ticker']} {t['side']} "
                    f"{payload['qty']}sh @ {payload['limit_price']} -> {resp.get('id', resp)}")
            log.info(line) if ok else log.error(line)
        else:
            log.info(f"  [dry-run] {payload['order_class']} {t['ticker']} {payload['side']} "
                     f"{payload['qty']}sh @ {payload['limit_price']} stop {payload['stop_loss']['stop_price']}"
                     + (f" tp {payload['take_profit']['limit_price']}" if 'take_profit' in payload else ""))
        submitted.append(t["trade_id"])
    for r in rejected:
        log.info(f"  ⛔ {r.get('ticker')} {r.get('side')} — {r.get('gate_reason')}")


if __name__ == "__main__":
    main()
