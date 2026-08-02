"""short_interest_screen.py -- Part 2 confluence, item 7: short interest /
short volume across the 194-ticker universe (data already pulled, no new
API calls).

Two different cadences, kept separate:
- short_interest_float: FINRA-reported standing short interest, settled
  bi-weekly (si_float = % of float short, days_to_cover) -- a slow-moving
  structural read.
- short_volume_and_ratio: daily short_volume_ratio -- what fraction of
  TODAY's volume was short-sold, a fast-moving activity read. High standing
  SI + high daily short-volume ratio together is a meaningfully different
  signal than either alone (persistent structural short interest still being
  actively pressed today, vs. one-off daily short-selling with no real
  standing position behind it).
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


def latest(rows, date_key="market_date"):
    return sorted(rows, key=lambda r: r[date_key])[-1] if rows else None


def per_ticker(ticker):
    si_path = UW / "short_interest_float" / f"{ticker}.json"
    vol_path = UW / "short_volume_and_ratio" / f"{ticker}.json"
    if not si_path.exists() or not vol_path.exists():
        return None

    si_rows = json.loads(si_path.read_text())["data"]
    si_latest = latest(si_rows)

    vol_body = json.loads(vol_path.read_text())
    vol_rows = vol_body.get("si", vol_body if isinstance(vol_body, list) else [])
    vol_latest = latest(vol_rows)

    if si_latest is None or vol_latest is None:
        return None

    return {
        "si_float": float(si_latest["si_float"]) * 100.0,
        "days_to_cover": float(si_latest["days_to_cover"]),
        "si_date": si_latest["market_date"],
        "short_vol_ratio": float(vol_latest["short_volume_ratio"]) * 100.0,
        "vol_date": vol_latest["market_date"],
    }


def main():
    results = {}
    for t in TICKERS:
        r = per_ticker(t)
        if r is not None:
            results[t] = r
    print(f"{len(results)}/{len(TICKERS)} tickers have both short-interest datasets")
    print()

    by_si = sorted(results.items(), key=lambda kv: kv[1]["si_float"])
    print("=== 10 highest standing short interest (% of float) -- squeeze-risk candidates ===")
    for t, r in by_si[-10:][::-1]:
        print(f"  {t:6} si_float={r['si_float']:5.1f}%  days_to_cover={r['days_to_cover']:5.1f}  "
              f"today's short_vol_ratio={r['short_vol_ratio']:5.1f}%  (SI as of {r['si_date']})")

    by_dtc = sorted(results.items(), key=lambda kv: kv[1]["days_to_cover"])
    print()
    print("=== 10 highest days-to-cover -- hardest to unwind if it squeezes ===")
    for t, r in by_dtc[-10:][::-1]:
        print(f"  {t:6} days_to_cover={r['days_to_cover']:5.1f}  si_float={r['si_float']:5.1f}%")

    print()
    print("=== both high standing SI (>10% of float) AND high today's short-vol-ratio (>50%) ===")
    both = [(t, r) for t, r in results.items() if r["si_float"] > 10.0 and r["short_vol_ratio"] > 50.0]
    for t, r in sorted(both, key=lambda kv: -kv[1]["si_float"]):
        print(f"  {t:6} si_float={r['si_float']:5.1f}%  short_vol_ratio={r['short_vol_ratio']:5.1f}%  "
              f"days_to_cover={r['days_to_cover']:5.1f}")
    if not both:
        print("  (none)")


if __name__ == "__main__":
    main()
