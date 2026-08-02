#!/usr/bin/env python3
"""gw_pmscout.py — Groundwork's PM-Scout agent.

At 8-15 units a deal needs a third-party property manager, and the PM fee +
their competence flow straight into the proforma the Underwriter runs. PM-Scout
scores and ranks PM candidates against a deal's needs and shows the fee DRAG on
NOI, so the choice is made on the numbers (Rey's rule: returns decide).

Scoring (0-100, transparent weights — tune in WEIGHTS):
  • unit-count fit     — does the deal's size sit in the PM's sweet spot?
  • affordable exp.    — have they run AH / mixed-income / compliance reporting?
  • fee competitiveness— lower mgmt fee % is better (it's NOI leakage)
  • market presence    — do they actually operate in the submarket?
  • tech / reporting   — owner portal, monthly reporting cadence

Output ranks candidates and, if a deal's NOI is supplied, shows the annual fee
drag each one implies. Pure stdlib; candidate data is yours to supply (sample
list is synthetic and clearly marked).

Usage:
  ./venv/bin/python gw_pmscout.py --sample
  ./venv/bin/python gw_pmscout.py --candidates pms.json --units 12 --egi 250000
  ./venv/bin/python gw_pmscout.py --sample --json
"""
import argparse
import json
from pathlib import Path

WEIGHTS = {"unit_fit": 0.25, "affordable": 0.25, "fee": 0.20, "market": 0.20, "tech": 0.10}
# Fee scoring band: <=4% is excellent for small MF, >=8% is poor.
FEE_BEST, FEE_WORST = 0.04, 0.08


def _unit_fit(units, lo, hi):
    if lo <= units <= hi:
        return 1.0
    # graceful falloff outside the stated range
    gap = (lo - units) if units < lo else (units - hi)
    return max(0.0, 1.0 - gap / 10.0)


def _fee_score(fee_pct):
    if fee_pct <= FEE_BEST:
        return 1.0
    if fee_pct >= FEE_WORST:
        return 0.0
    return 1.0 - (fee_pct - FEE_BEST) / (FEE_WORST - FEE_BEST)


def score(cand, units, submarket=None):
    fit = _unit_fit(units, cand.get("min_units", 1), cand.get("max_units", 9999))
    aff = 1.0 if cand.get("affordable_experience") else 0.25
    fee = _fee_score(cand["fee_pct"])
    markets = [m.lower() for m in cand.get("markets", [])]
    if submarket:
        mkt = 1.0 if submarket.lower() in markets else 0.3
    else:
        mkt = 1.0 if markets else 0.5
    tech = 0.5 * bool(cand.get("owner_portal")) + 0.5 * bool(cand.get("monthly_reporting"))
    parts = {"unit_fit": fit, "affordable": aff, "fee": fee, "market": mkt, "tech": tech}
    total = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS) * 100
    return total, parts


def rank(cands, units, submarket=None, egi=None):
    rows = []
    for c in cands:
        s, parts = score(c, units, submarket)
        fee_drag = c["fee_pct"] * egi if egi else None
        rows.append({"name": c["name"], "score": s, "parts": parts,
                     "fee_pct": c["fee_pct"], "fee_drag": fee_drag, "raw": c})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def format_report(rows, units, submarket, egi):
    L = ["=" * 78,
         "  GROUNDWORK · PM-SCOUT — third-party PM shortlist  [DRAFT]",
         f"  Deal: {units} units" + (f" · {submarket}" if submarket else "")
         + (f" · EGI {egi:,.0f}" if egi else ""),
         "=" * 78,
         f"  {'#':<3}{'Manager':<26}{'Score':>7}{'Fee':>7}"
         + (f"{'Annual drag':>14}" if egi else "") + f"{'  AH?':<6}",
         "  " + "-" * 76]
    for i, r in enumerate(rows, 1):
        ah = "yes" if r["raw"].get("affordable_experience") else "no"
        drag = f"{r['fee_drag']:>13,.0f}" if r["fee_drag"] is not None else ""
        L.append(f"  {i:<3}{r['name'][:25]:<26}{r['score']:>6.0f} {r['fee_pct']*100:>5.1f}%"
                 + (f"{drag:>14}" if egi else "") + f"  {ah:<6}")
    L.append("  " + "-" * 76)
    top = rows[0]
    L.append(f"  → Top fit: {top['name']} (score {top['score']:.0f}).")
    sub = "  Subscores [fit/affordable/fee/market/tech]: " + ", ".join(
        f"{k}={top['parts'][k]:.2f}" for k in WEIGHTS)
    L.append(sub)
    if egi:
        spread = max(r["fee_drag"] for r in rows) - min(r["fee_drag"] for r in rows)
        L.append(f"  Fee-drag spread across the field: {spread:,.0f}/yr of NOI — that")
        L.append(f"  difference compounds into the stabilized value at the exit cap.")
    L.append("\n  Score is a transparent weighted heuristic; verify licensing, AH")
    L.append("  compliance track record, and references before engaging. [verify]")
    L.append("=" * 78)
    return "\n".join(L)


