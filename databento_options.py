#!/usr/bin/env python3
"""
databento_options.py — budget-gated OPRA historical fetcher for the options backtest.

Spends REAL Databento credit. A HARD CEILING (--budget) is enforced: every actual
download is preceded by metadata get_cost() and skipped if it would breach the ceiling.
Definitions are CACHED to parquet — re-runs never re-charge for them.

Pipeline (cost-minimizing):
  1. DEFINITIONS (cheap) for the universe/window -> enumerate every contract. Cached.
  2. FILTER to contracts strategies actually trade: strike within an NTM band (from each
     name's free Alpaca underlying high/low over the window) + expiration <= window_end+max_dte.
  3. OHLCV-1d for ONLY those filtered raw_symbols (batched, stype_in=raw_symbol).

Outputs to data/options/:  <SYM>_defs.parquet, <SYM>_ohlcv1d.parquet, _manifest.json

Usage:
  ./venv/bin/python databento_options.py --budget 74 --plan          # cost-only (uses cached defs), $0
  ./venv/bin/python databento_options.py --budget 74 --arm           # actually pull, ceiling $74
"""
import argparse, json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import databento as db

ROOT = Path(__file__).resolve().parent
KEY = (ROOT / "credentials" / "databento.key").read_text().strip()
A_KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
A_SEC = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
OUT = ROOT / "data" / "options"
OUT.mkdir(parents=True, exist_ok=True)
DS = "OPRA.PILLAR"
DEFAULT_UNIVERSE = ["F", "BAC", "INTC", "PFE", "CSCO", "MU", "AMD", "PLTR", "XLF", "GDX", "HOOD", "NVDA"]
# only columns the backtest needs — keeps tiny-RAM box (1.9GB) from OOM on big defs
DEF_COLS = ["instrument_id", "raw_symbol", "strike_price", "expiration", "instrument_class"]


class Budget:
    def __init__(self, ceiling):
        self.ceiling = float(ceiling); self.spent = 0.0
    def afford(self, cost, label):
        if self.spent + cost > self.ceiling + 1e-9:
            print(f"   BUDGET STOP: '{label}' ${cost:.4f} would exceed ceiling "
                  f"(spent ${self.spent:.2f}/${self.ceiling:.2f}) -> SKIP"); return False
        return True
    def add(self, cost, label):
        self.spent += cost
        print(f"   spent ${cost:.4f} :: {label}   (running ${self.spent:.2f}/${self.ceiling:.2f})")


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def alpaca_band(sym, start, end):
    h = {"APCA-API-KEY-ID": A_KEY, "APCA-API-SECRET-KEY": A_SEC}
    p = {"symbols": sym, "timeframe": "1Day", "start": start, "end": end,
         "limit": 10000, "adjustment": "raw", "feed": "iex"}
    try:
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars", headers=h, params=p, timeout=25)
        bars = r.json().get("bars", {}).get(sym, [])
        if not bars:
            return None
        return (min(b["l"] for b in bars), max(b["h"] for b in bars))
    except Exception as e:
        print(f"   alpaca_band {sym} ERR {e}"); return None


