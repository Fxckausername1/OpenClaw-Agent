#!/usr/bin/env python3
"""portfolio_sim.py — What did the REAL 3-slot/2-per-side/$800 portfolio gate actually
capture, vs the raw sum of every signal the scanners fired?

paper_trades.csv / orb_paper_trades.csv track EVERY signal as if it were independently
tradeable -- that's correct for measuring raw signal quality (the backtest question: "does
this generator have edge") but overstates what a real, capacity-constrained book realizes
(the trading question: "what would I actually have made"). This replays every closed signal
in chronological order through the ACTUAL portfolio_gate.select_portfolio() (imported
directly, not reimplemented, so this can never silently drift from the live gate logic),
maintaining a real open-position ledger (freed only when a position's own close_time
passes), and reports P&L for the ADMITTED subset vs the WITHHELD subset.

KNOWN LIMITATION (labeled, not hidden): does not model the 2026-06-29/30
Prove-It-Or-Lose-It rotation logic (cut-loser / choke-winner on a capacity-rejected
signal) -- that needs intraday mark-to-market on open positions at the instant of
rejection, which isn't available from these EOD-summary CSVs. So this UNDERSTATES what
the real system captures today; it's a clean lower bound on the capped-book edge, not
the final word.
"""
import sys
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio_gate as pg

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ET = "America/New_York"


ORB_RR_CAP = 5.0


def apply_smart_orb_rr(orb_df):
    """Retroactively applies the 2026-07-01 range-compression planned_rr to historical ORB
    rows (they were logged before the orb_scanner.py patch shipped, so planned_rr is NaN ->
    the gate treated every one as NEUTRAL_RR=2.0). Reconstructs the opening range from
    entry/stop: orh=max, orl=min regardless of side (LONG entry=orh/stop=orl, SHORT
    entry=orl/stop=orh), using the SAME (orh-orl)/orh convention orb_scanner.py already
    uses for MAX_RANGE_FRAC -- not day_open, to stay consistent with what the live scanner
    actually computes."""
    orh = orb_df[["entry", "stop"]].max(axis=1)
    orl = orb_df[["entry", "stop"]].min(axis=1)
    range_pct = (orh - orl) / orh * 100
    orb_df["planned_rr"] = (1.0 / range_pct).clip(upper=ORB_RR_CAP).round(2)
    return orb_df


def load_signals(smart_orb_rr=True):
    mr = pd.read_csv(DATA / "paper_trades.csv")
    orb = pd.read_csv(DATA / "orb_paper_trades.csv")
    if "strategy" not in orb.columns:
        orb["strategy"] = "ORB"
    keep = ["trade_id", "ticker", "side", "strategy", "entry_time", "close_time",
           "shares", "entry", "stop", "planned_rr", "risk_dollars", "outcome_r", "dollar_pnl"]
    for df in (mr, orb):
        for c in keep:
            if c not in df.columns:
                df[c] = None
    if smart_orb_rr:
        orb = apply_smart_orb_rr(orb)
    both = pd.concat([mr[keep], orb[keep]], ignore_index=True)
    both = both.dropna(subset=["entry_time", "close_time", "outcome_r"])
    both["notional"] = both["shares"].astype(float) * both["entry"].astype(float)
    both["entry_dt"] = pd.to_datetime(both["entry_time"])
    close_dt = pd.to_datetime(both["close_time"], utc=True).dt.tz_convert(ET).dt.tz_localize(None)
    both["close_dt"] = close_dt
    both = both.sort_values("entry_dt").reset_index(drop=True)
    return both


