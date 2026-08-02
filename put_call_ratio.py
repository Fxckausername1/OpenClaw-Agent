"""put_call_ratio.py -- Part 2 confluence, item 5: put/call ratio and net
premium by ticker across the full 194-ticker universe, from data already
pulled (data/unusualwhales/{oi_per_strike,flow_per_strike}/{ticker}.json --
no new API calls needed).

Two ratios, deliberately kept separate since they answer different
questions:
- OI-based P/C ratio: standing positioning (what's been built up over time).
- Volume-based P/C ratio: today's flow (what's happening right now) -- plus
  net premium (call $ - put $) as a dollar-weighted directional read, since
  a ratio alone treats a 1-lot and a 10,000-lot the same.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UW = ROOT / "data" / "unusualwhales"


def load_wide_universe_tickers():
    data = json.loads((ROOT / "data" / "wide_universe.json").read_text())
    return data["symbols"]


SECTOR_TICKERS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLY",
                  "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]
TICKERS = SECTOR_TICKERS + load_wide_universe_tickers()


def per_ticker_ratios(ticker):
    oi_path = UW / "oi_per_strike" / f"{ticker}.json"
    flow_path = UW / "flow_per_strike" / f"{ticker}.json"
    if not oi_path.exists() or not flow_path.exists():
        return None

    oi_rows = json.loads(oi_path.read_text())["data"]
    flow_rows = json.loads(flow_path.read_text())

    call_oi = sum(r["call_oi"] for r in oi_rows)
    put_oi = sum(r["put_oi"] for r in oi_rows)
    pc_oi_ratio = put_oi / call_oi if call_oi else float("inf")

    call_vol = sum(r["call_volume"] for r in flow_rows)
    put_vol = sum(r["put_volume"] for r in flow_rows)
    pc_vol_ratio = put_vol / call_vol if call_vol else float("inf")

    call_premium = sum(float(r["call_premium"]) for r in flow_rows)
    put_premium = sum(float(r["put_premium"]) for r in flow_rows)
    net_premium = call_premium - put_premium  # positive = net call-side $ flow

    return {
        "call_oi": call_oi, "put_oi": put_oi, "pc_oi_ratio": pc_oi_ratio,
        "call_vol": call_vol, "put_vol": put_vol, "pc_vol_ratio": pc_vol_ratio,
        "net_premium": net_premium,
    }


def main():
    results = {}
    for t in TICKERS:
        r = per_ticker_ratios(t)
        if r is not None:
            results[t] = r

    print(f"{'ticker':6} {'PC_OI':>7} {'PC_VOL':>7} {'net_premium':>16}  (PC = put/call, >1 = put-heavy)")
    for t, r in sorted(results.items(), key=lambda kv: kv[0]):
        print(f"{t:6} {r['pc_oi_ratio']:7.2f} {r['pc_vol_ratio']:7.2f} {r['net_premium']:>+16,.0f}")

    print()
    by_oi_ratio = sorted(results.items(), key=lambda kv: kv[1]["pc_oi_ratio"])
    print("=== 10 most call-heavy by standing OI (lowest PC_OI) ===")
    for t, r in by_oi_ratio[:10]:
        print(f"  {t:6} PC_OI={r['pc_oi_ratio']:.2f}  call_oi={r['call_oi']:,} put_oi={r['put_oi']:,}")
    print("=== 10 most put-heavy by standing OI (highest PC_OI) ===")
    for t, r in by_oi_ratio[-10:][::-1]:
        print(f"  {t:6} PC_OI={r['pc_oi_ratio']:.2f}  call_oi={r['call_oi']:,} put_oi={r['put_oi']:,}")

    print()
    by_premium = sorted(results.items(), key=lambda kv: kv[1]["net_premium"])
    print("=== 10 most net put-$ (bearish premium flow) today ===")
    for t, r in by_premium[:10]:
        print(f"  {t:6} net_premium=${r['net_premium']:>+14,.0f}  PC_VOL={r['pc_vol_ratio']:.2f}")
    print("=== 10 most net call-$ (bullish premium flow) today ===")
    for t, r in by_premium[-10:][::-1]:
        print(f"  {t:6} net_premium=${r['net_premium']:>+14,.0f}  PC_VOL={r['pc_vol_ratio']:.2f}")


if __name__ == "__main__":
    main()