def cost(c, schema, symbols, start, end, stype):
    return c.metadata.get_cost(dataset=DS, symbols=symbols, schema=schema,
                               start=start, end=end, stype_in=stype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=74.0)
    ap.add_argument("--universe", nargs="*", default=DEFAULT_UNIVERSE)
    ap.add_argument("--start", default="2025-12-01")
    ap.add_argument("--end", default="2026-06-01")
    ap.add_argument("--band", type=float, default=0.10)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--weeklies", action="store_true", help="include weekly expiries (default: monthly 3rd-Fri only)")
    ap.add_argument("--arm", action="store_true", help="actually download")
    ap.add_argument("--plan", action="store_true", help="cost-only using cached defs (spends $0)")
    ap.add_argument("--defs-only", action="store_true", help="pull/cache definitions only, skip bars")
    ap.add_argument("--stats", action="store_true", help="also pull statistics schema (open interest) for GEX")
    a = ap.parse_args()
    arm = a.arm and not a.plan
    bud = Budget(a.budget)
    c = db.Historical(KEY)
    exp_cap = pd.Timestamp((datetime.fromisoformat(a.end) + timedelta(days=a.max_dte)).date(), tz="UTC")
    manifest = {"created": datetime.now(timezone.utc).isoformat(), "dataset": DS,
                "window": [a.start, a.end], "band": a.band, "max_dte": a.max_dte,
                "arm": arm, "names": {}}
    planned_pull = 0.0
    print(f"=== databento_options  {a.start}->{a.end}  ceiling ${a.budget:.2f}  "
          f"mode={'ARM(spend)' if arm else 'PLAN($0)'} ===")

    for sym in a.universe:
        print(f"\n--- {sym} ---")
        parent = f"{sym}.OPT"
        defs_path = OUT / f"{sym}_defs.parquet"
        ohlcv_path = OUT / f"{sym}_ohlcv1d.parquet"
        defs = None
        # 1) DEFINITIONS (cached)
        if defs_path.exists():
            defs = pd.read_parquet(defs_path, columns=DEF_COLS)  # only needed cols -> low RAM
            print(f"   definitions: CACHED ({defs['raw_symbol'].nunique():,} contracts, $0)")
        else:
            try:
                dcost = cost(c, "definition", [parent], a.start, a.end, "parent")
            except Exception as e:
                print(f"   definition cost ERR {str(e)[:120]}"); continue
            print(f"   definition cost: ${dcost:.4f}")
            if arm and bud.afford(dcost, f"{sym} defs"):
                try:
                    defs = c.timeseries.get_range(dataset=DS, schema="definition", symbols=[parent],
                                                  start=a.start, end=a.end, stype_in="parent").to_df()
                    defs = defs[[col for col in DEF_COLS if col in defs.columns]].drop_duplicates("raw_symbol")
                    defs.to_parquet(defs_path); bud.add(dcost, f"{sym} defs")
                except Exception as e:
                    print(f"   definition pull ERR {str(e)[:160]}"); continue
            else:
                continue
        if a.defs_only:
            continue
        # 2) FILTER to NTM / short-DTE contracts
        band = alpaca_band(sym, a.start, a.end)
        if band:
            lo, hi = band[0] * (1 - a.band), band[1] * (1 + a.band)
            print(f"   underlying ~[{band[0]:.1f},{band[1]:.1f}] -> strikes [{lo:.0f},{hi:.0f}]")
        else:
            lo, hi = 0, 1e12; print("   (no band; all strikes)")
        df = defs.drop_duplicates("raw_symbol").copy()
        sk = df["strike_price"].astype(float)
        if sk.max() > 1e6:
            sk = sk / 1e9
        exp = pd.to_datetime(df["expiration"], utc=True)
        keep = (sk >= lo) & (sk <= hi) & (exp <= exp_cap)
        if not a.weeklies:  # standard monthly = 3rd Friday (weekday 4, day 15-21)
            keep = keep & (exp.dt.weekday == 4) & (exp.dt.day.between(15, 21))
        narrowed = sorted(df.loc[keep, "raw_symbol"].unique().tolist())
        exptag = "monthly" if not a.weeklies else "all-exp"
        print(f"   contracts: {df['raw_symbol'].nunique():,} -> {len(narrowed):,} kept "
              f"(NTM band ±{a.band:.0%}, {exptag}, <= {exp_cap.date()})")
        if not narrowed:
            continue
        # 3) per-contract schemas — SMALL batches streamed to disk (1.9GB box, 0 swap)
        BATCH = 300
        schemas = [("ohlcv-1d", ohlcv_path)]
        if a.stats:
            schemas.append(("statistics", OUT / f"{sym}_stats.parquet"))
        for schema, path in schemas:
            if path.exists():
                print(f"   {schema}: already saved, skip", flush=True); continue
            try:
                scost = sum(cost(c, schema, ch, a.start, a.end, "raw_symbol") for ch in chunks(narrowed, BATCH))
            except Exception as e:
                print(f"   {schema} cost ERR {str(e)[:140]}", flush=True); continue
            planned_pull += scost
            print(f"   {schema} (narrowed {len(narrowed)}) cost: ${scost:.4f}   [plan total ${planned_pull:.2f}]", flush=True)
            if not (arm and bud.afford(scost, f"{sym} {schema}")):
                continue
            try:
                writer = None; nrows = 0; tmp = path.with_suffix(".tmp.parquet")
                for ch in chunks(narrowed, BATCH):
                    df = c.timeseries.get_range(dataset=DS, schema=schema, symbols=ch, start=a.start,
                                                end=a.end, stype_in="raw_symbol").to_df().reset_index()
                    if len(df):
                        tbl = pa.Table.from_pandas(df, preserve_index=False)
                        if writer is None:
                            writer = pq.ParquetWriter(tmp, tbl.schema)
                        writer.write_table(tbl)
                        nrows += len(df)
                    del df
                if writer:
                    writer.close(); tmp.rename(path)
                    bud.add(scost, f"{sym} {schema}")
                    manifest["names"].setdefault(sym, {})[schema] = nrows
                    manifest["names"][sym]["contracts"] = len(narrowed)
                    print(f"   {schema}: wrote {nrows} rows -> {path.name}", flush=True)
                else:
                    print(f"   {schema}: no data", flush=True)
            except Exception as e:
                print(f"   {schema} pull ERR {str(e)[:160]}", flush=True)

    (OUT / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n=== SPENT ${bud.spent:.2f}/${a.budget:.2f} | planned pull ${planned_pull:.2f} | "
          f"{'ARMED' if arm else 'PLAN $0'} ===")


if __name__ == "__main__":
    main()
