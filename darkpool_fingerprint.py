"""darkpool_fingerprint.py -- Part 2 confluence, item 1 (dark pool prints).

Institutional-vs-speculative fingerprint from Unusual Whales' per-print dark
pool tape (data/unusualwhales/darkpool/{ticker}.json). Cross-checked against
real, freshly-computed Ghost Wall hits (via ghost_wall_followup's
compute_ghost_wall_hits -- not a hardcoded snapshot, so this always reflects
whatever TICKERS currently covers, sector ETFs or the full 180-stock
wide_universe, both pulled from the same session).

Methodology (deliberately simple/explainable, same philosophy as
flag_ghost_wall's plain V/OI + bid-fraction rule -- no ML, no hidden
weights):

- notional: UW's own `premium` field (size * price, already computed
  upstream).
- block: size >= BLOCK_SIZE_FLOOR (10,000 sh) OR notional >=
  BLOCK_NOTIONAL_FLOOR ($200k) -- standard block-trade thresholds used to
  separate institutional-scale prints from retail/odd-lot-scale ones.
- aggressor: classified against the print's OWN nbbo_bid/nbbo_ask snapshot
  -- at/above ask = BUY, at/below bid = SELL, else MIDPOINT (a negotiated
  cross, the typical execution style for large blocks -- deliberately not
  forced into a buy/sell bucket).

Per ticker this prints: block-trade participation (share of volume/notional
from block-sized prints), the directional lean of blocks only (their
buy/sell/midpoint split), and -- where that ticker also has a live Ghost
Wall hit -- a side-by-side check of whether the day's institutional
dark-pool lean agrees or disagrees with the engine's own stated Ghost Wall
direction (put wall Ghost Wall = bearish/trapdoor lean, call wall Ghost
Wall = bullish/squeeze lean, per evaluate_momentum_short_trapdoor /
evaluate_momentum_long_squeeze in gex_quant_engine.py). Ends with an
aggregate agree/disagree/inconclusive tally across the whole universe.
"""
import json
from pathlib import Path
from collections import defaultdict

from feed_flow_through_classifier import TICKERS
from ghost_wall_followup import compute_ghost_wall_hits

ROOT = Path(__file__).resolve().parent
DP = ROOT / "data" / "unusualwhales" / "darkpool"

BLOCK_SIZE_FLOOR = 10_000
BLOCK_NOTIONAL_FLOOR = 200_000.0


def load_ghost_wall_hits(tickers):
    """Real, current Ghost Wall hits (not a hardcoded snapshot), grouped by
    ticker since a name can carry both a put-wall and a call-wall hit at
    once. Returns {ticker: [(wall_type, expected_direction, strike), ...]}.
    """
    _, meaningful_hits = compute_ghost_wall_hits(tickers, verbose=False)
    by_ticker = defaultdict(list)
    for t, strike, wall_type, v_oi, bid_frac, prior_oi in meaningful_hits:
        expected = "BEARISH" if wall_type == "put" else "BULLISH"
        by_ticker[t].append((wall_type, expected, strike))
    return by_ticker


def classify(rows):
    block_vol = block_notional = 0
    nonblock_vol = nonblock_notional = 0
    block_side = {"BUY": 0, "SELL": 0, "MIDPOINT": 0}
    block_side_notional = {"BUY": 0.0, "SELL": 0.0, "MIDPOINT": 0.0}
    nonblock_side = {"BUY": 0, "SELL": 0, "MIDPOINT": 0}

    for r in rows:
        size = r["size"]
        notional = float(r["premium"])
        bid, ask = float(r["nbbo_bid"]), float(r["nbbo_ask"])
        price = float(r["price"])
        if price >= ask:
            side = "BUY"
        elif price <= bid:
            side = "SELL"
        else:
            side = "MIDPOINT"

        is_block = size >= BLOCK_SIZE_FLOOR or notional >= BLOCK_NOTIONAL_FLOOR
        if is_block:
            block_vol += size
            block_notional += notional
            block_side[side] += 1
            block_side_notional[side] += notional
        else:
            nonblock_vol += size
            nonblock_notional += notional
            nonblock_side[side] += 1

    return {
        "block_vol": block_vol, "block_notional": block_notional,
        "nonblock_vol": nonblock_vol, "nonblock_notional": nonblock_notional,
        "block_side": block_side, "block_side_notional": block_side_notional,
        "nonblock_side": nonblock_side,
    }


def main():
    ghost_wall_hits = load_ghost_wall_hits(TICKERS)
    tally = {"AGREES": 0, "DISAGREES": 0, "INCONCLUSIVE": 0}

    for t in TICKERS:
        path = DP / f"{t}.json"
        if not path.exists():
            continue
        rows = json.load(open(path))["data"]
        n = len(rows)
        if n == 0:
            continue
        c = classify(rows)
        total_vol = c["block_vol"] + c["nonblock_vol"]
        block_share = c["block_vol"] / total_vol if total_vol else 0.0

        bs = c["block_side"]
        n_block = sum(bs.values())
        lean = "NEUTRAL"
        if n_block:
            buy_frac = bs["BUY"] / n_block
            sell_frac = bs["SELL"] / n_block
            if buy_frac > sell_frac * 1.3:
                lean = "BULLISH"
            elif sell_frac > buy_frac * 1.3:
                lean = "BEARISH"

        hits = ghost_wall_hits.get(t, [])

        print(f"{t:6} prints={n:4} block_share_of_volume={block_share:.1%} "
              f"({c['block_vol']:>10,} / {total_vol:>10,} sh)  "
              f"block_aggressor=BUY:{bs['BUY']:3} SELL:{bs['SELL']:3} MID:{bs['MIDPOINT']:3} "
              f"-> institutional lean={lean}")

        for wall_type, expected, strike in hits:
            if lean == expected:
                verdict = "AGREES"
            elif lean == "NEUTRAL":
                verdict = "INCONCLUSIVE"
            else:
                verdict = "DISAGREES"
            tally[verdict] += 1
            print(f"       Ghost Wall hit ({wall_type} wall @ {strike:g}) -> engine expects {expected}. "
                  f"Dark-pool institutional lean {'no clear lean (inconclusive)' if verdict == 'INCONCLUSIVE' else verdict}.")

    print()
    print(f"=== confluence tally across {len(TICKERS)} tickers: "
          f"{tally['AGREES']} agree / {tally['DISAGREES']} disagree / {tally['INCONCLUSIVE']} inconclusive ===")
    print(f"(block thresholds: size>={BLOCK_SIZE_FLOOR:,} sh OR notional>=${BLOCK_NOTIONAL_FLOOR:,.0f}; "
          f"lean requires one side >=1.3x the other among block prints only)")


if __name__ == "__main__":
    main()
