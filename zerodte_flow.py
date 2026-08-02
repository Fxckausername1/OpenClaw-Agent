"""zerodte_flow.py -- Part 2 confluence, item 6: live per-minute SPY/QQQ
0DTE options flow. Flagged as the highest practical-value remaining item
since heff trades SPY 0DTEs by hand.

Two data sources, two honest caveats:
1. flow_per_expiry gives a clean per-expiry breakdown for the day -- the row
   where expiry == date IS that day's 0DTE flow, unambiguously isolated (no
   guessing). Used here for the aggregate 0DTE-day read: what share of the
   day's total options volume was 0DTE, and which side (bid/ask) dominated.
2. flow_per_strike_intraday gives real per-minute granularity but has NO
   expiry filter in the API -- it aggregates ALL active expiries at each
   strike, not just 0DTE. For SPY/QQQ this is a reasonable practical proxy
   given how 0DTE-dominated their daily volume typically is (the share
   computed in #1 validates just how dominant), but it is NOT a strict
   0DTE isolation -- flagged explicitly here, not glossed over.
"""
import unusualwhales_client as uw
from collections import defaultdict

TICKERS = ["SPY", "QQQ"]
BUCKET_MINUTES = 15


def zero_dte_day_summary(ticker):
    code, body = uw.flow_per_expiry(ticker)
    rows = body if isinstance(body, list) else body.get("data", [])
    if not rows:
        return None
    total_call_vol = sum(r["call_volume"] for r in rows)
    total_put_vol = sum(r["put_volume"] for r in rows)
    zero_dte = next((r for r in rows if r["expiry"] == r["date"]), None)
    return rows, zero_dte, total_call_vol, total_put_vol


def intraday_buckets(ticker):
    code, body = uw.flow_per_strike_intraday(ticker)
    rows = body if isinstance(body, list) else body.get("data", [])
    rows = sorted(rows, key=lambda r: r["timestamp"])
    buckets = defaultdict(lambda: {"call_ask": 0, "call_bid": 0, "put_ask": 0, "put_bid": 0, "net_premium": 0.0})
    for r in rows:
        ts = r["timestamp"]  # e.g. "2026-07-02T13:30:49.109000Z"
        hh, mm = int(ts[11:13]), int(ts[14:16])
        bucket_mm = (mm // BUCKET_MINUTES) * BUCKET_MINUTES
        key = f"{hh:02}:{bucket_mm:02}"
        b = buckets[key]
        b["call_ask"] += r["call_volume_ask_side"]
        b["call_bid"] += r["call_volume_bid_side"]
        b["put_ask"] += r["put_volume_ask_side"]
        b["put_bid"] += r["put_volume_bid_side"]
        b["net_premium"] += float(r["net_premium"])
    return buckets


def main():
    for t in TICKERS:
        print(f"=== {t} ===")
        result = zero_dte_day_summary(t)
        if result is None:
            print("  no flow-per-expiry data returned")
            continue
        rows, zero_dte, total_call_vol, total_put_vol = result

        if zero_dte is None:
            print("  no same-day (0DTE) expiry found today")
        else:
            zdte_vol = zero_dte["call_volume"] + zero_dte["put_volume"]
            total_vol = total_call_vol + total_put_vol
            share = zdte_vol / total_vol if total_vol else 0.0
            call_ask, call_bid = zero_dte["call_volume_ask_side"], zero_dte["call_volume_bid_side"]
            put_ask, put_bid = zero_dte["put_volume_ask_side"], zero_dte["put_volume_bid_side"]
            print(f"  0DTE ({zero_dte['expiry']}) share of today's total options volume: {share:.1%} "
                  f"({zdte_vol:,} / {total_vol:,})")
            print(f"  0DTE call flow: ask-side(buy)={call_ask:,} bid-side(sell)={call_bid:,} "
                  f"-> {'net buying' if call_ask > call_bid else 'net selling'}")
            print(f"  0DTE put flow:  ask-side(buy)={put_ask:,} bid-side(sell)={put_bid:,} "
                  f"-> {'net buying' if put_ask > put_bid else 'net selling'}")

        buckets = intraday_buckets(t)
        print(f"  intraday per-{BUCKET_MINUTES}min buckets, UTC (ALL expiries combined -- "
              f"0DTE-heavy proxy per the caveat above, not a strict isolation):")
        for key in sorted(buckets):
            b = buckets[key]
            call_skew = b["call_ask"] - b["call_bid"]
            put_skew = b["put_ask"] - b["put_bid"]
            print(f"    {key}  call_skew(ask-bid)={call_skew:>+7,}  put_skew(ask-bid)={put_skew:>+7,}  "
                  f"net_premium=${b['net_premium']:>+14,.0f}")
        print()


if __name__ == "__main__":
    main()
