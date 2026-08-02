import numpy as np
from feed_flow_through_classifier import load_contracts, TICKERS
from gex_quant_engine import ChainState, flag_ghost_wall, bid_side_volume_fraction


def compute_ghost_wall_hits(tickers, verbose=True):
    """Runs the real Ghost Wall check (with the engine's established-OI
    floor) across `tickers`. Returns (raw_hit_count, meaningful_hits) where
    meaningful_hits is a list of (ticker, strike, call/put, v_oi, bid_frac,
    prior_oi) tuples. Factored out so other scripts (darkpool_fingerprint.py)
    can pull real, current hits instead of a hardcoded snapshot.
    """
    meaningful_hits = []
    raw_hits = 0
    for t in tickers:
        strikes, is_call, prior_oi, ask_vol, bid_vol = load_contracts(t)
        n = strikes.shape[0]
        if n == 0:
            continue
        contract_idx = np.concatenate([np.arange(n), np.arange(n)])
        volume = np.concatenate([ask_vol, bid_vol])
        direction = np.concatenate([np.full(n, 1.0), np.full(n, -1.0)])
        chain = ChainState(prior_oi=prior_oi, lam=1.5)
        chain.apply_ticks(contract_idx, volume, direction)
        v_oi = chain.v_oi_ratio
        bid_frac = bid_side_volume_fraction(contract_idx, direction, volume, n)
        # audit fix 2026-07-04: the >=100 established-OI split used to be a
        # post-hoc filter bolted onto this script; it now lives in the engine
        # itself (flag_ghost_wall's established_oi/min_established_oi params).
        # Both raw and floored calls are kept here to show the before/after.
        ghost_raw = flag_ghost_wall(v_oi, bid_frac)
        ghost_floored = flag_ghost_wall(v_oi, bid_frac, established_oi=prior_oi, min_established_oi=100.0)
        eoi = chain.effective_oi
        valid = prior_oi > 0
        if verbose and valid.any():
            print(f"{t:6} mean EOI/priorOI (established strikes only): {np.mean(eoi[valid] / prior_oi[valid]):.3f}")
        raw_hits += int(np.sum(ghost_raw))
        for i in np.where(ghost_floored)[0]:
            meaningful_hits.append((t, strikes[i], "call" if is_call[i] else "put",
                                     float(v_oi[i]), float(bid_frac[i]), float(prior_oi[i])))
    return raw_hits, meaningful_hits


if __name__ == "__main__":
    raw_hits, meaningful_hits = compute_ghost_wall_hits(TICKERS)
    print()
    print(f"raw hits (no OI floor): {raw_hits}")
    print(f"hits surviving the engine's established-OI floor (>=100): {len(meaningful_hits)}")
    print(f"suppressed as noise by the floor: {raw_hits - len(meaningful_hits)}")
    for h in sorted(meaningful_hits, key=lambda x: -x[3]):
        print(" ", h)
