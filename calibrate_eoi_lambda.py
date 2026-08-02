"""calibrate_eoi_lambda.py -- Part 1, item 5: calibrate ChainState's
exponential decay constant lambda (default 1.5) against real day-over-day
OI change data (UW's oi-change endpoint, ordered by |OI change|, paginated
here via limit=500 for broader-than-"only the biggest movers" coverage).

HONEST CAVEAT, read before trusting the number below: oi-change gives NET
day-over-day OI change (today's settled OI vs yesterday's), which conflates
two opposite-sign effects -- existing positions closing (decay, what
ChainState's lambda models) and fresh positions opening (growth, which the
model doesn't represent at all). There's no way to cleanly separate the two
from this data alone. So this calibration is restricted to contracts where
OI net DECREASED session-over-session (closing activity likely dominated)
and reports how large a fraction of contracts that even is -- if most
high-turnover contracts actually show OI *increases*, that's a real finding
about how well the pure-decay framing matches reality, independent of
whatever lambda value comes out of the subset where it does apply.

For the decrease-only subset: model says survival_fraction =
exp(-lambda * turnover) where turnover = volume/prior_oi. Solving for
lambda per contract: lambda_implied = -ln(curr_oi/last_oi) / turnover.
"""
import math
import time

import unusualwhales_client as uw
from uw_historical_pull import PRIORITY_TICKERS

# keep this to the already-familiar 14 (sector ETFs + SPY/QQQ/IWM) rather
# than the full 194 -- this is a calibration sanity check, not a universe
# sweep, and 500 contracts/ticker * 14 is already a meaningfully large
# sample (up to 7,000 contracts).
TICKERS = PRIORITY_TICKERS
LIMIT = 500


def main():
    all_turnover = []
    all_lambda = []
    n_total = n_usable = n_decrease = n_increase = n_flat = 0

    for t in TICKERS:
        try:
            code, body = uw._get(f"/api/stock/{t}/oi-change", limit=LIMIT)
        except Exception as e:
            print(f"  {t}: ERROR {e}")
            continue
        rows = body["data"] if isinstance(body, dict) else body
        for r in rows:
            n_total += 1
            last_oi = r.get("last_oi")
            curr_oi = r.get("curr_oi")
            volume = r.get("volume")
            if not last_oi or last_oi <= 0 or volume is None or curr_oi is None:
                continue
            if curr_oi > last_oi:
                n_increase += 1
                continue
            if curr_oi == last_oi:
                n_flat += 1
                continue
            n_decrease += 1
            turnover = volume / last_oi
            survival = curr_oi / last_oi
            if turnover <= 0 or survival <= 0:
                continue
            lam_implied = -math.log(survival) / turnover
            n_usable += 1
            all_turnover.append(turnover)
            all_lambda.append(lam_implied)
        time.sleep(0.25)

    print(f"contracts seen: {n_total}  (decrease={n_decrease} increase={n_increase} flat={n_flat})")
    print(f"decrease-subset usable for lambda fit: {n_usable}")
    print(f"  -> {n_increase/n_total:.1%} of contracts show NET OI INCREASE despite same-day volume "
          f"-- the pure-decay model doesn't apply to these at all")
    if not all_lambda:
        print("no usable contracts, nothing to calibrate")
        return

    all_lambda.sort()
    n = len(all_lambda)
    median = all_lambda[n // 2]
    mean = sum(all_lambda) / n
    p25 = all_lambda[int(n * 0.25)]
    p75 = all_lambda[int(n * 0.75)]
    print()
    print(f"implied lambda across {n} decrease-only contracts:")
    print(f"  mean={mean:.3f}  median={median:.3f}  IQR=[{p25:.3f}, {p75:.3f}]")
    print(f"  current ChainState default: lambda=1.5")


if __name__ == "__main__":
    main()
