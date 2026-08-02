"""sweep_alert_summary.py -- Part 2 confluence, item 1: unusual/sweep
options activity, from real UW flow-alerts data (is_sweep=true, up to 20
most recent per ticker, already pulled to data/unusualwhales/sweep_alerts/).

A "sweep" here is UW's own rule-based tag (RepeatedHits family, etc.) for
aggressive, fragmented, multi-exchange fills -- not something derived here.
This just aggregates what UW already flagged: sweep count and $ premium per
ticker, split call vs put, to surface where today's unusual activity
actually concentrated.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UW = ROOT / "data" / "unusualwhales" / "sweep_alerts"


def load_wide_universe_tickers():
    data = json.loads((ROOT / "data" / "wide_universe.json").read_text())
    return data["symbols"]


SECTOR_TICKERS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLY",
                  "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]
TICKERS = SECTOR_TICKERS + load_wide_universe_tickers()


def per_ticker(ticker):
    path = UW / f"{ticker}.json"
    if not path.exists():
        return None
    body = json.loads(path.read_text())
    rows = body.get("data", [])
    if not rows:
        return None

    call_prem = sum(float(r["total_premium"]) for r in rows if r["type"] == "call")
    put_prem = sum(float(r["total_premium"]) for r in rows if r["type"] == "put")
    n_call = sum(1 for r in rows if r["type"] == "call")
    n_put = sum(1 for r in rows if r["type"] == "put")
    biggest = max(rows, key=lambda r: float(r["total_premium"]))

    return {
        "n_alerts": len(rows), "n_call": n_call, "n_put": n_put,
        "call_prem": call_prem, "put_prem": put_prem,
        "net_prem": call_prem - put_prem,
        "biggest": biggest,
    }


def main():
    results = {}
    for t in TICKERS:
        r = per_ticker(t)
        if r is not None:
            results[t] = r

    total_tickers_with_sweeps = len(results)
    print(f"{total_tickers_with_sweeps}/{len(TICKERS)} tickers had at least one sweep alert today")
    print()

    by_total_prem = sorted(results.items(), key=lambda kv: -(kv[1]["call_prem"] + kv[1]["put_prem"]))
    print("=== 15 busiest tickers by total sweep premium ===")
    for t, r in by_total_prem[:15]:
        total = r["call_prem"] + r["put_prem"]
        lean = "BULLISH" if r["net_prem"] > 0 else "BEARISH"
        b = r["biggest"]
        print(f"  {t:6} n_sweeps={r['n_alerts']:3} (calls={r['n_call']} puts={r['n_put']})  "
              f"total_prem=${total:>12,.0f}  net_prem=${r['net_prem']:>+12,.0f} -> {lean:8}  "
              f"biggest: {b['type']} ${b['strike']} exp={b['expiry']} prem=${float(b['total_premium']):,.0f}")

    print()
    print(f"overall: {sum(1 for r in results.values() if r['net_prem'] > 0)} tickers net call-sweep-heavy, "
          f"{sum(1 for r in results.values() if r['net_prem'] < 0)} net put-sweep-heavy")


if __name__ == "__main__":
    main()
