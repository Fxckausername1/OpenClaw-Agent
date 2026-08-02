#!/usr/bin/env python3
"""
options_orchestrator.py — per-tick options agent wiring (OPTIONS_ORCHESTRATOR_SPEC.md).

CRON-TICK model (NOT an always-on async daemon — the existing */2 executor cadence already
IS the spec's temporal aggregation window; we ported the math, not the AWS/websocket service).

Per signal:  live chain -> compute IV (indicative feed gives NO greeks) -> Breeden-Litzenberger RND
-> Esscher tilt by the equity signal (mu=alpha*z*dir) -> discrete spread optimizer -> guardrails +
portfolio gate -> Alpaca mleg payload.  DRY-RUN by default; --arm submits to PAPER. Real money is
HARD-REFUSED here (confirm-every-trade governs real money).

Usage:
  ./venv/bin/python options_orchestrator.py --ticker F --direction 1 --z 1.5            # dry-run
  ./venv/bin/python options_orchestrator.py --ticker F --direction 1 --z 1.5 --arm      # paper submit
"""
import argparse
import json
import logging
import math
import time
from pathlib import Path
from datetime import datetime, timezone, date, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import requests

import greeks
import options_lib as ol
import guardrails
from options_eval import RegimeTag, connect as eval_connect, init_db as eval_init, \
    record_open as eval_record_open, resolve_trade as eval_resolve, apply_close as eval_apply_close, \
    TradeStatus, _occ_expiry
from options_confluence_tag import build_confluence_tag

ROOT = Path(__file__).resolve().parent
KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}
PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"

R = 0.05              # risk-free proxy
ALPHA = 0.01          # signal scaling: mu_signal = ALPHA * z * direction (calibratable)
PER_TRADE_RISK = ol.MAX_RISK_PER_TRADE  # single-sourced from options_lib (was its own separate
                         # literal that happened to still match after the 500->150 change --
                         # see options_lib.py's own history comment for that rationale) --
                         # this codebase has drifted duplicate risk constants before (MAX_
                         # CONCURRENT/MAX_SLOTS), so a second source of truth here is unwarranted.
BOOK = 1000.0
VIX_THRESHOLD = 20.0  # regime boundary (spec §3)
TP_FRAC = 0.50        # take-profit: capture 50% of max credit
SL_MULT = 2.00        # stop-loss: close if buyback cost >= 2x entry credit
IOC_BUFFER_S = 2.0    # synthetic-IOC latency buffer (blocking sleep, cron-safe per directive)

# --- 2026-07-01 forensics of the 6/30 -$3,852 session (12/12 losers, several stopped out
# 23-125s after entry): the losses were bid-ask FRICTION, not direction. The exit engine marked
# open spreads at the NATURAL (worst-side) quote while SL thresholds sit at 40-60% of entry --
# on thin low-priced chains the quote width alone breaches SL the moment the fill prints, and the
# close then crosses the spread again, realizing the phantom loss. Three defenses:
TOURNAMENT_DAILY_LOSS_LIMIT = 500.0  # $: halt NEW tournament entries once today's realized <= -this
MAX_DRAWDOWN_LIMIT = 2500.0          # $: 5x the daily limit -- rolling all-time-peak drawdown halt
                                      # (2026-07-05, heff), catches a multi-day bleed the daily-only
                                      # check above can't (that one resets every midnight)
FRICTION_MAX_FRAC = 0.25             # entry gate: max immediate mid-mark markdown vs entry price
SL_CONFIRM_S = 60.0                  # SL must persist across ticks this long before we cross the spread
SL_PENDING_PATH = ROOT / "data" / "options_sl_pending.json"

# --- 0DTE force-close (2026-07-02, defined-risk/assignment gap-risk hardening) ---
# A short option can be exercised by its holder at ANY time it's ITM, not just at expiry -- a
# defined-risk spread's max_loss bound only holds if both legs close TOGETHER, which early
# assignment breaks. S1/S2/S5 are DTE=0 outright and S6/S9's front leg is always same-day, so any
# of them left open into the final minutes is riding uncompensated assignment/pin risk for no
# remaining edge. See options_eval.check_assignments() (intraday detection) for the complementary
# defense once assignment has already happened.
ET = ZoneInfo("America/New_York")
FORCE_CLOSE_HOUR, FORCE_CLOSE_MINUTE = 15, 45  # ET -- 15min before the 16:00 close

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [orch] %(message)s",
                    handlers=[logging.FileHandler(ROOT / "logs" / "options_orchestrator.log"),
                              logging.StreamHandler()])
log = logging.getLogger("orch")


# Token Bucket guarding Alpaca's 200 req/min (spec §2): capacity 190, refill ~3.16/s. All live
# reads pass through _throttle() so a burst (all 10 arms fetching chains/quotes at once) can never
# breach the limit — it queues instead.
_BUCKET = ol.TokenBucket(capacity=190, refill_per_sec=190 / 60.0)
_BUCKET_STATS = {"calls": 0, "throttled": 0, "waited_s": 0.0}


def _throttle():
    w = _BUCKET.take(1)
    _BUCKET_STATS["calls"] += 1
    if w > 0:
        _BUCKET_STATS["throttled"] += 1
        _BUCKET_STATS["waited_s"] += w
        time.sleep(w)


def _get(base, path, **params):
    _throttle()
    r = requests.get(base + path, headers=H, params=params or None, timeout=25)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


def spot(ticker):
    sc, s = _get(DATA, f"/v2/stocks/{ticker}/snapshot", feed="iex")
    if sc != 200:
        return None
    return (s.get("latestTrade") or {}).get("p")


def pick_expiration(ticker, dte_target, min_dte=7):
    """Nearest listed expiration to dte_target (>= min_dte days out)."""
    today = date.today()
    sc, c = _get(PAPER, "/v2/options/contracts", underlying_symbols=ticker,
                 expiration_date_gte=today.isoformat(), limit=10000)
    if sc != 200:
        return None
    exps = sorted({x["expiration_date"] for x in c.get("option_contracts", [])})
    cand = [(abs((date.fromisoformat(e) - today).days - dte_target), e)
            for e in exps if (date.fromisoformat(e) - today).days >= min_dte]
    return min(cand)[1] if cand else None


def fetch_chain(ticker, exp, S, band=0.15):
    """Contracts in an NTM band for one expiration, with live indicative quotes + COMPUTED IV."""
    lo, hi = S * (1 - band), S * (1 + band)
    sc, c = _get(PAPER, "/v2/options/contracts", underlying_symbols=ticker,
                 expiration_date=exp, strike_price_gte=f"{lo:.2f}", strike_price_lte=f"{hi:.2f}",
                 limit=10000)
    if sc != 200:
        return []
    rows = {x["symbol"]: x for x in c.get("option_contracts", [])}
    if not rows:
        return []
    syms = list(rows)
    T = max((date.fromisoformat(exp) - date.today()).days, 1) / 365.0
    chain = []
    for i in range(0, len(syms), 100):                       # batch quote requests
        batch = syms[i:i + 100]
        sc, q = _get(DATA, "/v1beta1/options/quotes/latest", symbols=",".join(batch), feed="indicative")
        if sc != 200:
            continue
        for sym, quote in q.get("quotes", {}).items():
            bp, ap = quote.get("bp", 0), quote.get("ap", 0)
            if not (bp > 0 and ap > 0):
                continue
            meta = rows[sym]
            K = float(meta["strike_price"])
            is_call = meta["type"] == "call"
            mid = 0.5 * (bp + ap)
            iv = float(greeks.implied_vol(mid, S, K, T, R, is_call))
            if not math.isfinite(iv):
                continue
            chain.append({"symbol": sym, "strike": K, "type": "C" if is_call else "P",
                          "bid": bp, "ask": ap, "iv": iv, "T": T,
                          "oi": meta.get("open_interest")})
    return chain


