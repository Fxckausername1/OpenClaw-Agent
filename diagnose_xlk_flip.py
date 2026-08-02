"""diagnose_xlk_flip.py -- chases the 2026-07-04 handoff's flagged, unresolved
anomaly: UW's gex_levels flip for XLK (47.5) vs. our box's own Newton-Raphson
solved flip (~160-180 depending on session). Mirrors diagnose_xlu_xlre.py's
approach (query UW's own data multiple ways, don't assume our box is wrong).

Finding (2026-07-04): NOT a bug in our box. UW's raw gamma_flip field for XLK
is itself broken -- persistently anomalous across 6 separate trading dates,
while call_wall/put_wall/gamma_magnet from the SAME endpoint are all sane and
spot-proximate every single day. The likely mechanism: UW's flip algorithm
scans cumulative net gamma from the LOWEST strike upward and reports the
FIRST zero-crossing, rather than the crossing nearest spot. XLK's 45-52.5
strike band carries real but thin OI (77-215 contracts, vs. 1,600-4,200+ at
strikes just above it) -- deep OTM relative to spot (~180-190), where
per-contract gamma is near zero, so a small residual imbalance there is
enough to trip a spurious sign flip long before the sum ever reaches the
real, economically meaningful crossing near spot (which is where our box's
spot-anchored Newton-Raphson solver converges instead, and where UW's own
call_wall/put_wall already bracket). A quick spot-check of other liquid
tickers (SPY, XLF) shows sane spot-proximate flips, and QQQ/XLE/XLY
correctly return null (no crossing) rather than a garbage number -- so this
isn't a global UW bug, it's XLK-specific bad data on this one field.

Verdict: same lesson as the earlier XLU/XLRE resolution, via a different
mechanism -- trust our own box + UW's call_wall/put_wall over UW's raw
gamma_flip field.
"""
import unusualwhales_client as uw

TICKER = "XLK"
DATES = ["2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02"]

print(f"=== {TICKER} gex-levels across {len(DATES)} sessions ===")
for d in DATES:
    code, body = uw._get(f"/api/stock/{TICKER}/gex-levels", date=d)
    data = body["data"]
    print(f"  {d}: call_wall={data['call_wall']:>5} put_wall={data['put_wall']:>5} "
          f"gamma_magnet={data['gamma_magnet']:>5} gamma_flip={data['gamma_flip']!s:>6}  "
          f"{'<-- anomalous (way below put_wall)' if data['gamma_flip'] and float(data['gamma_flip']) < float(data['put_wall']) * 0.5 else ''}")

print()
print(f"=== {TICKER} OI-per-strike near the anomalous 47.5 level (low end of chain) ===")
code, body = uw.oi_per_strike(TICKER)
rows = sorted(body["data"], key=lambda r: float(r["strike"]))
for r in rows[:10]:
    print(f"  strike={r['strike']:>6} call_oi={r['call_oi']:>6} put_oi={r['put_oi']:>6}")

print()
print("=== sanity check: other liquid tickers' gex-levels (current) ===")
for t in ["SPY", "QQQ", "XLE", "XLF", "XLY"]:
    code, body = uw.gex_levels(t)
    print(f"  {t:5} {body['data']}")
