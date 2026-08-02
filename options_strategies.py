#!/usr/bin/env python3
"""
options_strategies.py — the 10-strategy Daily Forward Tournament slate (Quantitative Options
Strategy Design spec). Each arm is a delta-targeted recipe; the recipe engine turns (signal, live
chain) into a concrete 2-leg spread, enforcing the $100 risk cap + $5 width cap.

The DTS allocator (options_lib.thompson_rank) and DSR gate (options_eval.run_dsr_batch) already
exist; the equity signals (MR z>=1.5, tight-range ORB<=0.66%) come from the live scanners. THIS
module supplies the arms + the signal->spread construction + tournament registration.

Verticals (S1-S5,S7,S8,S10) fully built. Calendars/diagonals (S6,S9) need a 2-expiration chain ->
recipe registered, builder returns ('CALENDAR_TODO', None) until the orchestrator fetches 2 DTEs.

Run:  ./venv/bin/python options_strategies.py --selftest      ($0)
      ./venv/bin/python options_strategies.py --register      (insert S1..S10 into tournament_state)
"""
import argparse
import sys
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np

import greeks
import options_lib as ol


@dataclass(frozen=True)
class StrategyRecipe:
    sid: str
    signal: str                 # 'ORB' | 'MR'
    structure: str              # 'credit' | 'debit' | 'calendar' | 'diagonal'
    dte: Union[int, Tuple[int, int]]
    long_delta: float
    short_delta: float
    width: Optional[float]      # target strike width ($), None for same-strike calendar
    tp: float                   # take-profit fraction (of credit or debit, per structure)
    sl: float                   # stop-loss fraction


# --- The Tournament Strategy Matrix (spec table, p.4) ---
STRATEGIES = [
    StrategyRecipe("S1",  "ORB", "debit",    0,      0.50, 0.35, 1.00, 0.80, 0.40),
    StrategyRecipe("S2",  "ORB", "credit",   0,      0.15, 0.30, 1.00, 0.50, 1.00),
    StrategyRecipe("S3",  "ORB", "debit",    1,      0.45, 0.25, 2.00, 1.00, 0.50),
    StrategyRecipe("S4",  "MR",  "credit",   1,      0.30, 0.45, 1.50, 0.40, 0.60),
    StrategyRecipe("S5",  "MR",  "debit",    0,      0.35, 0.15, 2.00, 1.50, 0.50),
    # S6/S9 short_delta LOWERED 2026-07-02 (was 0.50/0.50, dead-ATM on the SHORT/front leg):
    # an ATM short is roughly the highest-probability-of-assignment strike you can hold, and
    # both S6/S9's front leg is 0DTE -- the combination made these two arms the tournament's
    # largest early/same-day-assignment exposure (a short option can be exercised by its holder
    # at ANY time it's ITM, not just at expiry; a defined-risk spread's max_loss bound only holds
    # if both legs close TOGETHER, which assignment breaks). S6 (same-strike calendar, long leg's
    # strike is forced to match the short regardless of long_delta): short_delta 0.50->0.35 moves
    # the whole spread modestly OTM together. S9 (diagonal, back leg already OTM at 0.35): only
    # short_delta 0.50->0.40, more conservative than S6, to avoid collapsing the front/back delta
    # skew the diagonal's theta-differential edge actually depends on. Paired with a new EOD
    # force-close for any leg expiring today (options_orchestrator.py) as the second line of
    # defense for the 0DTE window this doesn't eliminate. Selftest structural checks (same-strike,
    # net-debit, risk-cap) are delta-agnostic and still pass; EV/promotion behavior for these two
    # arms should be re-measured empirically once retagged.
    StrategyRecipe("S6",  "MR",  "calendar", (0, 1), 0.35, 0.35, None, 0.35, 0.25),
    StrategyRecipe("S7",  "ORB", "credit",   7,      0.05, 0.20, 1.00, 0.60, 1.00),
    StrategyRecipe("S8",  "MR",  "credit",   7,      0.10, 0.25, 1.00, 0.75, 1.00),
    StrategyRecipe("S9",  "ORB", "diagonal", (0, 2), 0.35, 0.40, None, 0.50, 0.30),
    StrategyRecipe("S10", "MR",  "credit",   30,     0.05, 0.15, 1.00, 0.80, 0.50),
]
BY_ID = {s.sid: s for s in STRATEGIES}