def two_expirations(ticker, front_dte, back_dte):
    """Front = nearest listed expiration >= front_dte; back = next sequential expiration after front
    (nearest to back_dte). Returns (exp_front, exp_back) or (None, None)."""
    today = date.today()
    sc, c = _get(PAPER, "/v2/options/contracts", underlying_symbols=ticker,
                 expiration_date_gte=today.isoformat(), limit=10000)
    if sc != 200:
        return None, None
    exps = sorted({x["expiration_date"] for x in c.get("option_contracts", [])})
    fcand = [e for e in exps if (date.fromisoformat(e) - today).days >= max(front_dte, 0)]
    if not fcand:
        return None, None
    ef = min(fcand, key=lambda e: abs((date.fromisoformat(e) - today).days - front_dte))
    bcand = [e for e in exps if e > ef]
    if not bcand:
        return ef, None
    eb = min(bcand, key=lambda e: abs((date.fromisoformat(e) - today).days - back_dte))
    return ef, eb


def fetch_multi_chain(ticker, front_dte, back_dte, band=0.20):
    """Dual-expiration chains for calendar/diagonal arms (S6, S9). Returns dict or None."""
    S = spot(ticker)
    if S is None:
        return None
    ef, eb = two_expirations(ticker, front_dte, back_dte)
    if not ef or not eb:
        return None
    cf, cb = fetch_chain(ticker, ef, S, band), fetch_chain(ticker, eb, S, band)
    if len(cf) < 2 or len(cb) < 2:
        return None
    return {"S": S, "front_chain": cf, "back_chain": cb, "exp_front": ef, "exp_back": eb,
            "T_front": cf[0]["T"], "T_back": cb[0]["T"]}


def build_rnd(chain, S, T):
    """IV smile (one IV per strike, prefer the call) -> Breeden-Litzenberger RND."""
    by_strike = {}
    for c in chain:
        by_strike.setdefault(c["strike"], {})[c["type"]] = c["iv"]
    strikes, ivs = [], []
    for K in sorted(by_strike):
        v = by_strike[K]
        iv = v.get("C", v.get("P"))
        if iv:
            strikes.append(K); ivs.append(iv)
    if len(strikes) < 4:
        return None, None
    return ol.breeden_litzenberger_rnd(np.array(strikes), np.array(ivs), S, T, R, n_grid=600)


