"""confluence_score.py -- combines the 7 independent UW-derived signal
sources built this session into one per-ticker confluence read, automating
the XLI/CAT/SNDK/KLAC pattern-spotting that otherwise needed a manual dig
each time (2026-07-04). Reuses every existing script's own functions --
nothing here re-derives logic already built.

Directional signals (each contributes BULLISH/BEARISH/None to the tally):
- Ghost Wall hit direction (put wall = bearish, call wall = bullish)
- Dark-pool institutional lean (block-trade aggressor split, already a
  relative test -- requires clearing a 1.3x buy/sell skew)
- Put/call net premium, RELATIVE to today's cross-sectional distribution
  (top/bottom quintile across the universe, not a raw sign test -- see the
  audit note below for why)
- Sweep-alert net premium, same relative treatment
- Sector RRG quadrant (sector ETFs only -- no RRG signal exists for
  individual stocks, so this is simply absent for those rows; already a
  relative-to-SPY/own-history z-score test)
- IV-stress (added 2026-07-04, heff's explicit ask): IV rank and VRP,
  neither of which has an inherent sign on its own (see AUDIT NOTE 2 below),
  combined into one relative "stress vs. complacency" read -- BEARISH only
  when IV rank is in the day's top quintile AND VRP is in the bottom
  quintile (elevated fear premium AND realized vol running hotter than what
  was priced -- both tails, not just one), BULLISH only on the mirror case
  (low IV rank + rich/positive-tail VRP -- priced-in calm). Silent (no vote)
  whenever the two don't agree, which will be most days for most names --
  this is deliberately a narrow, high-bar signal, not a everyday reading.
- Short interest (added 2026-07-04, heff's explicit ask): ONE-SIDED --
  BEARISH only when SI-of-float is in the day's top quintile (crowded short
  conviction, matching the framing of every other signal here as real
  positioning, not a squeeze-setup read). No vote on low SI; the absence of
  a crowded short isn't evidence of anything bullish on its own.

AUDIT NOTE (2026-07-04): the first version of this script used a plain sign
test (positive/negative) for put/call and sweep net premium. Real data
proved that's wrong: on an ordinary session, ~132 of 194 tickers show net
call-heavy premium (confirmed via sweep_alert_summary.py's own "132 net
call-sweep-heavy, 50 net put-sweep-heavy" run) -- calls are simply the more
common speculative vehicle market-wide, not evidence of anything ticker-
specific. A sign test flagged 130+/194 tickers as "strong confluence,"
almost all bullish, which is a systemic bias reading as signal, not real
cross-signal agreement. Switched to top/bottom-quintile-of-the-day
thresholds so these two signals only fire on genuine outliers relative to
their peers that same session (matching what actually made XLI's PC_VOL=
32.72 or SNDK's -$386M net premium stand out originally -- they were
extreme versus the rest of the universe, not merely negative).

AUDIT NOTE 2 (2026-07-04): IV rank and VRP were originally left OUT of the
tally entirely, on the grounds that neither has an inherent bullish/
bearish sign -- a high IV rank or a rich VRP can precede a rally just as
easily as a crash, unlike Ghost Wall/dark-pool/put-call/sweep/RRG, which
are all real directional positioning. heff explicitly asked to fold them
in anyway; rather than invent a raw-sign reading for VRP (which likely
carries the SAME market-wide baseline bias just proven above for put/call
-- VRP is structurally positive on average across the whole market, since
implied vol systematically runs above subsequently-realized vol, so a raw-
sign test would probably flag most names "bullish" for the same reason the
first put/call version did), both metrics are gated behind the SAME
top/bottom-quintile relative-outlier treatment as put/call and sweep, and
required to agree with each other before voting at all (see iv_stress
above). Similarly, short interest's directional read is genuinely
contested (crowded-short-bearish vs. squeeze-potential-bullish) -- heff
chose the bearish-crowded-short framing to stay consistent with this
script's other signals, all of which read as real smart-money positioning
rather than a contrarian squeeze thesis.

Still context-only (shown per-ticker, NOT counted toward the tally) --
today's short-volume ratio has no established relative-outlier treatment
of its own yet:
- Short-volume ratio

Still diagnostic-only, same posture as everything else built against UW
this session -- not wired into anything live. Confluence agreement is
suggestive, not a validated edge: dark-pool agreement with Ghost Wall was
only 56% (barely better than chance) across 133 real hits earlier today --
treat "N signals agree" as a prompt to look closer (per the CAT/Burry dig),
not proof of anything.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ghost_wall_followup import compute_ghost_wall_hits
from put_call_ratio import per_ticker_ratios, TICKERS
from darkpool_fingerprint import classify as dp_classify, DP as DP_DIR
from sweep_alert_summary import per_ticker as sweep_per_ticker
from iv_rank_vrp import per_ticker as ivvrp_per_ticker
from short_interest_screen import per_ticker as short_per_ticker
from sector_rotation import load_latest_quadrants, SECTOR_ETFS

HOT_QUADRANTS = ("Leading", "Improving")
EXTREME_PCTILE = 20  # bottom/top 20% of the day's cross-sectional distribution


def dark_pool_lean(ticker):
    path = DP_DIR / f"{ticker}.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text())["data"]
    if not rows:
        return None
    c = dp_classify(rows)
    bs = c["block_side"]
    n = sum(bs.values())
    if not n:
        return None
    buy_frac, sell_frac = bs["BUY"] / n, bs["SELL"] / n
    if buy_frac > sell_frac * 1.3:
        return "BULLISH"
    if sell_frac > buy_frac * 1.3:
        return "BEARISH"
    return None


def _relative_lean(value, lo, hi):
    if value >= hi:
        return "BULLISH"
    if value <= lo:
        return "BEARISH"
    return None


def _iv_stress_lean(iv_rank, vrp, ivr_lo, ivr_hi, vrp_lo, vrp_hi):
    """Combined IV-rank/VRP tail read (see the module docstring's AUDIT NOTE 2)
    -- both metrics must independently clear the day's opposite-tail
    threshold before this votes at all; a single metric in its tail with the
    other one unremarkable stays silent rather than guessing."""
    if iv_rank >= ivr_hi and vrp <= vrp_lo:
        return "BEARISH"
    if iv_rank <= ivr_lo and vrp >= vrp_hi:
        return "BULLISH"
    return None


def compute_confluence():
    """Returns a list of per-ticker confluence rows (only tickers with >=1
    directional signal today). Factored out so other scripts (the
    catalyst-alert trigger) can reuse it without re-running main()'s prints.
    """
    _, ghost_hits = compute_ghost_wall_hits(TICKERS, verbose=False)
    ghost_by_ticker = defaultdict(list)
    for t, strike, wall_type, v_oi, bid_frac, prior_oi in ghost_hits:
        ghost_by_ticker[t].append("BEARISH" if wall_type == "put" else "BULLISH")

    quadrants = load_latest_quadrants()

    # single pass to gather per-ticker data + the day's cross-sectional
    # distribution for the signals that need relative thresholds
    pc_data = {t: per_ticker_ratios(t) for t in TICKERS}
    sweep_data = {t: sweep_per_ticker(t) for t in TICKERS}
    ctx_data = {t: ivvrp_per_ticker(t) for t in TICKERS}
    short_data = {t: short_per_ticker(t) for t in TICKERS}
    pc_premiums = np.array([v["net_premium"] for v in pc_data.values() if v])
    sweep_premiums = np.array([v["net_prem"] for v in sweep_data.values() if v])
    iv_ranks = np.array([v["iv_rank"] for v in ctx_data.values() if v and v.get("iv_rank") is not None])
    vrps = np.array([v["vrp"] for v in ctx_data.values() if v and v.get("vrp") is not None])
    si_floats = np.array([v["si_float"] for v in short_data.values() if v and v.get("si_float") is not None])
    pc_lo, pc_hi = np.percentile(pc_premiums, [EXTREME_PCTILE, 100 - EXTREME_PCTILE])
    sweep_lo, sweep_hi = np.percentile(sweep_premiums, [EXTREME_PCTILE, 100 - EXTREME_PCTILE])
    ivr_lo, ivr_hi = np.percentile(iv_ranks, [EXTREME_PCTILE, 100 - EXTREME_PCTILE])
    vrp_lo, vrp_hi = np.percentile(vrps, [EXTREME_PCTILE, 100 - EXTREME_PCTILE])
    si_hi = np.percentile(si_floats, 100 - EXTREME_PCTILE)

    rows_out = []
    for t in TICKERS:
        signals = {}

        gw = ghost_by_ticker.get(t)
        if gw:
            signals["ghost_wall"] = gw[0] if len(set(gw)) == 1 else "MIXED"

        dp = dark_pool_lean(t)
        if dp:
            signals["dark_pool"] = dp

        pcr = pc_data.get(t)
        if pcr:
            lean = _relative_lean(pcr["net_premium"], pc_lo, pc_hi)
            if lean:
                signals["put_call"] = lean

        sw = sweep_data.get(t)
        if sw:
            lean = _relative_lean(sw["net_prem"], sweep_lo, sweep_hi)
            if lean:
                signals["sweep"] = lean

        if t in SECTOR_ETFS:
            q = quadrants.get(t)
            if q:
                signals["sector_rrg"] = "BULLISH" if q["quadrant"] in HOT_QUADRANTS else "BEARISH"

        ctx = ctx_data.get(t) or {}
        short = short_data.get(t) or {}

        if ctx.get("iv_rank") is not None and ctx.get("vrp") is not None:
            lean = _iv_stress_lean(ctx["iv_rank"], ctx["vrp"], ivr_lo, ivr_hi, vrp_lo, vrp_hi)
            if lean:
                signals["iv_stress"] = lean

        if short.get("si_float") is not None and short["si_float"] >= si_hi:
            signals["short_interest"] = "BEARISH"

        directional = [v for v in signals.values() if v in ("BULLISH", "BEARISH")]
        if not directional:
            continue

        rows_out.append({
            "ticker": t, "signals": signals,
            "n_bull": directional.count("BULLISH"), "n_bear": directional.count("BEARISH"),
            "n_total": len(directional),
            "iv_rank": ctx.get("iv_rank"), "vrp": ctx.get("vrp"),
            "si_float": short.get("si_float"), "short_vol_ratio": short.get("short_vol_ratio"),
        })
    return rows_out


def strength(r):
    """3+ directional signals, at most 1 disagreeing -- returns the winning
    side's count, or 0 if it doesn't clear that bar."""
    m = max(r["n_bull"], r["n_bear"])
    return m if r["n_total"] >= 3 and m >= r["n_total"] - 1 else 0


CONFLUENCE_JSON_PATH = Path(__file__).resolve().parent / "data" / "confluence_score.json"


def save_confluence(strong_rows):
    """Writes the dashboard-facing payload -- only the strong-confluence
    subset (the full ~175-ticker list is too noisy for a UI panel, matches
    the console output's own framing). generated_at lets the frontend/
    dashboard_snapshot.py judge staleness honestly once the underlying UW
    pulls stop (see build-tough-data-independence)."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": [
            {
                "ticker": r["ticker"],
                "lean": "BEARISH" if r["n_bear"] > r["n_bull"] else "BULLISH",
                "n_bull": r["n_bull"], "n_bear": r["n_bear"], "n_total": r["n_total"],
                "signals": r["signals"],
                "iv_rank": r["iv_rank"], "vrp": r["vrp"],
                "si_float": r["si_float"], "short_vol_ratio": r["short_vol_ratio"],
            }
            for r in strong_rows
        ],
    }
    CONFLUENCE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFLUENCE_JSON_PATH.write_text(json.dumps(payload, indent=2))
    return CONFLUENCE_JSON_PATH


def main():
    rows_out = compute_confluence()
    strong = sorted([r for r in rows_out if strength(r) > 0], key=lambda r: -strength(r))
    out_path = save_confluence(strong)
    print(f"wrote {len(strong)} strong-confluence rows -> {out_path}")

    print(f"{len(rows_out)}/{len(TICKERS)} tickers had at least one directional signal today "
          f"(put_call/sweep/iv_stress/short_interest all use top/bottom {EXTREME_PCTILE}% of the "
          f"day's cross-section, not raw sign)")
    print()
    print("=== strong confluence (3+ directional signals, at most 1 disagreeing) ===")
    for r in strong:
        lean = "BEARISH" if r["n_bear"] > r["n_bull"] else "BULLISH"
        ctx_bits = []
        if r["iv_rank"] is not None:
            ctx_bits.append(f"iv_rank={r['iv_rank']:.0f}")
        if r["vrp"] is not None:
            ctx_bits.append(f"vrp={r['vrp']:+.2f}")
        if r["si_float"] is not None:
            ctx_bits.append(f"si_float={r['si_float']:.1f}%")
        ctx_str = "  ".join(ctx_bits)
        print(f"  {r['ticker']:6} {lean:8} {r['n_bull']}bull/{r['n_bear']}bear of {r['n_total']}  "
              f"{r['signals']}  {ctx_str}")
    if not strong:
        print("  (none today)")


if __name__ == "__main__":
    main()