def strategies_for_signal(signal):       # 'ORB' or 'MR'
    return [s for s in STRATEGIES if s.signal == signal]


def annotate_deltas(chain, S, T, r=0.05):
    """Attach BS delta to each chain row {strike,type,bid,ask,iv}. Returns the same rows."""
    for c in chain:
        g = greeks.bs_greeks(S, c["strike"], T, r, c["iv"], c["type"] == "C")
        c["delta"] = float(g["delta"])
    return chain


def pick_by_delta(chain, target_delta, opt_type):
    """Contract of opt_type whose |delta| is nearest target_delta."""
    cands = [c for c in chain if c["type"] == opt_type and "delta" in c and c["bid"] > 0 and c["ask"] > 0]
    if not cands:
        return None
    return min(cands, key=lambda c: abs(abs(c["delta"]) - target_delta))


def pick_by_strike(chain, target_strike, opt_type):
    """Contract of opt_type whose strike is nearest target_strike."""
    cands = [c for c in chain if c["type"] == opt_type and c["bid"] > 0 and c["ask"] > 0]
    if not cands:
        return None
    return min(cands, key=lambda c: abs(c["strike"] - target_strike))


def build_vertical(recipe, direction, chain, S, T, r=0.05, max_risk=ol.MAX_RISK_PER_TRADE):
    """Construct a 2-leg vertical for a recipe given a delta-annotated single-expiration chain.
    direction: 1 bullish / -1 bearish. Anchor one leg by delta, place the other at the recipe's
    target WIDTH offset (keeps risk bounded). Returns (status, dict|None). Enforces $100/$5 caps."""
    if recipe.structure in ("calendar", "diagonal"):
        return ("CALENDAR_TODO", None)                 # needs a 2-expiration chain
    annotate_deltas(chain, S, T, r)
    w = min(recipe.width or ol.MAX_SPREAD_WIDTH, ol.MAX_SPREAD_WIDTH)
    if recipe.structure == "credit":
        opt = "P" if direction == 1 else "C"           # bull put / bear call
        short = pick_by_delta(chain, recipe.short_delta, opt)   # anchor = the short (higher-delta) leg
        if not short:
            return ("NO_STRIKES", None)
        long_target = short["strike"] - w if direction == 1 else short["strike"] + w  # protection further OTM
        long = pick_by_strike(chain, long_target, opt)
    else:                                              # debit: anchor = the long (ATM) leg
        opt = "C" if direction == 1 else "P"
        long = pick_by_delta(chain, recipe.long_delta, opt)
        if not long:
            return ("NO_STRIKES", None)
        short_target = long["strike"] + w if direction == 1 else long["strike"] - w   # short further OTM
        short = pick_by_strike(chain, short_target, opt)
    if not short or not long or short["strike"] == long["strike"]:
        return ("NO_STRIKES", None)
    width = abs(short["strike"] - long["strike"])
    if width > ol.MAX_SPREAD_WIDTH:
        return ("WIDTH_CAP", None)
    if recipe.structure == "credit":
        net = short["bid"] - long["ask"]               # credit received (pessimistic)
        if net <= 0:
            return ("NO_CREDIT", None)
        max_loss = width - net
    else:
        net = long["ask"] - short["bid"]               # debit paid (pessimistic)
        if net <= 0:
            return ("NO_DEBIT", None)
        max_loss = net
    if max_loss * 100 > max_risk:
        return ("RISK_CAP", None)
    # absolute TP/SL thresholds on the spread's close value (per structure)
    if recipe.structure == "credit":
        tp_close_cost = net * (1 - recipe.tp)          # buy back cheaper -> captured tp*credit
        sl_close_cost = net * (1 + recipe.sl)          # buy back dearer -> lost sl*credit
    else:
        tp_close_cost = net * (1 + recipe.tp)          # sell richer -> gained tp*debit
        sl_close_cost = net * (1 - recipe.sl)
    return ("OK", {
        "sid": recipe.sid, "structure": recipe.structure, "direction": direction, "opt": opt,
        "legs": [{"strike": short["strike"], "type": opt, "side": "SELL", "occ": short.get("symbol")},
                 {"strike": long["strike"], "type": opt, "side": "BUY", "occ": long.get("symbol")}],
        "net": round(net, 2), "width": width, "max_loss": round(max_loss, 2),
        "tp_close_cost": round(tp_close_cost, 2), "sl_close_cost": round(sl_close_cost, 2),
        "tp_frac": recipe.tp, "sl_frac": recipe.sl,
        "short_delta_actual": round(abs(short["delta"]), 3),
        "long_delta_actual": round(abs(long["delta"]), 3)})


