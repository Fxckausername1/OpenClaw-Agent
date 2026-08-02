"""compute_real_adv.py -- Part 1, item 6: replace WSS's notional-proxy
standardizing scalar (advanced_gex.py's `notional = S * sum(oi) * 100`,
explicitly commented there as "NOT real average daily dollar volume") with
a real 30-trading-day average daily dollar volume (ADV) per ticker.

Reuses sector_rotation.py's existing fetch_daily_bars (Alpaca free daily
bars, same credentials/pattern already live for the 11 sector ETFs) rather
than introducing a second equity-data pathway -- same free data source,
just a wider symbol list.
"""
import json
from pathlib import Path

from sector_rotation import fetch_daily_bars
from uw_historical_pull import ALL_TICKERS

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "real_adv.json"
ADV_WINDOW = 30  # trading days


def main():
    symbols = sorted(set(ALL_TICKERS))
    print(f"fetching daily bars for {len(symbols)} symbols via Alpaca...")
    df = fetch_daily_bars(symbols, days=45)  # calendar-day buffer past 30 trading days

    adv = {}
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("t").tail(ADV_WINDOW)
        if g.empty:
            continue
        dollar_vol = (g["Close"] * g["Volume"]).mean()
        if dollar_vol > 0:
            adv[sym] = float(dollar_vol)

    OUT.write_text(json.dumps(adv, indent=2))
    print(f"{len(adv)}/{len(symbols)} symbols -> {OUT}")
    missing = sorted(set(symbols) - set(adv))
    if missing:
        print(f"missing ADV for {len(missing)}: {missing}")


if __name__ == "__main__":
    main()