def size_qty(max_loss):
    """Contracts so that max_loss*100*qty <= PER_TRADE_RISK (defined-risk, $1000 book).
    Returns 0 if even ONE contract breaches the cap (the optimizer should already reject these)."""
    per_contract = max_loss * 100.0
    if per_contract <= 0 or per_contract > PER_TRADE_RISK:
        return 0
    return int(PER_TRADE_RISK // per_contract)


def mleg_payload(spread, qty):
    """Alpaca multi-leg order (spec §6). TIF=day (mleg has no native IOC -> synthetic IOC at t_max).
    Legs carry the OCC symbol stamped onto them in propose()."""
    legs = [{"symbol": getattr(leg, "_occ", None),
             "ratio_qty": "1",
             "side": "sell" if leg.side == "SELL" else "buy",
             "position_intent": "sell_to_open" if leg.side == "SELL" else "buy_to_open"}
            for leg in spread.legs]
    return {"order_class": "mleg", "qty": str(qty), "type": "limit", "time_in_force": "day",
            "limit_price": f"{spread.p_mid:.2f}", "legs": legs}


def propose(ticker, direction, z, dte=35, band=0.15, pop_min=0.60, r_min=0.20):
    S = spot(ticker)
    if S is None:
        return None, "no spot"
    exp = pick_expiration(ticker, dte)
    if not exp:
        return None, "no expiration"
    chain = fetch_chain(ticker, exp, S, band)
    if len(chain) < 4:
        return None, f"thin chain ({len(chain)})"
    T = chain[0]["T"]
    grid, dens = build_rnd(chain, S, T)
    if grid is None:
        return None, "RND build failed"
    mu = ALPHA * abs(z) * (1 if direction == 1 else -1)
    tilted, theta = ol.esscher_tilt(grid, dens, S, mu)
    slip = 0.0   # TODO: pull per-ticker slippage EMA from options_recon (§5 feedback)
    spread = ol.optimize_spread(ticker, direction, f"{ticker}_{'bullput' if direction==1 else 'bearcall'}",
                                chain, S, T, R, grid, tilted, slippage=slip,
                                max_width=None, pop_min=pop_min, r_min=r_min,
                                max_risk=PER_TRADE_RISK)
    if spread is None:
        return None, "no positive-EV defined-risk spread (valid 'no clean trade')"
    # attach OCC symbols to the chosen legs
    occ = {(c["strike"], c["type"]): c["symbol"] for c in chain}
    for leg in spread.legs:
        setattr(leg, "_occ", occ.get((leg.strike, leg.option_type)))
    return {"spread": spread, "exp": exp, "S": S, "theta": theta, "mu": mu}, "ok"


# ===================================================== DIRECTIVE 1 — REGIME TAGGER
def get_regime_tag(net_gex, vix, vvix=None, vix_prev=None, vvix_prev=None):
    """Strict hierarchical RegimeTag mapping (spec §3). Pure + testable. Returns RegimeTag.
    VOL_EXPANSION_SHOCK first (ΔVV>=0.15 OR ΔV>=0.10), then POS/NEG GEX × VIX(20) quadrants.
    Missing GEX or VIX -> UNKNOWN (never fabricate a regime)."""
    if net_gex is None or vix is None:
        return RegimeTag.UNKNOWN
    dvv = (vvix - vvix_prev) / vvix_prev if (vvix and vvix_prev) else 0.0
    dv = (vix - vix_prev) / vix_prev if (vix and vix_prev) else 0.0
    if dvv >= 0.15 or dv >= 0.10:
        return RegimeTag.VOL_EXPANSION_SHOCK
    if net_gex > 0:
        return RegimeTag.POS_GEX_LOW_VIX if vix < VIX_THRESHOLD else RegimeTag.POS_GEX_HIGH_VIX
    if net_gex < 0:
        return RegimeTag.NEG_GEX_LOW_VIX if vix < VIX_THRESHOLD else RegimeTag.NEG_GEX_HIGH_VIX
    return RegimeTag.UNKNOWN


def fetch_vix_vvix():
    """VIX/VVIX (+ 15m-prior) via yfinance ^VIX/^VVIX. Free; ~15m delayed is fine for the entry tag.
    Returns (vix, vvix, vix_prev, vvix_prev); all None on failure (regime degrades to UNKNOWN)."""
    try:
        import yfinance as yf
        vals = {}
        for sym in ("^VIX", "^VVIX"):
            h = yf.Ticker(sym).history(period="1d", interval="15m")
            c = h["Close"].dropna().tolist() if len(h) else []
            if not c:                                   # after hours -> daily fallback
                h = yf.Ticker(sym).history(period="5d", interval="1d")
                c = h["Close"].dropna().tolist() if len(h) else []
            vals[sym] = (float(c[-1]) if c else None, float(c[-2]) if len(c) >= 2 else None)
        return vals["^VIX"][0], vals["^VVIX"][0], vals["^VIX"][1], vals["^VVIX"][1]
    except Exception as e:
        log.warning(f"regime: VIX/VVIX yfinance pull failed: {e}")
        return None, None, None, None


def fetch_regime_inputs(ticker):
    """Live inputs. Net GEX from gex.py's parquet (lazy pandas import); VIX/VVIX (+15m deltas) via
    yfinance. Any missing piece degrades the regime to UNKNOWN — honest, never fabricated."""
    net_gex = None
    try:
        import pandas as pd
        gp = ROOT / "data" / "options" / f"{ticker}_gex.parquet"
        if gp.exists():
            df = pd.read_parquet(gp)
            clean = df[df["regime"] != "none"]
            if len(clean):
                net_gex = float(clean.iloc[-1]["net_gex"])
    except Exception as e:
        log.warning(f"regime: net_gex fetch failed for {ticker}: {e}")
    vix, vvix, vix_prev, vvix_prev = fetch_vix_vvix()
    return net_gex, vix, vvix, vix_prev, vvix_prev


def regime_for_entry(ticker):
    net_gex, vix, vvix, vix_prev, vvix_prev = fetch_regime_inputs(ticker)
    tag = get_regime_tag(net_gex, vix, vvix, vix_prev, vvix_prev)
    log.info(f"regime {ticker}: net_gex={net_gex} vix={vix} vvix={vvix} -> {tag.value}")
    return tag


# ===================================================== DIRECTIVE 2 — SYNTHETIC IOC EXIT LOOP
def leg_quote(occ):
    sc, q = _get(DATA, "/v1beta1/options/quotes/latest", symbols=occ, feed="indicative")
    if sc != 200:
        return None
    qq = q.get("quotes", {}).get(occ)
    return qq if qq and qq.get("bp", 0) > 0 and qq.get("ap", 0) > 0 else None


def spread_close_cost(legs):
    """Natural debit to CLOSE a credit spread (buy it back): pay ask on the short leg we sold,
    receive bid on the long leg we bought. P_nat = Σ ask(short) − Σ bid(long).  None if any leg unquoted."""
    cost = 0.0
    for leg in legs:
        q = leg_quote(leg["occ"])
        if not q:
            return None
        if leg["side"] == "SELL":          # short leg -> buy_to_close at ask
            cost += q["ap"]
        else:                               # long leg -> sell_to_close at bid
            cost -= q["bp"]
    return round(cost, 2)


def spread_close_mid(legs):
    """Mid-quote value of closing the spread (the DECISION mark). Natural is systematically
    pessimistic by half the total bid-ask width, which on thin chains mechanically breaches SL
    at entry (the 6/30 failure). None if any leg unquoted."""
    cost = 0.0
    for leg in legs:
        q = leg_quote(leg["occ"])
        if not q:
            return None
        mid = 0.5 * (q["bp"] + q["ap"])
        cost += mid if leg["side"] == "SELL" else -mid
    return round(cost, 2)


def _friction_verdict(structure, entry, mid_close, nat_close, sl_close_cost):
    """Pure pre-trade liquidity check. mid_close/nat_close are in spread_close_cost sign convention
    (debit to buy back). Returns (ok, reason). Rejects when (a) the mid-mark close is already
    FRICTION_MAX_FRAC away from entry (quotes are junk), or (b) the NATURAL close -- what we'd
    actually get crossing the spread -- is already at/past the SL threshold (unwinnable trade:
    the exit engine would stop it out on quote width alone)."""
    if mid_close is None:
        return False, "legs unquoted"
    if structure == "credit":
        m = mid_close
        if m >= entry * (1 + FRICTION_MAX_FRAC):
            return False, f"mid buyback {m:.2f} >= {1 + FRICTION_MAX_FRAC:.2f}x credit {entry:.2f}"
        if nat_close is not None and sl_close_cost is not None and nat_close >= sl_close_cost:
            return False, f"natural buyback {nat_close:.2f} already >= SL {sl_close_cost:.2f} (spread too wide)"
    else:
        m = -mid_close
        if m <= entry * (1 - FRICTION_MAX_FRAC):
            return False, f"mid close value {m:.2f} <= {1 - FRICTION_MAX_FRAC:.2f}x debit {entry:.2f}"
        if nat_close is not None and sl_close_cost is not None and -nat_close <= sl_close_cost:
            return False, f"natural close value {-nat_close:.2f} already <= SL {sl_close_cost:.2f} (spread too wide)"
    return True, "ok"


def entry_friction_ok(meta):
    """Live wrapper for _friction_verdict: quotes the legs and checks the chosen arm is not
    pre-stopped by its own bid-ask width."""
    legs = meta["legs"]
    return _friction_verdict(meta.get("structure", "credit"), float(meta["entry_credit"]),
                             spread_close_mid(legs), spread_close_cost(legs),
                             meta.get("sl_close_cost"))


def tournament_realized_today(conn):
    """Sum of the tournament's OWN realized P&L since midnight ET (its daily-loss halt input --
    the equity book's halt was scoped to its own ledger 2026-07-01, so this book needs its own)."""
    from zoneinfo import ZoneInfo
    midnight_et = datetime.now(ZoneInfo("America/New_York")).replace(hour=0, minute=0,
                                                                     second=0, microsecond=0)
    row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM trades_ledger "
                       "WHERE realized_pnl IS NOT NULL AND exit_time >= ?",
                       (int(midnight_et.timestamp()),)).fetchone()
    return float(row[0])


def tournament_peak_drawdown(conn):
    """Rolling all-time peak-equity drawdown for the tournament's own ledger (2026-07-05, heff:
    5x the daily-loss limit -- a multi-day bleed never trips the daily-only halt above, since
    that one resets every midnight regardless of a string of smaller losing days). Note: this
    table's status CHECK constraint only allows PENDING/OPEN/PARTIAL_CLOSE/CLOSED -- no EXPIRED
    value exists here, unlike an earlier assumption."""
    rows = conn.execute(
        "SELECT exit_time, realized_pnl FROM trades_ledger "
        "WHERE status = 'CLOSED' AND realized_pnl IS NOT NULL "
        "ORDER BY exit_time ASC"
    ).fetchall()
    cum = peak = 0.0
    for row in rows:
        cum += row["realized_pnl"]
        peak = max(peak, cum)
    current = cum
    drawdown = max(0.0, peak - current)
    return round(peak, 2), round(current, 2), round(drawdown, 2)


def closing_payload(legs, qty, limit):
    """Reverse the entry sides to close. Net debit limit (positive) at the natural price."""
    rev = []
    for leg in legs:
        rev.append({"symbol": leg["occ"], "ratio_qty": "1",
                    "side": "buy" if leg["side"] == "SELL" else "sell",
                    "position_intent": "buy_to_close" if leg["side"] == "SELL" else "sell_to_close"})
    return {"order_class": "mleg", "qty": str(qty), "type": "limit", "time_in_force": "day",
            "limit_price": f"{max(limit, 0.01):.2f}", "legs": rev}


