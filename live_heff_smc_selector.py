"""Phase 2 of live validated-strategy wiring: contract selection off real
live data, using the EXACT same bt2_selector.select_contract() logic
validated in backtest. Reuses options_orchestrator.py's own auth/throttle/
spot/pick_expiration (same credentials, same rate-limit bucket as the live
tournament -- not a second, uncoordinated Alpaca consumer).

LIVE_SELECTOR_CONFIG: REVERTED 2026-07-31 (P0 repair) to moderate_combo
(min_abs_delta=0.10, premium band $0.15-$0.40).

Stated plainly, because this changed twice in one day:
* Earlier on 2026-07-31 this was promoted to wide_premium_020_100 (premium
  $0.20-$1.00) on a 162-session sweep showing fill rate 27.7%->52.8%,
  sessions-with-a-fill 74.1%->94.4%, expectancy $8.07->$11.91/trade.
* That promotion is WITHDRAWN, for two independent reasons:
  1. The sweep was EXPLORATORY, over history already mined repeatedly for
     parameter selection (B0, B1, a 30-variant indicator sweep, and two rounds
     of selector sweeps). BT0_CHARTER.md Section 13's nested-selection rule
     forbids treating such a result as validated, and the charter's 2026-07-31
     addendum closed that development window. Promoting off it was selection on
     already-used data.
  2. Every expectancy figure in that sweep was produced under the P&L accounting
     bug fixed the same day (bt2_fills.friction_metrics: premium-unit slippage
     subtracted from dollar midpoint P&L). Those numbers are inflated, so the
     comparison that justified the promotion does not stand as measured.

Re-promotion requires ONE frozen candidate config, re-measured under the
corrected accounting, tested ONCE against the forward-only validation window
opened 2026-07-31 -- not another sweep over development history.

SHADOW MODE: reads new triggers from live_heff_smc_detector.py's output,
selects a real contract against LIVE quotes, and logs what WOULD be
entered -- does NOT place an order. Phase 3 (real order submission) is
built and tested separately, after this is verified live against real
signals, same staged-rollout discipline as the rest of this codebase.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import options_orchestrator as oo
from thetadata_pipeline.bt2_schemas import DIRECTION_TO_RIGHT
from thetadata_pipeline.bt2_selector import (
    SelectorConfig, evaluate_candidate, point_in_time_delta, select_contract,
)

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
TRIGGERS_PATH = ROOT / "data" / "live_heff_smc" / "triggers.jsonl"
PROCESSED_PATH = ROOT / "data" / "live_heff_smc" / "phase2_processed.json"
SHADOW_LOG_PATH = ROOT / "data" / "live_heff_smc" / "phase2_shadow_decisions.jsonl"

LIVE_SELECTOR_CONFIG = SelectorConfig(min_abs_delta=0.10, premium_low=0.15, premium_high=0.40)
DIRECTION_LONG = "CALL WATCH"
DIRECTION_SHORT = "PUT WATCH"
SIDE_TO_DIRECTION = {"long": DIRECTION_LONG, "short": DIRECTION_SHORT}


def _load_processed() -> set:
    if not PROCESSED_PATH.exists():
        return set()
    try:
        return set(json.loads(PROCESSED_PATH.read_text()))
    except Exception:
        return set()


def _save_processed(s: set) -> None:
    tmp = PROCESSED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(s)))
    tmp.replace(PROCESSED_PATH)


def _load_new_triggers(processed: set) -> list:
    if not TRIGGERS_PATH.exists():
        return []
    out = []
    for line in TRIGGERS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        key = f"{rec['session']}:{rec['bar_index']}:{rec['side']}"
        if key not in processed:
            out.append((key, rec))
    return out


def fetch_live_book(ticker: str, right: str) -> pd.DataFrame:
    """Real live chain via options_orchestrator's own auth/throttle
    (same rate-limit bucket the live tournament uses -- one coordinated
    Alpaca consumer, not two). Captures bid_size/ask_size/quote timestamp
    too (fetch_chain doesn't retain these), needed by select_contract."""
    S = oo.spot(ticker)
    if not S:
        return pd.DataFrame()
    exp = oo.pick_expiration(ticker, dte_target=1, min_dte=0)
    if not exp:
        return pd.DataFrame()

    band = 0.10
    lo, hi = S * (1 - band), S * (1 + band)
    sc, c = oo._get(oo.PAPER, "/v2/options/contracts", underlying_symbols=ticker,
                     expiration_date=exp, strike_price_gte=f"{lo:.2f}", strike_price_lte=f"{hi:.2f}",
                     limit=10000)
    if sc != 200:
        return pd.DataFrame()
    rows = {x["symbol"]: x for x in c.get("option_contracts", []) if x["type"] == ("call" if right == "C" else "put")}
    if not rows:
        return pd.DataFrame()

    now_utc = dt.datetime.now(dt.timezone.utc)
    book_rows = []
    syms = list(rows)
    for i in range(0, len(syms), 100):
        batch = syms[i:i + 100]
        sc, q = oo._get(oo.DATA, "/v1beta1/options/quotes/latest", symbols=",".join(batch), feed="opra")
        if sc != 200:
            continue
        for sym, quote in q.get("quotes", {}).items():
            bp, ap = quote.get("bp", 0), quote.get("ap", 0)
            if not (bp > 0 and ap > 0):
                continue
            meta = rows[sym]
            K = float(meta["strike_price"])
            mid = 0.5 * (bp + ap)
            delta = point_in_time_delta(mid, S, K, exp, now_utc, right, oo.R)
            if delta is None:
                continue
            qts = quote.get("t")
            try:
                if not qts:
                    raise ValueError("missing OPRA quote timestamp")
                quote_age = (now_utc - dt.datetime.fromisoformat(qts.replace("Z", "+00:00"))).total_seconds()
            except (AttributeError, TypeError, ValueError):
                quote_age = None
            book_rows.append({
                "strike": K, "right": right, "expiration": exp,
                "bid": bp, "ask": ap, "delta": delta,
                "bid_size": quote.get("bs", 999), "ask_size": quote.get("as", 999),
                "quote_age_seconds": max(quote_age, 0.0) if quote_age is not None else None,
            })
    return pd.DataFrame(book_rows)


def _near_miss_diagnostics(book: pd.DataFrame, decision_ts, config: SelectorConfig) -> dict:
    """When nothing passes, tallies WHICH rule(s) blocked each checked candidate and surfaces
    the closest miss (fewest failing rules) -- so a 'no_candidate_passed_all_rules' day is
    diagnosable (was it the premium band? delta floor? stale/thin quotes?) instead of a black
    box, which is all the shadow log gave before 2026-07-30 (a whole day of zero real fills with
    no way to tell why). Read-only: reuses bt2_selector.evaluate_candidate exactly as
    select_contract does internally, never affects the actual PASS/FAIL selection."""
    decision_date = pd.Timestamp(decision_ts).date()
    reason_counts = Counter()
    closest = None
    for _, row in book.iterrows():
        c = evaluate_candidate(row, decision_date, config)
        for r in c["reasons"]:
            if r.startswith("no valid two-sided quote"):
                reason_counts["no_two_sided_quote"] += 1
            elif "premium band" in r:
                reason_counts["premium_band"] += 1
            elif "delta" in r:
                reason_counts["delta_floor"] += 1
            elif "spread" in r and "% of mid" in r:
                reason_counts["spread_pct"] += 1
            elif "spread" in r:
                reason_counts["spread_dollars"] += 1
            elif "quote age" in r:
                reason_counts["quote_age"] += 1
            elif "ask size" in r:
                reason_counts["ask_size"] += 1
            elif "DTE" in r:
                reason_counts["dte"] += 1
            else:
                reason_counts["other"] += 1
        if closest is None or len(c["reasons"]) < len(closest["reasons"]):
            closest = c
    return {
        "candidates_checked": len(book),
        "fail_reason_counts": dict(reason_counts),
        "closest_candidate": {k: v for k, v in closest.items() if k != "_delta_distance"} if closest else None,
    }


def process_trigger(key: str, rec: dict) -> dict:
    ticker = rec["ticker"]
    direction = SIDE_TO_DIRECTION[rec["side"]]
    right = DIRECTION_TO_RIGHT[direction]

    book = fetch_live_book(ticker, right)
    if book.empty:
        decision = {"key": key, "trigger": rec, "found": False, "reason": "no_candidates_in_book"}
    else:
        now_et = dt.datetime.now(ET)
        selection = select_contract(book, right, now_et, LIVE_SELECTOR_CONFIG)
        if selection.found:
            decision = {"key": key, "trigger": rec, "found": True, "contract": selection.contract,
                        "candidates_checked": selection.candidates_checked}
        else:
            decision = {"key": key, "trigger": rec, "found": False, "reason": selection.reason,
                        "candidates_checked": selection.candidates_checked,
                        "near_miss": _near_miss_diagnostics(book, now_et, LIVE_SELECTOR_CONFIG)}

    decision["decided_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    decision["mode"] = "SHADOW_NO_ORDER_PLACED"
    return decision


def main():
    processed = _load_processed()
    new = _load_new_triggers(processed)
    if not new:
        print(json.dumps({"status": "no_new_triggers"}))
        return

    (ROOT / "data" / "live_heff_smc").mkdir(parents=True, exist_ok=True)
    results = []
    for key, rec in new:
        decision = process_trigger(key, rec)
        results.append(decision)
        with open(SHADOW_LOG_PATH, "a") as f:
            f.write(json.dumps(decision, default=str) + "\n")
        processed.add(key)
        print(json.dumps({"key": key, "found": decision["found"],
                           "reason_or_contract": decision.get("reason") or decision.get("contract", {}).get("strike"),
                           "fail_reason_counts": decision.get("near_miss", {}).get("fail_reason_counts")}))

    _save_processed(processed)
    print(json.dumps({"status": "ok", "processed": len(results)}))


if __name__ == "__main__":
    main()