SAMPLE = [  # synthetic — replace with real diligence
    {"name": "Peachtree Residential Mgmt", "fee_pct": 0.06, "min_units": 5, "max_units": 50,
     "affordable_experience": True, "markets": ["grant park", "east atlanta", "intown"],
     "owner_portal": True, "monthly_reporting": True},
    {"name": "Skyline Asset Co.", "fee_pct": 0.045, "min_units": 20, "max_units": 300,
     "affordable_experience": True, "markets": ["midtown", "buckhead"],
     "owner_portal": True, "monthly_reporting": True},
    {"name": "HomeKey Small-MF", "fee_pct": 0.08, "min_units": 2, "max_units": 20,
     "affordable_experience": False, "markets": ["intown", "westside"],
     "owner_portal": True, "monthly_reporting": False},
    {"name": "Cornerstone AH Partners", "fee_pct": 0.07, "min_units": 8, "max_units": 120,
     "affordable_experience": True, "markets": ["southside", "adair park", "grant park"],
     "owner_portal": False, "monthly_reporting": True},
]


def render_html(rows, units, submarket, egi):
    import gw_report as R
    headers = [("#", "r"), ("Manager", "l"), ("Score", "r"), ("Fee", "r")]
    if egi:
        headers.append(("Annual drag", "r"))
    headers.append(("AH?", "r"))
    trows = []
    for i, r in enumerate(rows, 1):
        ah = "yes" if r["raw"].get("affordable_experience") else "no"
        row = [i, (r["name"], "nm"), f"{r['score']:.0f}", f"{r['fee_pct']*100:.1f}%"]
        if egi:
            row.append(f"${r['fee_drag']:,.0f}")
        row.append(ah)
        trows.append(row)
    top = rows[0]
    body = R.section(f"Shortlist — {units} units" + (f" · {submarket}" if submarket else ""),
                     R.table(headers, trows, top1=True))
    sub = ", ".join(f"{k} {top['parts'][k]:.2f}" for k in WEIGHTS)
    notes = [f"Top fit: {top['name']} (score {top['score']:.0f}).",
             f"Subscores [fit/affordable/fee/market/tech]: {sub}."]
    if egi:
        spread = max(r["fee_drag"] for r in rows) - min(r["fee_drag"] for r in rows)
        notes.append(f"Fee-drag spread across the field: ${spread:,.0f}/yr of NOI — that "
                     f"difference compounds into stabilized value at the exit cap.")
    notes.append("Score is a transparent weighted heuristic; verify licensing, AH compliance "
                 "track record, and references before engaging.")
    body += R.section("Read", R.ul(notes))
    kpis = [("Top fit", top["name"]), ("Score", f"{top['score']:.0f}"),
            ("Mgmt fee", f"{top['fee_pct']*100:.1f}%")]
    return R.page("GROUNDWORK · PM-SCOUT", "Property-manager shortlist",
                  f"{units}-unit deal" + (f" · {submarket}" if submarket else ""),
                  body, kpis=kpis)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", help="path to PM candidates JSON (list)")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--units", type=int, default=10)
    ap.add_argument("--submarket", default="Grant Park")
    ap.add_argument("--egi", type=float, default=None, help="effective gross income for fee-drag")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", help="write a styled HTML review doc to this path")
    a = ap.parse_args()
    if a.candidates:
        try:
            cands = json.loads(Path(a.candidates).read_text())
        except FileNotFoundError:
            raise SystemExit(f"Error: candidates file not found: {a.candidates}")
    elif a.sample:
        cands = SAMPLE
        if a.egi is None:
            a.egi = 250000.0
    else:
        ap.error("give --candidates <file.json> or --sample")
    rows = rank(cands, a.units, a.submarket, a.egi)
    if a.json:
        print(json.dumps([{k: r[k] for k in ("name", "score", "parts", "fee_pct", "fee_drag")}
                          for r in rows], indent=2))
    else:
        print(format_report(rows, a.units, a.submarket, a.egi))
    if a.html:
        Path(a.html).write_text(render_html(rows, a.units, a.submarket, a.egi), encoding="utf-8")
        print(f"\n[HTML review doc -> {a.html}]")


if __name__ == "__main__":
    main()