def _submit_close(conn, tid, legs, qty, entry, structure, metric, reason, arm):
    """Shared submit/confirm/realize/resolve sequence for a spread close -- used by both the
    TP/SL path and the 0DTE force-close safety net below. `metric` is the close price (same
    non-negative dollar-magnitude convention closing_payload/the realized formula already used).
    Returns True if the close fired (dry-run preview OR a real fill), False if nothing happened
    (submit failed / zero fill -> caller should leave the trade OPEN for the next tick)."""
    if not arm:
        log.info(f"{tid}: {reason} would fire (dry-run, no order)")
        return True
    payload = closing_payload(legs, qty, abs(metric))
    log.info(f"{tid}: {reason} -> submit synthetic-IOC close @P={abs(metric):.2f}: {json.dumps(payload)}")
    try:
        resp = requests.post(PAPER + "/v2/orders", headers=H, json=payload, timeout=25)
        order = resp.json()
    except Exception as e:
        log.error(f"{tid}: close submit failed: {e}")
        return False
    oid = order.get("id")
    log.info(f"{tid}: close order {oid} HTTP {resp.status_code}; latency buffer {IOC_BUFFER_S}s")
    time.sleep(IOC_BUFFER_S)                        # synthetic-IOC buffer (cron-safe blocking)
    chk = requests.get(PAPER + f"/v2/orders/{oid}", headers=H, timeout=20).json()
    filled = int(float(chk.get("filled_qty", 0) or 0))
    status = chk.get("status")
    log.info(f"{tid}: post-buffer status={status} filled_qty={filled}/{qty}")
    if filled < qty:                                # force-cancel remainder => synthesize the 'C' in IOC
        d = requests.delete(PAPER + f"/v2/orders/{oid}", headers=H, timeout=20)
        log.info(f"{tid}: DELETE remainder -> HTTP {d.status_code}")
    if filled == 0:
        log.info(f"{tid}: no fill at P, leave OPEN for next tick")
        return False
    # Alpaca reports multi-leg filled_avg_price with a BUYS-minus-SELLS sign convention, so a
    # spread CLOSED for a net credit comes back NEGATIVE (close a debit spread for +0.40 credit
    # -> filled_avg_price = -0.40). The realized formula below wants the MAGNITUDE of the close
    # transaction (credit received to close a debit / debit paid to close a credit), both
    # non-negative -- so abs() it. Without this, a debit spread closed for a credit booked a
    # loss LARGER than its defined max risk (the -$625-on-$425-max FE row, 2026-06-29).
    exec_px = abs(float(chk.get("filled_avg_price") or abs(metric)))
    realized = ((entry - exec_px) if structure == "credit" else (exec_px - entry)) * 100 * filled
    remaining = qty - filled
    eval_apply_close(conn, tid, realized_pnl=realized, filled_qty=filled,
                     total_qty=qty, exec_price=exec_px)
    log.info(f"{tid}: {reason} closed {filled}/{qty} pnl=${realized:.2f} remaining={remaining}")
    return True


def process_exits(arm=False, tp_frac=TP_FRAC, sl_mult=SL_MULT):
    """Read OPEN/PARTIAL_CLOSE trades, check TP/SL on the live natural price, and on a trigger run a
    SYNTHETIC IOC: submit aggressive mleg at P_nat -> blocking sleep(2) -> force-cancel remainder.
    All ledger writes go through options_eval (BEGIN IMMEDIATE). Logs aggressively for fill tracing.
    Also force-closes any position with a leg expiring TODAY once past FORCE_CLOSE_HOUR:MINUTE ET,
    unconditionally (skips TP/SL) -- see the module-level 0DTE force-close comment above."""
    conn = eval_connect(); eval_init(conn)
    rows = conn.execute("SELECT trade_id, legs_metadata, initial_risk, status FROM trades_ledger "
                        "WHERE status IN ('OPEN','PARTIAL_CLOSE')").fetchall()
    log.info(f"process_exits: {len(rows)} open trade(s) to evaluate (arm={arm})")
    try:
        pend = json.loads(SL_PENDING_PATH.read_text())   # {trade_id: first_breach_unix_ts}
    except Exception:
        pend = {}
    now_et = datetime.now(ET)
    today_str = now_et.date().isoformat()
    force_close_now = now_et.time() >= dtime(FORCE_CLOSE_HOUR, FORCE_CLOSE_MINUTE)
    acted = 0
    for row in rows:
        tid = row["trade_id"]
        meta = json.loads(row["legs_metadata"])
        legs, entry, qty = meta["legs"], float(meta["entry_credit"]), int(meta["qty"])
        structure = meta.get("structure", "credit")
        raw = spread_close_cost(legs)                   # Σask(short) − Σbid(long) -- prices the ORDER
        if raw is None:
            log.warning(f"{tid}: legs unquoted, skip"); continue

        if force_close_now and any(_occ_expiry(l.get("occ")) == today_str for l in legs):
            log.info(f"{tid}: leg expires today, past {FORCE_CLOSE_HOUR}:{FORCE_CLOSE_MINUTE:02d} ET "
                     f"cutoff -- forcing close regardless of TP/SL (assignment-risk safety net)")
            pend.pop(tid, None)
            if _submit_close(conn, tid, legs, qty, entry, structure, raw, "0DTE_CLOSE", arm):
                acted += 1
            continue

        raw_mid = spread_close_mid(legs)                # mid mark -- makes the TP/SL DECISION
        if structure == "credit":
            metric = raw                                # debit to buy the spread back
            dmet = raw_mid if raw_mid is not None else raw
            tp = dmet <= meta.get("tp_close_cost", tp_frac * entry)
            sl = dmet >= meta.get("sl_close_cost", sl_mult * entry)
        else:                                           # debit / calendar: close value (credit to sell)
            metric = -raw
            dmet = -raw_mid if raw_mid is not None else metric
            tp = dmet >= meta.get("tp_close_cost", entry * (1 + tp_frac))
            sl = dmet <= meta.get("sl_close_cost", entry * (1 - tp_frac))
        log.info(f"{tid}: {structure} entry={entry:.2f} mid_metric={dmet:.2f} nat_metric={metric:.2f} TP={tp} SL={sl}")
        # SL persistence gate: crossing the spread converts one bad quote sample into a realized
        # loss, so a stop must survive SL_CONFIRM_S across ticks before we act on it. TP is a
        # favorable exit and fires immediately.
        if sl and not tp:
            first = pend.get(tid)
            if first is None:
                pend[tid] = time.time()
                log.info(f"{tid}: SL first sighting -- confirm after {SL_CONFIRM_S:.0f}s persistence")
                continue
            if time.time() - first < SL_CONFIRM_S:
                log.info(f"{tid}: SL pending confirmation ({time.time() - first:.0f}s elapsed)")
                continue
        elif tid in pend:
            pend.pop(tid, None)                          # breach healed -> reset
        if not (tp or sl):
            continue
        reason = "TP" if tp else "SL"
        pend.pop(tid, None)
        if _submit_close(conn, tid, legs, qty, entry, structure, metric, reason, arm):
            acted += 1
    try:
        SL_PENDING_PATH.write_text(json.dumps(pend))
    except Exception as e:
        log.warning(f"SL pending-state write failed (non-fatal): {e}")
    conn.close()
    return {"open": len(rows), "acted": acted}


