"""sector_rrg_crosscheck.py -- Part 2 confluence, item 4: sector-level
options positioning cross-check vs. sector_rotation.py's RRG.

Two genuinely independent signals for each of the 11 sector ETFs:
- RRG quadrant (price-based relative strength vs SPY, from sector_rotation.py
  -- Leading/Improving = "hot"/inflow-leaning, Weakening/Lagging = "cold").
- Options positioning (dollar-weighted net premium: call $ - put $ from UW's
  flow-per-strike, already pulled; plus any Ghost Wall hit direction from
  ghost_wall_followup.py's real, floor-applied hits).

This is a cross-check, not a filter -- same "informational, not a gate"
posture as the existing sector_hot tag threaded through the live scanners.
"""
from sector_rotation import load_latest_quadrants, SECTOR_ETFS, ETF_SECTOR_NAME
from put_call_ratio import per_ticker_ratios
from ghost_wall_followup import compute_ghost_wall_hits

HOT_QUADRANTS = ("Leading", "Improving")


def main():
    quadrants = load_latest_quadrants()
    _, ghost_hits = compute_ghost_wall_hits(SECTOR_ETFS, verbose=False)
    hits_by_ticker = {}
    for t, strike, wall_type, v_oi, bid_frac, prior_oi in ghost_hits:
        hits_by_ticker.setdefault(t, []).append((wall_type, strike))

    agree = disagree = no_signal = 0
    for etf in SECTOR_ETFS:
        q = quadrants.get(etf)
        r = per_ticker_ratios(etf)
        if q is None or r is None:
            print(f"{etf:6} missing data (RRG={q is not None}, options={r is not None}), skipping")
            continue

        rrg_hot = q["quadrant"] in HOT_QUADRANTS
        rrg_lean = "BULLISH" if rrg_hot else "BEARISH"

        net_premium = r["net_premium"]
        opt_lean = "BULLISH" if net_premium > 0 else "BEARISH"

        if rrg_lean == opt_lean:
            verdict = "AGREES"
            agree += 1
        else:
            verdict = "DISAGREES"
            disagree += 1

        hits = hits_by_ticker.get(etf, [])
        hit_str = ""
        if hits:
            hit_str = "  Ghost Wall: " + ", ".join(
                f"{wt} wall @ {strike:g}" for wt, strike in hits)

        print(f"{etf:6} ({ETF_SECTOR_NAME.get(etf, etf):12}) RRG={q['quadrant']:10} "
              f"(z={q['rs_zscore']:+.2f} mom={q['rs_mom']:+.2f}) -> {rrg_lean:8}  "
              f"options net_premium=${net_premium:>+14,.0f} -> {opt_lean:8}  "
              f"[{verdict}]{hit_str}")

    print()
    print(f"=== {agree} agree / {disagree} disagree across {len(SECTOR_ETFS)} sector ETFs ===")


if __name__ == "__main__":
    main()
