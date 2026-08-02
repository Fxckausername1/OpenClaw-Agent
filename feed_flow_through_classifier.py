"""feed_flow_through_classifier.py -- the first real-data test of Phase 1's
TradeFlowClassifier / ChainState and Phase 3's flag_ghost_wall, using
Unusual Whales' ask/bid-side-tagged flow_per_strike + oi_per_strike data.

For each contract (strike + call/put), UW's ask_side volume is treated as
Lee-Ready "Buy" (direction=+1, trade near the ask) and bid_side volume as
"Sell" (direction=-1, trade near the bid) -- fed as two synthetic aggregate
ticks per contract into the EXACT existing apply_ticks/dealer_weight_from_flow/
bid_side_volume_fraction functions, unmodified. No new logic; this only
proves out code that's been built and tested since Phase 1 but never fed
real data on the live box (which still uses the static +1/-1 fallback).
"""
import json
from pathlib import Path

import numpy as np

from gex_quant_engine import ChainState, TradeFlowClassifier, bid_side_volume_fraction, flag_ghost_wall

ROOT = Path(__file__).resolve().parent
UW = ROOT / "data" / "unusualwhales"

_SECTOR_TICKERS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLY",
                   "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]


def _load_wide_universe_tickers():
    # 2026-07-04: extend past the sector-ETF sample to heff's actual 180-stock
    # trading universe (same list mean_reversion/orb/premarket/continuation
    # scanners use). Loaded dynamically so this always tracks
    # wide_universe.json rather than a copy-pasted snapshot of it.
    path = ROOT / "data" / "wide_universe.json"
    return json.loads(path.read_text())["symbols"]


TICKERS = _SECTOR_TICKERS + _load_wide_universe_tickers()


def load_contracts(ticker: str):
    flow_path = UW / "flow_per_strike" / f"{ticker}.json"
    oi_path = UW / "oi_per_strike" / f"{ticker}.json"
    # 2026-07-04: wide_universe.json is dynamic (rebuilds, plus the new
    # supplementary-movers union) and can add a ticker after the last UW
    # historical pull -- treat a missing file as "no data yet" (empty
    # arrays, same as the existing n==0 handling downstream), not a crash.
    if not flow_path.exists() or not oi_path.exists():
        empty = np.array([])
        return empty, empty, empty, empty, empty
    flow = json.load(open(flow_path))
    oi = json.load(open(oi_path))["data"]
    oi_by_strike = {row["strike"]: row for row in oi}

    strikes, is_call, prior_oi, ask_vol, bid_vol = [], [], [], [], []
    for row in flow:
        k = row["strike"]
        oi_row = oi_by_strike.get(k)
        if oi_row is None:
            continue
        # call side
        strikes.append(float(k)); is_call.append(True)
        prior_oi.append(float(oi_row["call_oi"]))
        ask_vol.append(float(row["call_volume_ask_side"]))
        bid_vol.append(float(row["call_volume_bid_side"]))
        # put side
        strikes.append(float(k)); is_call.append(False)
        prior_oi.append(float(oi_row["put_oi"]))
        ask_vol.append(float(row["put_volume_ask_side"]))
        bid_vol.append(float(row["put_volume_bid_side"]))

    return (np.array(strikes), np.array(is_call), np.array(prior_oi, dtype=np.float64),
            np.array(ask_vol, dtype=np.float64), np.array(bid_vol, dtype=np.float64))


def main():
    all_ghost = []
    all_w_diff = []

    for t in TICKERS:
        strikes, is_call, prior_oi, ask_vol, bid_vol = load_contracts(t)
        n = strikes.shape[0]
        if n == 0:
            print(f"{t}: no matched contracts, skipping")
            continue

        # two synthetic aggregate ticks per contract: ask-side = Buy (+1), bid-side = Sell (-1)
        contract_idx = np.concatenate([np.arange(n), np.arange(n)])
        volume = np.concatenate([ask_vol, bid_vol])
        direction = np.concatenate([np.full(n, 1.0), np.full(n, -1.0)])

        chain = ChainState(prior_oi=prior_oi, lam=1.5)
        chain.apply_ticks(contract_idx, volume, direction)

        v_oi = chain.v_oi_ratio
        bid_frac = bid_side_volume_fraction(contract_idx, direction, volume, n)
        ghost = flag_ghost_wall(v_oi, bid_frac, established_oi=prior_oi, min_established_oi=100.0)

        w_dynamic = TradeFlowClassifier.dealer_weight_from_flow(contract_idx, direction, volume, is_call, n)
        w_static = np.where(is_call, 1.0, -1.0)
        w_diff = np.abs(w_dynamic - w_static)

        n_ghost = int(np.sum(ghost))
        mean_w_diff = float(np.mean(w_diff))
        max_w_diff = float(np.max(w_diff))
        eoi = chain.effective_oi
        mean_eoi_vs_oi = float(np.mean(eoi / np.where(prior_oi > 0, prior_oi, np.nan)))

        print(f"{t:6} contracts={n:4} ghost_wall_hits={n_ghost:3} "
              f"mean|w_dyn-w_static|={mean_w_diff:.3f} max={max_w_diff:.3f} "
              f"mean(EOI/priorOI)={mean_eoi_vs_oi:.2f}")

        if n_ghost > 0:
            idx = np.where(ghost)[0]
            for i in idx:
                all_ghost.append((t, strikes[i], "call" if is_call[i] else "put",
                                  float(v_oi[i]), float(bid_frac[i]), float(prior_oi[i])))
        all_w_diff.append(mean_w_diff)

    print()
    print(f"=== Ghost Wall hits across all {len(TICKERS)} tickers: {len(all_ghost)} ===")
    for t, k, typ, voi, bf, oi in all_ghost:
        print(f"  {t} {k} {typ}: V/OI={voi:.2f} bid_frac={bf:.2f} prior_oi={oi:.0f}")

    print()
    print(f"Average dynamic-vs-static weight divergence across universe: {np.mean(all_w_diff):.3f}")


if __name__ == "__main__":
    main()