def recipe_mleg_payload(spread, qty):
    """Opening mleg payload for a recipe-built spread (legs carry OCC symbols)."""
    legs = [{"symbol": l["occ"], "ratio_qty": "1",
             "side": "sell" if l["side"] == "SELL" else "buy",
             "position_intent": "sell_to_open" if l["side"] == "SELL" else "buy_to_open"}
            for l in spread["legs"]]
    return {"order_class": "mleg", "qty": str(qty), "type": "limit", "time_in_force": "day",
            "limit_price": f"{abs(spread['net']):.2f}", "legs": legs}


def notify_telegram(msg):
    """Fire a Telegram alert via openclaw. Fire-and-forget (detached) AND fully try/excepted so a slow
    or failed send can NEVER hang or crash the execution loop (2026-06-29). The order is already
    submitted + ledger-recorded before this is called, so a dropped notification is harmless --
    a hung order loop is not. Best-effort by design."""
    try:
        import subprocess
        # Detached (start_new_session) + all stdio to DEVNULL => returns INSTANTLY, the loop never
        # waits, and the send completes in the background even after this short-lived cron process
        # exits and even when the single-core box is busy (e.g. the 22:05 nightly options pipeline,
        # which starved a blocking 10s send into a timeout on 2026-06-29).
        subprocess.Popen(["openclaw", "message", "send", "--channel", "telegram",
                          "--target", "7590346809", "--message", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log.warning(f"telegram notify failed to launch (non-fatal): {e}")


def run_tournament(ticker, signal, direction, arm=False):
    """FULL batch entry loop: build every arm matching `signal` -> MILP gate (3 total / 2 per side /
    $100 risk) -> Discounted-Thompson-Sampling pick among survivors -> mleg payload with the arm's
    TP/SL recorded in the ledger. DRY-RUN unless arm=True."""
    import options_strategies as oss
    # CROSS-BOOK CORRELATION CAP (2026-07-05, heff): hard block on same-ticker overlap with the
    # equity books (mean-reversion + ORB). Fail-open by construction (cross_book_registry
    # returns {} on any read error), so a broken read here can never itself block an
    # otherwise-valid entry.
    import cross_book_registry
    if ticker.upper() in cross_book_registry.equity_open_tickers():
        log.info(f"  SKIP {ticker}: already open in equity book (mean-rev/ORB)")
        return {"candidates": 0, "chosen": None, "reason": "cross-book: ticker open in equity book"}
    recipes = oss.strategies_for_signal(signal)
    log.info(f"tournament {ticker} signal={signal} dir={direction}: {len(recipes)} candidate arms")
    candidates = []
    for rec in recipes:
        res, msg = propose_strategy(rec.sid, ticker, direction)
        if res is None:
            log.info(f"  {rec.sid}: skip ({msg})"); continue
        sp = res["recipe"]
        ev = oss.recipe_ev(sp)
        if ev <= 0:
            log.info(f"  {rec.sid}: skip (EV {ev:.3f}<=0)"); continue
        ps = ol.ProposedSpread(ticker=ticker, direction=direction, strategy_id=sp["sid"],
                               kind=sp["structure"], legs=[], net_credit=sp["net"],
                               max_loss=sp["max_loss"], ev=ev, pop=0.0,
                               r_multiple=(sp["net"] / sp["max_loss"] if sp["max_loss"] else 0),
                               p_mid=sp["net"], p_nat=sp["net"], utility=ev)
        candidates.append((sp, ps))
        log.info(f"  {rec.sid}: EV={ev:.3f} risk=${sp['max_loss']*100:.0f} ({sp['structure']})")
    if not candidates:
        return {"candidates": 0, "chosen": None, "reason": "no valid arms"}
    conn = eval_connect(); eval_init(conn)
    cur = conn.execute("SELECT legs_metadata FROM trades_ledger WHERE status IN "
                       "('OPEN','PARTIAL_CLOSE','PENDING')").fetchall()
    cur_bull = sum(1 for r in cur if json.loads(r["legs_metadata"]).get("direction") == 1)
    cur_bear = sum(1 for r in cur if json.loads(r["legs_metadata"]).get("direction") == -1)
    survivors = ol.select_portfolio_milp([ps for _, ps in candidates], cur_bull=cur_bull, cur_bear=cur_bear)
    sids = {ps.strategy_id for ps in survivors}
    surv = [(sp, ps) for sp, ps in candidates if ps.strategy_id in sids]
    log.info(f"  MILP survivors (bull={cur_bull} bear={cur_bear} open): {sorted(sids)}")
    if not surv:
        conn.close(); return {"candidates": len(candidates), "chosen": None, "reason": "MILP gate empty"}
    rng = np.random.default_rng()
    best, best_theta = None, -1.0
    for sp, ps in surv:
        row = conn.execute("SELECT alpha_param, beta_param FROM tournament_state WHERE strategy_id=?",
                           (ps.strategy_id,)).fetchone()
        a, b = (row["alpha_param"], row["beta_param"]) if row else (1.0, 1.0)
        theta = float(rng.beta(a, b))
        log.info(f"  DTS {ps.strategy_id}: Beta({a:.1f},{b:.1f}) -> theta={theta:.3f}")
        if theta > best_theta:
            best, best_theta = sp, theta
    qty = size_qty(best["max_loss"])
    # HARD PREMIUM/RISK CAP re-validation (2026-06-29, heff) -- defense in depth at the submission
    # boundary. size_qty already sizes on max_loss; for a DEBIT spread max_loss == the premium PAID,
    # so total max_loss*100*qty == total premium == the "$500 premium cap". Re-check the FINAL qty
    # here so no upstream change (MILP / DTS / recipe edit) can ever route an oversized order to the
    # broker: if the chosen qty would exceed the cap, fail SAFE (reject -> no trade) rather than submit.
    total_risk = best["max_loss"] * 100.0 * qty
    if qty > 0 and total_risk > PER_TRADE_RISK + 1e-6:
        log.error(f"  HARD-CAP REJECT {best['sid']}: ${total_risk:.0f} > ${PER_TRADE_RISK:.0f} cap "
                  f"(qty {qty}, max_loss {best['max_loss']:.2f}) -- failing safe, no trade")
        qty = 0
    # PRE-TRADE FRICTION GATE (2026-07-01, from the 6/30 forensics): reject arms whose own
    # bid-ask width already marks them at/near SL -- every 6/30 loser would have failed this.
    if qty > 0:
        ok_f, fmsg = entry_friction_ok({"legs": best["legs"], "entry_credit": best["net"],
                                        "structure": best["structure"],
                                        "sl_close_cost": best["sl_close_cost"]})
        if not ok_f:
            log.info(f"  FRICTION REJECT {best['sid']}: {fmsg}")
            conn.close()
            return {"candidates": len(candidates), "survivors": sorted(sids), "chosen": None,
                    "reason": f"friction gate: {fmsg}"}
    payload = recipe_mleg_payload(best, qty)
    meta = {"legs": best["legs"], "entry_credit": best["net"], "qty": qty, "max_loss": best["max_loss"],
            "kind": best["structure"], "structure": best["structure"], "direction": direction,
            "tp_frac": best["tp_frac"], "sl_frac": best["sl_frac"],
            "tp_close_cost": best["tp_close_cost"], "sl_close_cost": best["sl_close_cost"]}
    result = {"candidates": len(candidates), "survivors": sorted(sids), "chosen": best["sid"],
              "theta": round(best_theta, 3), "qty": qty, "risk": best["max_loss"] * 100 * qty,
              "tp_close_cost": best["tp_close_cost"], "sl_close_cost": best["sl_close_cost"],
              "payload": payload}
    log.info(f"  CHOSEN {best['sid']} theta={best_theta:.3f} qty={qty} risk=${result['risk']:.0f}")
    if arm and qty > 0:
        # TOURNAMENT DAILY-LOSS HALT (2026-07-01): the equity book's halt is now scoped to ITS
        # ledger, so this book brakes on its own realized P&L. Withhold-only; existing exits
        # still run every tick.
        realized_today = tournament_realized_today(conn)
        if realized_today <= -TOURNAMENT_DAILY_LOSS_LIMIT:
            log.error(f"  DAILY-LOSS HALT: tournament realized ${realized_today:.0f} today "
                      f"(limit -${TOURNAMENT_DAILY_LOSS_LIMIT:.0f}) -- no new entries")
            result["armed"] = False; result["blocked"] = "tournament daily-loss halt"
            conn.close(); return result
        # MAX-DRAWDOWN-FROM-PEAK HALT (2026-07-05, heff: 5x TOURNAMENT_DAILY_LOSS_LIMIT) --
        # the daily halt above resets every midnight regardless of a string of smaller losing
        # days; this one tracks the all-time peak of the tournament's own realized-P&L curve so
        # a slow multi-day bleed also gets caught. Withhold-only, same semantics as the daily halt.
        peak, current, drawdown = tournament_peak_drawdown(conn)
        if drawdown > MAX_DRAWDOWN_LIMIT:
            log.error(f"  MAX-DRAWDOWN HALT: tournament ${drawdown:.0f} below all-time peak "
                      f"${peak:.0f} (limit ${MAX_DRAWDOWN_LIMIT:.0f}) -- no new entries")
            result["armed"] = False; result["blocked"] = "tournament max-drawdown halt"
            conn.close(); return result
        import subprocess
        kc = subprocess.run([str(ROOT / "venv/bin/python"), str(ROOT / "guardrails.py"), "--kill-check"])
        if kc.returncode != 0:
            result["armed"] = False; result["blocked"] = "kill-switch"; conn.close(); return result
        regime = regime_for_entry(ticker)
        resp = requests.post(PAPER + "/v2/orders", headers=H, json=payload, timeout=25)
        result["http"] = resp.status_code
        if resp.status_code in (200, 201):
            oid = resp.json().get("id")
            # CONFLUENCE TAG (2026-07-05, heff's ask): INFORMATIONAL ONLY -- attaches a read of
            # the separate GEX/confluence pipeline to this trade's own metadata for future
            # analysis (options_confluence_outcome.py). Deliberately NOT a filter/gate: this
            # tournament has ~1 real trade ever, nowhere near enough history to validate a
            # signal against, and this codebase's standing discipline (confluence_score.py,
            # advanced_gex.py) treats GEX/confluence as diagnostic until real trade outcomes
            # prove otherwise. build_confluence_tag() fails open (all-null dict) on any missing/
            # malformed data, so a broken read here can never block or alter this entry.
            meta["confluence"] = build_confluence_tag(ticker, direction, signal)
            eval_record_open(conn, oid, best["sid"], meta, regime,
                             initial_risk=best["max_loss"] * 100 * qty, status=TradeStatus.PENDING)
            result["trade_id"] = oid; result["regime"] = regime.value
            log.info(f"  ARMED {best['sid']} trade_id={oid} regime={regime.value}")
            # New-trade Telegram alert (2026-06-29): symbol, spread type, qty, net entry, total risk.
            opt_name = "Call" if best["legs"][0]["type"] == "C" else "Put"
            spread_type = f"{opt_name} {best['structure'].capitalize()} Spread"
            legs_str = " / ".join(f"{l['side']} {l['strike']:g}{l['type']}" for l in best["legs"])
            notify_telegram(
                f"\U0001F3AF OPTIONS EXECUTED \u2014 {ticker}\n"
                f"{spread_type} ({best['sid']})\n"
                f"{legs_str}\n"
                f"Qty: {qty} | Net {best['structure']}: ${best['net']:.2f}/share\n"
                f"Total risk: ${best['max_loss'] * 100 * qty:.0f}")
    conn.close()
    return result


def stress_test():
    """FULL-STACK DRY-RUN STRESS: simulate a high-vol event where all 10 arms fire on BOTH sides.
    Verifies (1) the MILP gate holds 3-total/2-per-side under max load, (2) the Token Bucket throttles
    a request burst to the 190/min cap without runaway, (3) the live integrated path doesn't crash."""
    import options_strategies as oss
    ok = True

    def chk(n, g):
        nonlocal ok; ok &= bool(g); print(f"  [{'OK' if g else 'FAIL'}] {n}")

    print("=== FULL-STACK STRESS TEST (dry-run): all 10 arms × both sides ===")
    # (1) MILP gate under 20 simultaneous candidates (10 arms × bull/bear)
    pool = []
    for i, rec in enumerate(oss.STRATEGIES):
        for d in (1, -1):
            ev = 0.5 + 0.01 * i + (0.03 if d == 1 else 0.0)
            pool.append(ol.ProposedSpread("X", d, f"{rec.sid}{'B' if d == 1 else 'S'}", "x", [],
                                          0.3, 0.8, ev, 0.5, 0.375, 0.3, 0.3, ev))
    sel = ol.select_portfolio_milp(pool, cur_bull=0, cur_bear=0)
    nb = sum(1 for s in sel if s.direction == 1); ns = sum(1 for s in sel if s.direction == -1)
    chk(f"MILP picks <=3 of 20 candidates (got {len(sel)})", len(sel) <= 3)
    chk(f"MILP <=2 per side (bull={nb} bear={ns})", nb <= 2 and ns <= 2)
    sel2 = ol.select_portfolio_milp(pool, cur_bull=2, cur_bear=1)
    nb2 = sum(1 for s in sel2 if s.direction == 1); ns2 = sum(1 for s in sel2 if s.direction == -1)
    chk(f"MILP respects OPEN positions (2B/1S open -> <=0 bull,<=1 bear; got {nb2}/{ns2})", nb2 <= 0 and ns2 <= 1)
    chk("MILP survivors all positive-EV", all(s.ev > 0 for s in sel))

    # (2) Token Bucket. Scenario A — INSTANTANEOUS burst (clock frozen): 220 requests at once,
    # exactly what an all-10-arms event triggers. First 190 clear, next 30 must queue.
    refill = 190 / 60.0
    clk = {"t": 0.0}
    tb = ol.TokenBucket(capacity=190, refill_per_sec=refill, clock=lambda: clk["t"])
    burst = [tb.take(1) for _ in range(220)]                    # clock frozen -> no refill
    imm = sum(1 for w in burst if w == 0); thr = sum(1 for w in burst if w > 0)
    chk(f"TokenBucket burst: 190 clear / 30 queued (got {imm}/{thr})", imm == 190 and thr == 30)
    chk(f"TokenBucket burst: queue wait bounded, no runaway (max {max(burst):.3f}s)", 0 < max(burst) < 0.4)
    # Scenario B — SUSTAINED rate must equal the 190/min cap (drain, let 60s pass, count issuable).
    clk2 = {"t": 0.0}
    tb2 = ol.TokenBucket(capacity=190, refill_per_sec=refill, clock=lambda: clk2["t"])
    for _ in range(190):
        tb2.take(1)
    clk2["t"] = 60.0
    issued = 0
    while tb2.take(1) == 0 and issued < 1000:
        issued += 1
    chk(f"TokenBucket sustained: <=190 issued per minute (got {issued})", 185 <= issued <= 190)

    # (3) live integrated path — both signals, dry-run; throttled by the real _BUCKET
    _BUCKET_STATS.update(calls=0, throttled=0, waited_s=0.0)
    t0 = time.time()
    r1 = run_tournament("BAC", "ORB", 1, arm=False)
    r2 = run_tournament("BAC", "MR", -1, arm=False)
    chk("live ORB+MR tournament ran without crash", isinstance(r1, dict) and isinstance(r2, dict))
    print(f"  live: ORB->{r1.get('chosen') or r1.get('reason')} | MR->{r2.get('chosen') or r2.get('reason')}")
    print(f"  TokenBucket telemetry over live burst: {_BUCKET_STATS} in {time.time()-t0:.1f}s")
    print("STRESS", "PASS" if ok else "FAIL")
    return ok


def _selftest():
    """Pure regime-tagger checks ($0, no I/O)."""
    ok = True

    def chk(n, g):
        nonlocal ok; ok &= bool(g); print(f"  [{'OK' if g else 'FAIL'}] {n}")
    chk("POS_GEX_LOW_VIX", get_regime_tag(1e9, 15) == RegimeTag.POS_GEX_LOW_VIX)
    chk("POS_GEX_HIGH_VIX", get_regime_tag(1e9, 25) == RegimeTag.POS_GEX_HIGH_VIX)
    chk("NEG_GEX_LOW_VIX", get_regime_tag(-1e9, 15) == RegimeTag.NEG_GEX_LOW_VIX)
    chk("NEG_GEX_HIGH_VIX", get_regime_tag(-1e9, 25) == RegimeTag.NEG_GEX_HIGH_VIX)
    chk("VOL_EXPANSION (VVIX +20%)", get_regime_tag(1e9, 15, vvix=120, vvix_prev=100) == RegimeTag.VOL_EXPANSION_SHOCK)
    chk("VOL_EXPANSION (VIX +12%)", get_regime_tag(-1e9, 25, vix_prev=22.3) == RegimeTag.VOL_EXPANSION_SHOCK)
    chk("missing VIX -> UNKNOWN", get_regime_tag(1e9, None) == RegimeTag.UNKNOWN)
    chk("missing GEX -> UNKNOWN", get_regime_tag(None, 15) == RegimeTag.UNKNOWN)
    chk("close cost math", spread_close_cost.__name__ == "spread_close_cost")
    # --- $150 premium/risk cap (size_qty), 2026-06-29, lowered from $500 2026-07-02 ---
    chk("cap SCALES qty: $0.30 debit -> 5 contracts ($150<=150)", size_qty(0.30) == 5)
    chk("cap REJECTS single contract > $150: $1.60 debit -> 0 (no trade)", size_qty(1.60) == 0)
    chk("cap allows exactly the $150 boundary: $1.50 debit -> 1 contract", size_qty(1.50) == 1)
    chk("cap rejects just over: $1.51 debit -> 0", size_qty(1.51) == 0)
    chk("cap INVARIANT: sized total cost never exceeds $150",
        all(size_qty(ml) * ml * 100 <= PER_TRADE_RISK + 1e-6 for ml in [0.15, 0.30, 0.5, 0.95, 1.0, 1.5, 2.5]))
    # --- friction gate (2026-07-01, fixtures = the real 6/30 APA trade: debit 0.65, SL 0.33,
    # natural close read 0.13 two minutes after entry -> should have been rejected at entry) ---
    chk("friction: 6/30 APA-style wide debit spread REJECTED",
        _friction_verdict("debit", 0.65, mid_close=-0.40, nat_close=-0.13, sl_close_cost=0.33)[0] is False)
    chk("friction: healthy debit spread (mid ~ entry, nat above SL) ACCEPTED",
        _friction_verdict("debit", 0.65, mid_close=-0.63, nat_close=-0.55, sl_close_cost=0.33)[0] is True)
    chk("friction: debit mid-markdown > 25% rejected even if nat above SL",
        _friction_verdict("debit", 1.00, mid_close=-0.70, nat_close=-0.60, sl_close_cost=0.50)[0] is False)
    chk("friction: credit spread with natural buyback already >= SL rejected",
        _friction_verdict("credit", 0.50, mid_close=0.55, nat_close=1.05, sl_close_cost=1.00)[0] is False)
    chk("friction: healthy credit spread accepted",
        _friction_verdict("credit", 0.50, mid_close=0.52, nat_close=0.60, sl_close_cost=1.00)[0] is True)
    chk("friction: unquoted legs rejected",
        _friction_verdict("debit", 0.65, mid_close=None, nat_close=None, sl_close_cost=0.33)[0] is False)
    # --- max-drawdown-from-peak halt (2026-07-05) -- in-memory DB, still $0/no real I/O. Added
    # after a verification pass flagged this logic had no regression coverage of its own. ---
    import sqlite3
    _mem = sqlite3.connect(":memory:"); _mem.row_factory = sqlite3.Row
    _mem.execute("CREATE TABLE trades_ledger (status TEXT, realized_pnl REAL, exit_time INTEGER)")
    _mem.executemany("INSERT INTO trades_ledger VALUES (?,?,?)",
                     [("CLOSED", 1000, 1), ("CLOSED", 2000, 2), ("CLOSED", -2600, 3)])
    _, _, dd_breach = tournament_peak_drawdown(_mem)
    chk(f"drawdown INVARIANT: peak $3000 -> current $400 -> dd=${dd_breach:.0f} breaches ${MAX_DRAWDOWN_LIMIT:.0f} limit",
        dd_breach > MAX_DRAWDOWN_LIMIT)
    _mem.execute("DELETE FROM trades_ledger")
    _mem.executemany("INSERT INTO trades_ledger VALUES (?,?,?)",
                     [("CLOSED", 1000, 1), ("CLOSED", 2000, 2), ("CLOSED", -1000, 3)])
    _, _, dd_safe = tournament_peak_drawdown(_mem)
    chk(f"drawdown INVARIANT: peak $3000 -> current $2000 -> dd=${dd_safe:.0f} stays under ${MAX_DRAWDOWN_LIMIT:.0f} limit",
        dd_safe <= MAX_DRAWDOWN_LIMIT)
    _mem.close()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def propose_strategy(sid, ticker, direction, band=0.20):
    """Build a specific tournament arm (S1..S10) from the LIVE chain via its delta/width recipe."""
    import options_strategies as oss
    recipe = oss.BY_ID.get(sid)
    if not recipe:
        return None, f"unknown strategy {sid}"
    if recipe.structure in ("calendar", "diagonal"):           # S6 / S9 — dual expiration
        fd, bd = recipe.dte
        mc = fetch_multi_chain(ticker, fd, bd, band)
        if not mc:
            return None, f"{sid}: no dual-expiration chain ({fd}/{bd}DTE)"
        status, spread = oss.build_calendar(recipe, direction, mc["front_chain"], mc["back_chain"],
                                            mc["S"], mc["T_front"], mc["T_back"], R)
        if spread is None:
            return None, f"recipe build: {status}"
        return {"recipe": spread, "exp": f"{mc['exp_front']}/{mc['exp_back']}", "S": mc["S"],
                "T": mc["T_front"]}, "ok"
    S = spot(ticker)
    if S is None:
        return None, "no spot"
    dte = recipe.dte if isinstance(recipe.dte, int) else recipe.dte[0]
    exp = pick_expiration(ticker, dte, min_dte=max(dte, 0))
    if not exp:
        return None, f"no expiration near {dte}DTE"
    chain = fetch_chain(ticker, exp, S, band)
    if len(chain) < 4:
        return None, f"thin chain ({len(chain)})"
    T = chain[0]["T"]
    status, spread = oss.build_vertical(recipe, direction, chain, S, T, R)
    if spread is None:
        return None, f"recipe build: {status}"
    return {"recipe": spread, "exp": exp, "S": S, "T": T}, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker")
    ap.add_argument("--strategy", help="build a specific tournament arm S1..S10 (recipe mode)")
    ap.add_argument("--direction", type=int, choices=[1, -1])
    ap.add_argument("--z", type=float)
    ap.add_argument("--dte", type=int, default=35)
    ap.add_argument("--pop-min", type=float, default=0.60)
    ap.add_argument("--r-min", type=float, default=0.20)
    ap.add_argument("--arm", action="store_true", help="submit to PAPER (default: dry-run)")
    ap.add_argument("--live", action="store_true", help="(refused — real money is confirm-every-trade)")
    ap.add_argument("--exits", action="store_true", help="run the synthetic-IOC exit loop (TP/SL)")
    ap.add_argument("--regime", action="store_true", help="print the current regime tag for --ticker")
    ap.add_argument("--tournament", action="store_true", help="full batch entry loop for a signal")
    ap.add_argument("--signal", choices=["ORB", "MR"], help="signal type for --tournament")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--stress", action="store_true", help="full-stack dry-run stress test")
    a = ap.parse_args()

    if a.selftest:
        import sys; sys.exit(0 if _selftest() else 1)
    if a.stress:
        import sys; sys.exit(0 if stress_test() else 1)
    if a.regime:
        print(f"regime[{a.ticker}] = {regime_for_entry(a.ticker).value}"); return
    if a.exits:
        print("process_exits:", process_exits(arm=a.arm)); return
    if a.tournament:
        if not (a.ticker and a.signal and a.direction):
            ap.error("--tournament needs --ticker --signal --direction")
        res = run_tournament(a.ticker, a.signal, a.direction, arm=a.arm)
        print(f"=== tournament {a.ticker} {a.signal} dir={a.direction}  [{datetime.now(timezone.utc):%H:%MZ}] ===")
        print(f"  candidates={res.get('candidates')} survivors={res.get('survivors')} "
              f"-> CHOSEN={res.get('chosen')} (DTS theta={res.get('theta')})")
        if res.get("chosen"):
            print(f"  qty={res.get('qty')} risk=${res.get('risk'):.0f} "
                  f"TP@{res.get('tp_close_cost')} SL@{res.get('sl_close_cost')}")
            print(f"  payload: {json.dumps(res['payload'])}")
            if not a.arm:
                print("  DRY-RUN (no order). --arm to submit to PAPER + record to ledger.")
        else:
            print(f"  NO TRADE: {res.get('reason')}")
        return

    if a.live:
        print("REFUSED: real-money options are governed by confirm-every-trade, not this script.")
        return
    if a.strategy:
        if not (a.ticker and a.direction):
            ap.error("recipe mode needs --ticker --direction --strategy")
        res, msg = propose_strategy(a.strategy, a.ticker, a.direction)
        print(f"=== arm {a.strategy} {a.ticker} dir={a.direction}  [{datetime.now(timezone.utc):%H:%MZ}] ===")
        if res is None:
            print(f"  NO TRADE: {msg}"); return
        sp = res["recipe"]
        print(f"  spot={res['S']:.2f} exp={res['exp']} {sp['structure']} "
              f"SELL {sp['legs'][0]['strike']}{sp['opt']}(Δ{sp['short_delta_actual']}) / "
              f"BUY {sp['legs'][1]['strike']}{sp['opt']}(Δ{sp['long_delta_actual']})")
        print(f"  net={sp['net']:.2f} width={sp['width']} max_loss=${sp['max_loss']*100:.0f} "
              f"TP@{sp['tp_close_cost']:.2f} SL@{sp['sl_close_cost']:.2f}  (DRY-RUN)")
        return
    if not (a.ticker and a.direction and a.z is not None):
        ap.error("entry mode needs --ticker --direction --z")

    res, msg = propose(a.ticker, a.direction, a.z, a.dte, pop_min=a.pop_min, r_min=a.r_min)
    print(f"=== options_orchestrator {a.ticker} dir={a.direction} z={a.z}  [{datetime.now(timezone.utc):%H:%MZ}] ===")
    if res is None:
        print(f"  NO TRADE: {msg}")
        return
    sp = res["spread"]
    qty = size_qty(sp.max_loss)
    if qty == 0:
        print(f"  NO TRADE: best spread risks ${sp.max_loss*100:.0f}/contract > ${PER_TRADE_RISK:.0f} cap.")
        return
    print(f"  spot={res['S']:.2f}  exp={res['exp']}  esscher mu={res['mu']:+.4f} theta={res['theta']:+.4f}")
    print(f"  {sp.kind}: SELL {sp.legs[0].strike}{sp.legs[0].option_type} / BUY {sp.legs[1].strike}{sp.legs[1].option_type}")
    print(f"  credit={sp.net_credit:.2f} max_loss={sp.max_loss:.2f} PoP={sp.pop:.2f} R={sp.r_multiple:.2f} "
          f"EV={sp.ev:.4f} util={sp.utility:.3f}  qty={qty} (risk ${sp.max_loss*100*qty:.0f})")
    payload = mleg_payload(sp, qty)
    print("  mleg payload:", json.dumps(payload))

    if not a.arm:
        print("  DRY-RUN (no order submitted). Re-run with --arm to submit to PAPER.")
        return
    # ---- arm: paper submit, gated by the existing kill-switch ----
    import subprocess
    kc = subprocess.run([str(ROOT / "venv/bin/python"), str(ROOT / "guardrails.py"), "--kill-check"])
    if kc.returncode != 0:
        print("  BLOCKED: kill-switch engaged (guardrails --kill-check)."); return
    resp = requests.post(PAPER + "/v2/orders", headers=H, json=payload, timeout=25)
    print(f"  PAPER submit -> HTTP {resp.status_code}: {str(resp.text)[:300]}")
    if resp.status_code in (200, 201):
        # ledger OWNS the trade record (options_eval), never hand-written. Regime tag captured at entry.
        order = resp.json()
        legs = [{"strike": l.strike, "type": l.option_type, "side": l.side,
                 "occ": getattr(l, "_occ", None)} for l in sp.legs]
        meta = {"legs": legs, "entry_credit": sp.net_credit, "qty": qty,
                "max_loss": sp.max_loss, "kind": sp.kind}
        regime = regime_for_entry(a.ticker)
        conn = eval_connect(); eval_init(conn)
        eval_record_open(conn, order.get("id"), sp.strategy_id, meta, regime,
                         initial_risk=sp.max_loss * 100 * qty, status=TradeStatus.PENDING)
        conn.close()
        log.info(f"recorded {order.get('id')} strat={sp.strategy_id} regime={regime.value} "
                 f"risk=${sp.max_loss*100*qty:.0f}")
        print(f"  recorded to ledger: trade_id={order.get('id')} regime={regime.value}")


if __name__ == "__main__":
    main()