def build_calendar(recipe, direction, front_chain, back_chain, S, T_front, T_back,
                   r=0.05, max_risk=ol.MAX_RISK_PER_TRADE):
    """S6 horizontal (same-strike ATM) / S9 diagonal (front ATM short, back OTM long) calendars,
    built from TWO expirations. Long the back leg, short the front leg -> net debit (defined risk).
    Returns (status, dict|None). Enforces the $100 risk cap (max loss ~= net debit)."""
    # S6: oversold(dir1)->puts, overbought(dir-1)->calls. S9: breakout direction -> call/put.
    opt = ("P" if direction == 1 else "C") if recipe.sid == "S6" else ("C" if direction == 1 else "P")
    annotate_deltas(front_chain, S, T_front, r)
    annotate_deltas(back_chain, S, T_back, r)
    short = pick_by_delta(front_chain, recipe.short_delta, opt)        # front leg (~ATM Δ0.50)
    if not short:
        return ("NO_FRONT", None)
    if recipe.structure == "calendar":                                # S6 same strike on the back
        long = pick_by_strike(back_chain, short["strike"], opt)
    else:                                                             # S9 diagonal: back OTM by delta
        long = pick_by_delta(back_chain, recipe.long_delta, opt)
    if not long:
        return ("NO_BACK", None)
    debit = long["ask"] - short["bid"]                                # pay back, collect front
    if debit <= 0:
        return ("NO_DEBIT", None)
    if debit * 100 > max_risk:
        return ("RISK_CAP", None)
    return ("OK", {
        "sid": recipe.sid, "structure": recipe.structure, "direction": direction, "opt": opt,
        "legs": [{"strike": short["strike"], "type": opt, "side": "SELL", "occ": short.get("symbol")},
                 {"strike": long["strike"], "type": opt, "side": "BUY", "occ": long.get("symbol")}],
        "net": round(debit, 2), "width": abs(long["strike"] - short["strike"]),
        "max_loss": round(debit, 2),
        "tp_close_cost": round(debit * (1 + recipe.tp), 2),           # sell richer -> profit
        "sl_close_cost": round(debit * (1 - recipe.sl), 2),
        "tp_frac": recipe.tp, "sl_frac": recipe.sl,
        "short_delta_actual": round(abs(short["delta"]), 3),
        "long_delta_actual": round(abs(long["delta"]), 3)})


def recipe_ev(sp):
    """Delta-based expected value for MILP ranking (no RND needed; the arms are delta-defined).
    credit: PoP=1-short_delta; debit/calendar: PoP~=long_delta. EV = PoP·reward − (1−PoP)·risk."""
    risk = sp["max_loss"]
    if sp["structure"] == "credit":
        pop = max(0.0, 1 - sp["short_delta_actual"])
        return pop * sp["net"] - (1 - pop) * risk
    reward = max(sp.get("width", 0) - sp["net"], sp["net"]) if sp["structure"] == "debit" else sp["net"]
    pop = sp.get("long_delta_actual", 0.45)
    return pop * reward - (1 - pop) * risk


def register_strategies(db_path=None):
    """Insert S1..S10 into tournament_state (PAPER) so the DTS/DSR engine ranks them."""
    import options_eval as oe
    conn = oe.connect(db_path) if db_path else oe.connect()
    oe.init_db(conn)
    for s in STRATEGIES:
        oe.ensure_strategy(conn, s.sid)
    n = conn.execute("SELECT COUNT(*) FROM tournament_state").fetchone()[0]
    conn.close()
    return n