def simulate(signals, max_concurrent=pg.MAX_CONCURRENT, max_per_side=pg.MAX_PER_SIDE,
            total_capital=pg.TOTAL_CAPITAL):
    open_positions = []   # each: {side, notional, close_dt, trade_id}
    admitted_ids, withheld = [], []

    for entry_time, batch in signals.groupby("entry_dt", sort=True):
        open_positions = [p for p in open_positions if p["close_dt"] > entry_time]
        cands = [{"trade_id": r.trade_id, "ticker": r.ticker, "side": str(r.side).upper(),
                 "strategy": r.strategy, "planned_rr": r.planned_rr,
                 "risk_dollars": r.risk_dollars, "notional": r.notional}
                for r in batch.itertuples()]
        gate_positions = [{"side": p["side"], "notional": p["notional"]} for p in open_positions]
        approved, rejected = pg.select_portfolio(cands, open_positions=gate_positions,
                                                 max_concurrent=max_concurrent, max_per_side=max_per_side,
                                                 total_capital=total_capital, write_log=False)

        approved_ids = {c["trade_id"] for c in approved}
        for r in batch.itertuples():
            if r.trade_id in approved_ids:
                admitted_ids.append(r.trade_id)
                open_positions.append({"side": str(r.side).upper(), "notional": r.notional,
                                       "close_dt": r.close_dt, "trade_id": r.trade_id})
        for c in rejected:
            withheld.append({**c, "close_dt": None})

    return set(admitted_ids), withheld


def summarize(df, label):
    if df.empty:
        print(f"  {label:22s} 0 trades")
        return
    n = len(df)
    win = (df["outcome_r"] > 0).mean() * 100
    total_r = df["outcome_r"].sum()
    total_d = df["dollar_pnl"].sum()
    wins = df.loc[df["outcome_r"] > 0, "outcome_r"].sum()
    losses = -df.loc[df["outcome_r"] < 0, "outcome_r"].sum()
    pf = wins / losses if losses > 0 else float("inf")
    print(f"  {label:22s} n={n:4d}  win={win:5.1f}%  total={total_r:+8.2f}R (${total_d:+9,.2f})  PF={pf:5.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None, help="filter to mr_z1.5 / ORB / etc")
    ap.add_argument("--max-concurrent", type=int, default=pg.MAX_CONCURRENT)
    ap.add_argument("--max-per-side", type=int, default=pg.MAX_PER_SIDE)
    ap.add_argument("--total-capital", type=float, default=pg.TOTAL_CAPITAL)
    ap.add_argument("--no-smart-orb-rr", action="store_true",
                    help="use the OLD flat NEUTRAL_RR=2.0 for every ORB signal (pre-2026-07-01 behavior)")
    a = ap.parse_args()

    signals = load_signals(smart_orb_rr=not a.no_smart_orb_rr)
    if a.strategy:
        signals = signals[signals["strategy"] == a.strategy]
    print(f"{len(signals)} closed signals, {signals['entry_dt'].min()} .. {signals['entry_dt'].max()}")
    print(f"gate: max_concurrent={a.max_concurrent} max_per_side={a.max_per_side} "
         f"total_capital=${a.total_capital:.0f}  smart_orb_rr={not a.no_smart_orb_rr}")
    print()

    admitted_ids, withheld = simulate(signals, max_concurrent=a.max_concurrent,
                                      max_per_side=a.max_per_side, total_capital=a.total_capital)
    admitted = signals[signals["trade_id"].isin(admitted_ids)]
    rejected_ids = set(signals["trade_id"]) - admitted_ids
    withheld_df = signals[signals["trade_id"].isin(rejected_ids)]

    print("=== RAW (every signal, uncapped -- what paper_trades.csv reports) ===")
    summarize(signals, "ALL SIGNALS")
    print()
    print(f"=== REAL BOOK (3 slots max, 2/side max, $800 cap -- what you'd actually realize) ===")
    summarize(admitted, "ADMITTED")
    summarize(withheld_df, "WITHHELD")
    print(f"\n  admitted {len(admitted)}/{len(signals)} signals ({len(admitted)/len(signals)*100:.1f}%)")
    print()

    print("by strategy:")
    for strat in sorted(signals["strategy"].unique()):
        print(f" [{strat}]")
        summarize(signals[signals["strategy"] == strat], "  all")
        summarize(admitted[admitted["strategy"] == strat], "  admitted")
        summarize(withheld_df[withheld_df["strategy"] == strat], "  withheld")

    print("\nwithheld reasons:")
    reason_counts = {}
    for w in withheld:
        reason = w["gate_reason"].split(":")[0].split("(")[0].strip()
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, n in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {reason:30s} {n}")


if __name__ == "__main__":
    main()
