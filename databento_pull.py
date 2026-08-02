#!/usr/bin/env python3
"""Databento historical data: cost preview + chunked pull for backtesting.

Pulls 1-minute OHLCV (no native 5-min schema; we resample locally) in
quarterly chunks sized for a 2GB-RAM box. Each chunk streams to a DBN file on
disk, converts to parquet, then the DBN is deleted. Resumable: existing
parquet chunks are skipped.

Usage:
  ./venv/bin/python databento_pull.py cost
  ./venv/bin/python databento_pull.py pull EQUS.MINI
Key: env DATABENTO_API_KEY or credentials/databento.key
"""
import os
import sys
from pathlib import Path
from datetime import date, timedelta

import databento as db

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "databento"
KEY_PATH = ROOT / "credentials" / "databento.key"

YEARS = 2
END = date.today() - timedelta(days=1)
START = END - timedelta(days=int(365.25 * YEARS))
SCHEMA = "ohlcv-1m"
CANDIDATE_DATASETS = ["EQUS.MINI", "XNAS.ITCH"]


def sp100_symbols():
    sys.path.insert(0, str(ROOT))
    import mean_reversion_scanner as mr
    return sorted({t.replace("-", ".") for t in mr.fetch_sp100()})


def client():
    key = os.environ.get("DATABENTO_API_KEY") or KEY_PATH.read_text().strip()
    return db.Historical(key)


def quarters(start, end):
    chunks = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=92), end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks


def preview():
    c = client()
    syms = sp100_symbols()
    print(f"Cost preview: {len(syms)} symbols, {SCHEMA}, {START} -> {END}\n")
    for ds in CANDIDATE_DATASETS:
        try:
            cost = c.metadata.get_cost(dataset=ds, symbols=syms, schema=SCHEMA,
                                       start=START.isoformat(), end=END.isoformat())
            print(f"  {ds:<12} ${cost:,.2f}")
        except Exception as e:
            print(f"  {ds:<12} unavailable: {str(e)[:120]}")


def pull(dataset):
    c = client()
    syms = sp100_symbols()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chunks = quarters(START, END)
    print(f"Pulling {dataset} {SCHEMA}: {len(syms)} symbols, "
          f"{len(chunks)} quarterly chunks, {START} -> {END}")
    total_rows = 0
    for i, (s, e) in enumerate(chunks, 1):
        tag = f"{s.isoformat()}_{e.isoformat()}"
        pq = OUT_DIR / f"chunk_{tag}.parquet"
        if pq.exists():
            print(f"[{i}/{len(chunks)}] {tag} exists, skipping")
            continue
        dbn = OUT_DIR / f"chunk_{tag}.dbn.zst"
        print(f"[{i}/{len(chunks)}] {tag} downloading...", flush=True)
        c.timeseries.get_range(dataset=dataset, symbols=syms, schema=SCHEMA,
                               start=s.isoformat(), end=e.isoformat(),
                               path=str(dbn))
        df = db.DBNStore.from_file(str(dbn)).to_df()
        df.to_parquet(pq)
        total_rows += len(df)
        dbn.unlink()
        print(f"    {len(df):,} rows -> {pq.name}")
        del df
    print(f"Done. New rows this run: {total_rows:,}")
    print(f"Chunks on disk: {len(list(OUT_DIR.glob('chunk_*.parquet')))}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cost"
    if cmd == "cost":
        preview()
    elif cmd == "pull":
        if len(sys.argv) < 3:
            raise SystemExit("usage: databento_pull.py pull <DATASET>")
        pull(sys.argv[2])
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
