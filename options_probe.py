#!/usr/bin/env python3
"""
options_probe.py — verify Alpaca PAPER account options access + data availability.
Answers the NEXT-MISSION open questions #1 and #2:
  1. What options TRADING LEVEL is the paper account approved for? (spreads need L2/L3)
  2. How much historical / live options DATA can we pull on the bootstrap (free) feed?
Read-only. Places no orders. Reuses the executor's credential + base-url pattern.
"""
import sys, json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import requests

ROOT = Path(__file__).resolve().parent
KEY = (ROOT / "credentials" / "alpaca_key.txt").read_text().strip()
SECRET = (ROOT / "credentials" / "alpaca_secret.txt").read_text().strip()
H = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}

PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"

def get(base, path, **params):
    r = requests.get(base + path, headers=H, params=params or None, timeout=25)
    return r.status_code, (r.json() if r.headers.get("content-type","").startswith("application/json") else r.text)

def hdr(t): print(f"\n{'='*62}\n{t}\n{'='*62}")

# ---------------------------------------------------------------- 1. ACCOUNT
hdr("1. ACCOUNT — options trading level")
sc, acct = get(PAPER, "/v2/account")
if sc == 200:
    for k in ("status","options_trading_level","options_approved_level",
              "options_buying_power","buying_power","cash","crypto_status",
              "shorting_enabled","pattern_day_trader","multiplier"):
        if k in acct:
            print(f"  {k:28s}: {acct[k]}")
    lvl = acct.get("options_trading_level", acct.get("options_approved_level"))
    print(f"\n  >> EFFECTIVE OPTIONS LEVEL = {lvl}")
    print("     L0/none=no options · L1=covered/CSP · L2=long opt+spreads · L3=naked/all spreads")
else:
    print(f"  ERROR {sc}: {acct}")

# ---------------------------------------------------------------- 2. CONTRACTS
hdr("2. TRADING API — option contracts discoverable? (/v2/options/contracts)")
sample = None
sc, contracts = get(PAPER, "/v2/options/contracts", underlying_symbols="SPY", limit=5)
if sc == 200:
    cs = contracts.get("option_contracts", [])
    print(f"  status {sc} · {len(cs)} SPY contracts returned (sample):")
    for c in cs[:5]:
        print(f"    {c.get('symbol')}  {c.get('type'):4s}  K={c.get('strike_price')}  exp={c.get('expiration_date')}  oi={c.get('open_interest')}")
    if cs: sample = cs[0]["symbol"]
else:
    print(f"  ERROR {sc}: {contracts}")

# ---------------------------------------------------------------- 3. LIVE QUOTES
hdr("3. DATA API — live chain snapshot (free 'indicative' feed)")
sc, snap = get(DATA, "/v1beta1/options/snapshots/SPY", feed="indicative", limit=3)
if sc == 200:
    snaps = snap.get("snapshots", {})
    print(f"  status {sc} · {len(snaps)} snapshots (indicative). sample:")
    for symb, s in list(snaps.items())[:3]:
        q = s.get("latestQuote", {})
        print(f"    {symb}  bid={q.get('bp')} ask={q.get('ap')} t={q.get('t')}")
    if not snaps:
        print("    (empty — try opra feed or market hours)")
else:
    print(f"  status {sc}: {str(snap)[:300]}")

# ---------------------------------------------------------------- 4. HISTORY DEPTH
hdr("4. DATA API — historical options bars depth (how far back?)")
if sample:
    # probe earliest available daily bars by asking from 2022
    start = "2022-01-01"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sc, bars = get(DATA, "/v1beta1/options/bars", symbols=sample,
                   timeframe="1Day", start=start, end=end, limit=10000, feed="indicative")
    if sc == 200:
        b = bars.get("bars", {}).get(sample, [])
        if b:
            print(f"  contract {sample}: {len(b)} daily bars")
            print(f"    EARLIEST bar: {b[0].get('t')}  ·  LATEST bar: {b[-1].get('t')}")
        else:
            print(f"  contract {sample}: 0 bars in [{start}..{end}] (history thin/none on free feed)")
    else:
        print(f"  status {sc}: {str(bars)[:300]}")
else:
    print("  (no sample contract from step 2 — skipping)")

# ---------------------------------------------------------------- 5. FEED ACCESS
hdr("5. FEED ACCESS — is paid OPRA available, or indicative only?")
sc, opra = get(DATA, "/v1beta1/options/snapshots/SPY", feed="opra", limit=1)
print(f"  opra feed snapshot status: {sc}  -> {'OPRA ACCESSIBLE' if sc==200 else 'indicative-only (free tier)'}")
if sc != 200:
    print(f"    {str(opra)[:200]}")

print("\nDONE.")
