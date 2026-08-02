"""iv_rank_vrp.py -- Part 2 confluence, item 3: IV Rank/Percentile and
Variance Risk Premium (VRP) by ticker across the 194-ticker universe.

Two independent vol-positioning reads, both already pulled to disk (no new
API calls):
- iv_rank_1y (data/unusualwhales/iv_rank/{ticker}.json): where CURRENT
  implied vol sits within its own trailing-1yr range (0-100). Low = vol is
  cheap relative to its own history; high = vol is rich.
- VRP (data/unusualwhales/variance_risk_premium/{ticker}.json): the
  IV-over-realized-vol premium and ITS OWN percentile rank (0-1 here,
  reported as 0-100 for consistency) -- a different question from IV rank:
  not "is IV high vs its own past" but "is IV overpriced vs what actually
  realized," the classic premium-selling edge question.
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


def latest_row(rows):
    return sorted(rows, key=lambda r: r["date"])[-1] if rows else None


def per_ticker(ticker):
    iv_path = UW / "iv_rank" / f"{ticker}.json"
    vrp_path = UW / "variance_risk_premium" / f"{ticker}.json"
    if not iv_path.exists() or not vrp_path.exists():
        return None

    iv_rows = json.loads(iv_path.read_text())
    iv_rows = iv_rows["data"] if isinstance(iv_rows, dict) else iv_rows
    iv_latest = latest_row(iv_rows)

    vrp_rows = json.loads(vrp_path.read_text())
    vrp_rows = vrp_rows if isinstance(vrp_rows, list) else vrp_rows.get("data", [])
    vrp_latest = latest_row(vrp_rows)

    if iv_latest is None or vrp_latest is None:
        return None

    return {
        "iv_rank": float(iv_latest["iv_rank_1y"]),
        "iv": float(iv_latest["volatility"]),
        "iv_date": iv_latest["date"],
        "vrp_rank": float(vrp_latest["rank"]) * 100.0,
        "vrp": float(vrp_latest["risk_premium"]),
        "vrp_date": vrp_latest["date"],
    }


def main():
    results = {}
    for t in TICKERS:
        r = per_ticker(t)
        if r is not None:
            results[t] = r

    print(f"{len(results)}/{len(TICKERS)} tickers have both iv_rank and VRP data")
    print()

    by_iv_rank = sorted(results.items(), key=lambda kv: kv[1]["iv_rank"])
    print("=== 10 lowest IV rank (vol cheap vs own 1yr history -- premium-buying candidates) ===")
    for t, r in by_iv_rank[:10]:
        print(f"  {t:6} iv_rank={r['iv_rank']:5.1f}  iv={r['iv']:.1%}  vrp_rank={r['vrp_rank']:5.1f}  vrp={r['vrp']:+.3f}")
    print("=== 10 highest IV rank (vol rich vs own 1yr history -- premium-selling candidates) ===")
    for t, r in by_iv_rank[-10:][::-1]:
        print(f"  {t:6} iv_rank={r['iv_rank']:5.1f}  iv={r['iv']:.1%}  vrp_rank={r['vrp_rank']:5.1f}  vrp={r['vrp']:+.3f}")

    print()
    by_vrp = sorted(results.items(), key=lambda kv: kv[1]["vrp"])
    print("=== 10 most negative VRP (realized vol running ABOVE implied -- IV underpricing risk) ===")
    for t, r in by_vrp[:10]:
        print(f"  {t:6} vrp={r['vrp']:+.3f}  vrp_rank={r['vrp_rank']:5.1f}  iv_rank={r['iv_rank']:5.1f}")
    print("=== 10 most positive VRP (implied running richest vs realized -- premium-selling edge) ===")
    for t, r in by_vrp[-10:][::-1]:
        print(f"  {t:6} vrp={r['vrp']:+.3f}  vrp_rank={r['vrp_rank']:5.1f}  iv_rank={r['iv_rank']:5.1f}")

    # cross-check: do the two independent vol reads agree on direction?
    agree = sum(1 for r in results.values() if (r["iv_rank"] > 50) == (r["vrp_rank"] > 50))
    print()
    print(f"=== IV rank vs VRP rank agree on above/below median for {agree}/{len(results)} tickers ===")


if __name__ == "__main__":
    main()
