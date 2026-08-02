#!/usr/bin/env python3
"""ADV-threshold sweep (2026-06-28) -- reuses the price-filtered ($5-$266) universe already
cached by screen_wide_universe_adv.py's last run (fresh within the 20h cache window, so no
re-pull of the asset list/snapshots) and the SAME batched daily-volume fetch logic, but
fetches volume ONCE and reports counts at multiple ADV cutoffs in one pass instead of
re-fetching per threshold. Reporting only -- no cache rebuild, no universe file written
beyond what wide_universe.load_universe() already cached.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wide_universe as wu
from screen_wide_universe_adv import fetch_daily_volume_batch

THRESHOLDS = [1_000_000, 750_000, 500_000, 250_000]

candidates = wu.load_universe(rebuild_if_stale=False)  # reuse the fresh cache, no re-pull
print(f"price-filtered universe ($5-$266): {len(candidates)} symbols (reused cache, no re-pull)",
      file=sys.stderr)

adv = fetch_daily_volume_batch(candidates)
print(f"30-day ADV computed for {len(adv)}/{len(candidates)} symbols\n", file=sys.stderr)

print("=== ADV THRESHOLD SWEEP ===")
for t in THRESHOLDS:
    n = sum(1 for v in adv.values() if v > t)
    print(f"  ADV > {t:>10,} shares: {n:>4} tickers")