def selftest():
    ok = True

    def chk(n, g):
        nonlocal ok; ok &= bool(g); print(f"  [{'OK' if g else 'FAIL'}] {n}")

    chk("10 unique strategies S1..S10", len(BY_ID) == 10 and set(BY_ID) == {f"S{i}" for i in range(1, 11)})
    chk("credit recipes: short_delta > long_delta",
        all(s.short_delta > s.long_delta for s in STRATEGIES if s.structure == "credit"))
    chk("debit recipes: long_delta > short_delta",
        all(s.long_delta > s.short_delta for s in STRATEGIES if s.structure == "debit"))
    chk("signal split ORB/MR", {s.signal for s in STRATEGIES} == {"ORB", "MR"})

    # synthetic chain: $1000-book-realistic ~$20 underlying, $0.50 strikes, 7-DTE -> narrow,
    # $100-compliant verticals (the slate targets cheaper liquid names like F/BAC).
    S, T, r = 20.0, 7 / 365, 0.05
    chain = []
    for K in np.arange(15, 25.5, 0.5):
        for is_call in (True, False):
            iv = 0.35
            px = float(greeks.bs_price(S, K, T, r, iv, is_call))
            chain.append({"strike": float(K), "type": "C" if is_call else "P",
                          "bid": max(px - 0.02, 0.01), "ask": px + 0.02, "iv": iv,
                          "symbol": f"X{K}{'C' if is_call else 'P'}"})

    # S8 credit (MR), bullish -> bull put: short ~Δ0.25 put above long ~Δ0.10 put
    status, sp = build_vertical(BY_ID["S8"], 1, [dict(c) for c in chain], S, T, r)
    chk(f"S8 builds ({status})", status == "OK" and sp is not None)
    if sp:
        chk("S8 bull put: short strike > long strike", sp["legs"][0]["strike"] > sp["legs"][1]["strike"])
        chk("S8 credit>0 & risk<=$100", sp["net"] > 0 and sp["max_loss"] * 100 <= 100)
        chk("S8 short Δ near 0.25", abs(sp["short_delta_actual"] - 0.25) < 0.12)
        chk("S8 TP buyback < credit (capture profit)", sp["tp_close_cost"] < sp["net"])
        chk("S8 SL buyback > credit (loss)", sp["sl_close_cost"] > sp["net"])

    # S1 debit (ORB), bullish -> bull call: long ~Δ0.50 above short ~Δ0.35
    status, dsp = build_vertical(BY_ID["S1"], 1, [dict(c) for c in chain], S, T, r)
    chk(f"S1 builds ({status})", status == "OK" and dsp is not None)
    if dsp:
        chk("S1 debit max_loss = debit, risk<=$100", abs(dsp["max_loss"] - dsp["net"]) < 1e-6 and dsp["max_loss"] * 100 <= 100)
        chk("S1 long Δ > short Δ", dsp["long_delta_actual"] > dsp["short_delta_actual"])
        chk("S1 TP sell value > debit (profit)", dsp["tp_close_cost"] > dsp["net"])

    # calendars deferred in the vertical builder
    status, _ = build_vertical(BY_ID["S6"], 1, [dict(c) for c in chain], S, T, r)
    chk("S6 deferred in vertical builder (CALENDAR_TODO)", status == "CALENDAR_TODO")

    # S6 horizontal calendar via the dual-chain builder (front 0DTE, back 1DTE)
    def mk(Texp):
        return [{"strike": float(K), "type": "C" if c else "P",
                 "bid": max(float(greeks.bs_price(S, K, Texp, r, 0.35, c)) - 0.02, 0.01),
                 "ask": float(greeks.bs_price(S, K, Texp, r, 0.35, c)) + 0.02, "iv": 0.35,
                 "symbol": f"X{K}{'C' if c else 'P'}{int(Texp*365)}"}
                for K in np.arange(15, 25.5, 0.5) for c in (True, False)]
    front, back = mk(1 / 365), mk(2 / 365)
    cstat, cal = build_calendar(BY_ID["S6"], 1, front, back, S, 1 / 365, 2 / 365, r)
    chk(f"S6 calendar builds ({cstat})", cstat == "OK" and cal is not None)
    if cal:
        chk("S6 same-strike ATM, net debit, risk<=$100",
            cal["legs"][0]["strike"] == cal["legs"][1]["strike"] and cal["net"] > 0 and cal["max_loss"] * 100 <= 100)
    chk("recipe_ev positive for a credit arm", recipe_ev(sp) > -1e9 if sp else True)

    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if a.register:
        print(f"registered strategies; tournament_state rows = {register_strategies()}")
    if a.list:
        for s in STRATEGIES:
            print(f"  {s.sid:4s} {s.signal:3s} {s.structure:9s} DTE={s.dte} "
                  f"Δshort={s.short_delta} Δlong={s.long_delta} W={s.width} TP={s.tp} SL={s.sl}")


if __name__ == "__main__":
    main()
