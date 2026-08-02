#!/usr/bin/env python3
"""deal_snapshot.py — Groundwork's Deal Snapshot engine (v2: return-first + compare).

Deterministic underwriting for small value-add multifamily (8-15 units + rehab).
v2 bakes in the practitioner reality (per Rey): deals are chosen on RETURN PERCENTAGES,
not absolute dollars, and the better return profile wins. So the report leads with the
return profile, adds the comparative metrics people actually decide on (development
spread in bps vs cap, return-on-equity, debt yield), and adds a --compare mode that
RANKS deals by return.

Usage:
  ./venv/bin/python deal_snapshot.py --sample d                  # one deal (return-first)
  ./venv/bin/python deal_snapshot.py --sample d --html out.html  # styled HTML report
  ./venv/bin/python deal_snapshot.py --compare all               # rank deals by return
  ./venv/bin/python deal_snapshot.py --compare a,c,d --rank-by roe --html cmp.html
  ./venv/bin/python deal_snapshot.py --deal mydeal.json
"""
import argparse
import json
from pathlib import Path

DEFAULTS = {
    "other_income_per_unit_monthly": 35.0, "vacancy_rate": 0.07, "rehab_per_unit": 55000.0,
    "soft_cost_pct": 0.18, "closing_pct": 0.03, "opex_ratio": 0.34, "pm_fee_pct": 0.05,
    "reserves_per_unit_annual": 300.0, "exit_cap_rate": 0.065, "soft_money": 0.0,
    "tax_credit_equity": 0.0, "rehab": True,
    "loan": {"ltc": 0.75, "ltv": 0.75, "interest_rate": 0.072, "amort_years": 30, "min_dscr": 1.20},
}
YOC_FLOOR = 0.065
SPREAD_TARGET_BPS = 100      # value-add development spread the deal should clear (100-150 typical)

RANK_KEYS = {  # human label, path into result, higher-is-better
    "spread": ("Dev spread (bps)", "development_spread_bps", True),
    "yoc": ("Yield-on-cost", "yield_on_cost", True),
    "roe": ("Return on equity", "return_on_equity", True),
    "coc": ("Cash-on-cash", "cash_on_cash", True),
    "dscr": ("DSCR", "dscr", True),
}
# Which return metric leads, by who's in the room:
#   partner  = Rey is a co-principal -> deal quality matters most -> development spread
#   flipper  = advising an investor deploying their own cash -> return on their equity
LENS = {"partner": "spread", "flipper": "roe"}


def money(x):
    return f"${x:,.0f}"


def pct(x):
    return "n/a" if x is None else f"{x*100:.1f}%"


def mortgage_constant_annual(rate, amort_years):
    i = rate / 12.0
    n = int(amort_years * 12)
    if n == 0:
        return rate          # interest-only: annual debt service = principal × rate
    if i == 0:
        return 12.0 / n
    return (i * (1 + i) ** n / ((1 + i) ** n - 1)) * 12.0


def gv(deal, key):
    return deal[key] if key in deal else DEFAULTS[key]


def underwrite(deal):
    units = deal["units"]
    rent = deal["avg_rent_monthly"]
    loan = {**DEFAULTS["loan"], **deal.get("loan", {})}
    acq = deal["acquisition_price"]
    rehab_total = deal.get("rehab_total", gv(deal, "rehab_per_unit") * units)
    soft_costs = gv(deal, "soft_cost_pct") * rehab_total
    closing = gv(deal, "closing_pct") * acq
    tdc = acq + rehab_total + soft_costs + closing

    gpr_rent = rent * units * 12.0
    gpr = gpr_rent + gv(deal, "other_income_per_unit_monthly") * units * 12.0
    vacancy_loss = gv(deal, "vacancy_rate") * gpr_rent
    egi = gpr - vacancy_loss
    opex = gv(deal, "opex_ratio") * egi
    pm_fee = gv(deal, "pm_fee_pct") * egi
    reserves = gv(deal, "reserves_per_unit_annual") * units
    total_opex = opex + pm_fee + reserves
    noi = egi - total_opex

    cap = gv(deal, "exit_cap_rate")
    stabilized_value = noi / cap if cap > 0 else 0.0

    K = mortgage_constant_annual(loan["interest_rate"], loan["amort_years"])
    loan_ltc, loan_ltv = loan["ltc"] * tdc, loan["ltv"] * stabilized_value
    loan_dscr = (noi / loan["min_dscr"]) / K if K > 0 else 0.0
    sized_loan = min(loan_ltc, loan_ltv, loan_dscr)
    binding = min((loan_ltc, "LTC"), (loan_ltv, "LTV"), (loan_dscr, "DSCR"))[1]
    annual_ds = sized_loan * K
    dscr_actual = noi / annual_ds if annual_ds > 0 else 0.0

    soft_money, credits = gv(deal, "soft_money"), gv(deal, "tax_credit_equity")
    equity_required = max(tdc - (sized_loan + soft_money + credits), 0.0)

    # --- return profile (what decision-makers compare first) ---
    yoc = noi / tdc if tdc > 0 else 0.0
    spread_bps = (yoc - cap) * 10000
    dev_margin = (stabilized_value - tdc) / tdc if tdc > 0 else 0.0
    value_created = stabilized_value - tdc
    return_on_equity = value_created / equity_required if equity_required > 0 else None
    debt_yield = noi / sized_loan if sized_loan > 0 else None
    cash_flow = noi - annual_ds
    coc = cash_flow / equity_required if equity_required > 0 else None

    reasons = []
    reasons.append((f"development spread {spread_bps:.0f} bps clears the {SPREAD_TARGET_BPS}-bps value-add target"
                    if spread_bps >= SPREAD_TARGET_BPS else
                    f"development spread is only {spread_bps:.0f} bps (target {SPREAD_TARGET_BPS}+)"))
    reasons.append((f"yield-on-cost {pct(yoc)} clears the ~{pct(YOC_FLOOR)} floor"
                    if yoc >= YOC_FLOOR else f"yield-on-cost {pct(yoc)} is below the ~{pct(YOC_FLOOR)} floor"))
    reasons.append((f"supports {dscr_actual:.2f}x DSCR"
                    if dscr_actual >= loan["min_dscr"] else f"DSCR {dscr_actual:.2f}x under the {loan['min_dscr']:.2f}x min"))
    passes = sum([spread_bps >= SPREAD_TARGET_BPS, yoc >= YOC_FLOOR, dscr_actual >= loan["min_dscr"]])
    verdict = "PENCILS" if passes == 3 else ("MARGINAL" if passes == 2 else "DOES NOT PENCIL")

    return {
        "uses": {"acquisition": acq, "rehab": rehab_total, "soft_costs": soft_costs,
                 "closing": closing, "total_development_cost": tdc},
        "proforma": {"gpr": gpr, "vacancy_loss": vacancy_loss, "egi": egi,
                     "operating_expenses": opex, "property_management": pm_fee,
                     "reserves": reserves, "noi": noi},
        "value": {"stabilized_value": stabilized_value, "exit_cap_rate": cap, "value_created": value_created},
        "debt": {"loan_by_ltc": loan_ltc, "loan_by_ltv": loan_ltv, "loan_by_dscr": loan_dscr,
                 "sized_loan": sized_loan, "binding_constraint": binding,
                 "annual_debt_service": annual_ds, "dscr": dscr_actual},
        "sources": {"sized_loan": sized_loan, "soft_money": soft_money,
                    "tax_credit_equity": credits, "equity_required": equity_required},
        "returns": {"development_spread_bps": spread_bps, "yield_on_cost": yoc,
                    "development_margin": dev_margin, "return_on_equity": return_on_equity,
                    "cash_on_cash": coc, "debt_yield": debt_yield, "dscr": dscr_actual},
        "verdict": verdict, "reasons": reasons,
    }


FUNDING_PROGRAMS = [
    "FHLBank Atlanta — Affordable Housing Program (AHP) grants",
    "Georgia DCA — HOME / National Housing Trust Fund / CHIP gap funds",
    "Invest Atlanta — Housing Opportunity Bond + Affordable Housing Trust Fund",
    "ANDP / LISC Atlanta / Reinvestment Fund — CDFI acquisition & rehab debt",
    "Federal Historic Tax Credit (20%) + Georgia State HTC (certified historic rehab)",
    "Section 179D / 45L energy incentives (subject to the 6/30/2026 sunset)",
]


def incentive_flags(deal):
    flags = []
    sqft = deal.get("sqft")
    if sqft and sqft >= 25000:
        flags.append(f"CBEEO applies (~{sqft:,.0f} sf): annual benchmarking + ASHRAE Level II audit; "
                     "non-compliance = $1,000/yr fine + public disclosure.")
    if deal.get("rehab", True) and (deal.get("historic") or (deal.get("year_built") and deal["year_built"] < 1936)):
        flags.append("Historic Tax Credit candidate (federal 20% + GA 25%) IF certifiable — verify with SHPO.")
    if deal.get("property_type", "multifamily") in ("commercial", "mixed-use"):
        flags.append("179D may apply to commercial space if construction began on/before 6/30/2026 (retroactive only now).")
    flags.append("45L (residential) ended for homes acquired after 6/30/2026 — retroactive only.")
    return flags


def _return_lines(ret, lead=None):
    lines = {
        "spread": f"     Development spread ....... {ret['development_spread_bps']:.0f} bps  (value-add target {SPREAD_TARGET_BPS}-150)",
        "yoc": f"     Yield-on-cost ............ {pct(ret['yield_on_cost'])}",
        "roe": f"     Return on equity ......... {pct(ret['return_on_equity'])}  (stabilized, levered)",
        "coc": f"     Cash-on-cash ............. {pct(ret['cash_on_cash'])}",
        "dy": f"     Debt yield ............... {pct(ret['debt_yield'])}",
        "dscr": f"     DSCR ..................... {ret['dscr']:.2f}x",
    }
    order = ["spread", "yoc", "roe", "coc", "dy", "dscr"]
    if lead and lead in lines:
        order.remove(lead)
        order.insert(0, lead)
    return [lines[k] for k in order]


def format_report(deal, r, lens=None):
    u, p, val, d, s, ret = r["uses"], r["proforma"], r["value"], r["debt"], r["sources"], r["returns"]
    mark = {"PENCILS": "✓ PENCILS", "MARGINAL": "~ MARGINAL", "DOES NOT PENCIL": "✗ DOES NOT PENCIL"}[r["verdict"]]
    L = ["=" * 70,
         "  GROUNDWORK · DEAL SNAPSHOT  (DRAFT — for expert review)",
         f"  {deal.get('name','(unnamed)')} · {deal.get('location','')}",
         f"  {deal['units']} units · {'value-add rehab' if deal.get('rehab',True) else 'acquisition'}",
         "=" * 70,
         f"\n  VERDICT:  {mark}"]
    for reason in r["reasons"]:
        L.append(f"     - {reason}")
    lead = LENS.get(lens) if lens else None
    note = {"partner": "PARTNER lens — leading on development spread (deal quality)",
            "flipper": "FLIPPER / INVESTOR lens — leading on return-on-equity (their cash)"}.get(lens)
    if note:
        L.append(f"\n  [{note}]")
    L.append("\n  >>> RETURN PROFILE — what decision-makers compare first  [ESTIMATE]")
    L += _return_lines(ret, lead)
    L.append(f"\n  Value created ............ {money(val['value_created'])}  (margin {pct(ret['development_margin'])})")
    L.append("\n  SOURCES & USES  [ESTIMATE]")
    for k, lab in [("acquisition", "Acquisition"), ("rehab", "Rehab"), ("soft_costs", "Soft costs"),
                   ("closing", "Closing"), ("total_development_cost", "TOTAL DEVELOPMENT COST")]:
        L.append(f"     {lab:<24} {money(u[k])}")
    L.append("\n  STABILIZED PROFORMA (as renovated)  [ESTIMATE]")
    L.append(f"     Effective gross income ... {money(p['egi'])}")
    L.append(f"     Total opex (incl PM+res) . ({money(p['operating_expenses']+p['property_management']+p['reserves'])})")
    L.append(f"     NET OPERATING INCOME ..... {money(p['noi'])}")
    L.append(f"     Stabilized value @ {pct(val['exit_cap_rate'])} . {money(val['stabilized_value'])}")
    L.append("\n  FUNDING STACK  [ESTIMATE]")
    L.append(f"     Senior debt ({d['binding_constraint']}-bound) ... {money(s['sized_loan'])}")
    L.append(f"     Soft money + credits ..... {money(s['soft_money']+s['tax_credit_equity'])}")
    L.append(f"     EQUITY / GAP TO FILL ..... {money(s['equity_required'])}")
    L.append("\n  INCENTIVES & COMPLIANCE  [verify]")
    for f in incentive_flags(deal):
        L.append(f"     • {f}")
    L.append("\n  " + "-" * 66)
    L.append("  Return %s are leverage/subsidy-sensitive — read alongside the absolute")
    L.append("  $ and the risk. Every figure an ESTIMATE pending verification.")
    L.append("=" * 70)
    return "\n".join(L)


# ---------- comparison / ranking (Rey's rule: the better return wins) ----------
def compare(deals, rank_by="spread"):
    label, path, hi = RANK_KEYS[rank_by]
    rows = []
    for name, deal in deals:
        r = underwrite(deal)
        rows.append((name, deal, r))
    rows.sort(key=lambda x: (x[2]["returns"][path] if x[2]["returns"][path] is not None else -1e9), reverse=hi)
    return label, rows


def format_compare(label, rows):
    L = ["=" * 86, f"  GROUNDWORK · DEAL COMPARISON — ranked by {label} (best return first)", "=" * 86,
         f"  {'#':<3}{'Deal':<26}{'Verdict':<10}{'Spread':>8}{'YoC':>7}{'RoE':>8}{'CoC':>7}{'DSCR':>6}{'Equity':>11}",
         "  " + "-" * 84]
    for i, (name, deal, r) in enumerate(rows, 1):
        ret = r["returns"]
        v = {"PENCILS": "PENCILS", "MARGINAL": "MARGINAL", "DOES NOT PENCIL": "NO-GO"}[r["verdict"]]
        L.append(f"  {i:<3}{name[:25]:<26}{v:<10}{ret['development_spread_bps']:>6.0f}bp"
                 f"{pct(ret['yield_on_cost']):>7}{pct(ret['return_on_equity']):>8}"
                 f"{pct(ret['cash_on_cash']):>7}{r['returns']['dscr']:>5.2f}x"
                 f"{money(r['sources']['equity_required']):>11}")
    L.append("  " + "-" * 84)
    L.append(f"  → Top pick by {label}: {rows[0][0]}.  (Ranking ignores risk/size — confirm against the $.)")
    L.append("=" * 86)
    return "\n".join(L)


# ---------- HTML renders ----------
def render_html(deal, r):
    u, p, val, d, s, ret = r["uses"], r["proforma"], r["value"], r["debt"], r["sources"], r["returns"]
    badge = {"PENCILS": ("✓ PENCILS", "#2e7d32", "#e6f4e6"),
             "MARGINAL": ("~ MARGINAL", "#9a7235", "#f7efdd"),
             "DOES NOT PENCIL": ("✗ DOES NOT PENCIL", "#b3261e", "#f7e4e2")}[r["verdict"]]
    bt, bc, bb = badge
    kpis = [("Dev spread", f"{ret['development_spread_bps']:.0f} bps"), ("Yield-on-cost", pct(ret['yield_on_cost'])),
            ("Return on equity", pct(ret['return_on_equity'])), ("Cash-on-cash", pct(ret['cash_on_cash'])),
            ("Debt yield", pct(ret['debt_yield'])), ("DSCR", f"{ret['dscr']:.2f}x")]
    tiles = "".join(f'<div class="kpi"><span class="kl">{l}</span><span class="kv">{v}</span></div>' for l, v in kpis)
    reasons = "".join(f"<li>{x}</li>" for x in r["reasons"])
    incentives = "".join(f"<li>{x}</li>" for x in incentive_flags(deal))

    def row(label, value, cls=""):
        return f'<div class="r"><span>{label}</span><span class="v {cls}">{value}</span></div>'
    su = "".join(row(l, money(u[k]), "tot" if k == "total_development_cost" else "")
                 for k, l in [("acquisition", "Acquisition"), ("rehab", "Rehab"), ("soft_costs", "Soft costs"),
                              ("closing", "Closing"), ("total_development_cost", "Total development cost")])
    fund = (row("Senior debt", money(s['sized_loan'])) + row("Soft money + credits", money(s['soft_money'] + s['tax_credit_equity'])) +
            row("Equity / gap to fill", money(s['equity_required']), "warn"))
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Groundwork Deal Snapshot — {deal.get('name','')}</title>
<style>
:root{{--forest:#2e4a3f;--sage:#7a9b76;--paper:#f7f5f0;--ink:#2b2b2b;--muted:#5d6b62;--gold:#b08641;--line:#e3ded3;}}
*{{box-sizing:border-box;margin:0;padding:0;}} body{{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--paper);color:var(--ink);line-height:1.5;padding:24px;}}
.sheet{{max-width:780px;margin:0 auto;background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 8px 30px rgba(46,74,63,.08);}}
.top{{background:var(--forest);color:#fff;padding:24px 28px;}} .brand{{font-family:Georgia,serif;letter-spacing:3px;font-size:14px;color:var(--sage);}}
.top h1{{font-family:Georgia,serif;font-size:22px;margin-top:6px;}} .top .sub{{font-size:13px;color:#bcccc2;margin-top:4px;}}
.badge{{display:inline-block;margin-top:14px;background:{bb};color:{bc};font-weight:700;font-size:15px;padding:7px 16px;border-radius:8px;}}
.kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);}}
.kpi{{background:#fbfaf6;padding:16px 18px;text-align:center;}} .kl{{display:block;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);}}
.kv{{display:block;font-family:Georgia,serif;font-size:26px;color:var(--forest);margin-top:4px;}}
.kphead{{background:var(--gold);color:#fff;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;padding:7px 28px;font-weight:700;}}
.body{{padding:20px 28px;}} .sec{{margin-bottom:20px;}}
.sec h2{{font-family:Georgia,serif;color:var(--forest);font-size:14px;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid var(--line);padding-bottom:6px;margin-bottom:8px;}}
.r{{display:flex;justify-content:space-between;gap:14px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:14px;}} .r:last-child{{border-bottom:none;}}
.r .v{{font-weight:700;color:var(--forest);}} .r .v.warn{{color:var(--gold);}} .r .v.tot{{border-top:2px solid var(--forest);}}
ul{{margin:6px 0 0 18px;font-size:13px;color:var(--muted);}} li{{margin-bottom:5px;}} .reasons li{{color:var(--ink);}}
.foot{{background:#faf8f3;padding:14px 28px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--line);font-style:italic;}}
</style></head><body><div class="sheet">
<div class="top"><div class="brand">GROUNDWORK · DEAL SNAPSHOT</div><h1>{deal.get('name','')}</h1>
<div class="sub">{deal.get('location','')} · {deal['units']} units · DRAFT for review</div><div class="badge">{bt}</div></div>
<div class="kphead">Return profile — what decision-makers compare first</div>
<div class="kpis">{tiles}</div>
<div class="body">
<div class="sec"><h2>Why</h2><ul class="reasons">{reasons}</ul></div>
<div class="sec"><h2>Value created</h2>{row("Stabilized value @ "+pct(val['exit_cap_rate'])+" cap", money(val['stabilized_value']), "")}{row("Value created over cost", money(val['value_created'])+" ("+pct(ret['development_margin'])+")", "")}</div>
<div class="sec"><h2>Sources &amp; Uses <span style="font-size:10px;color:var(--gold);">EST</span></h2>{su}</div>
<div class="sec"><h2>Funding Stack <span style="font-size:10px;color:var(--gold);">EST</span></h2>{fund}</div>
<div class="sec"><h2>Incentives &amp; Compliance</h2><ul>{incentives}</ul></div>
</div>
<div class="foot">Return percentages are leverage- and subsidy-sensitive — read alongside the absolute $ and the risk. Every figure an ESTIMATE pending verification against primary sources. Not tax, legal, or investment advice.</div>
</div></body></html>"""


def render_compare_html(label, rows):
    trs = ""
    for i, (name, deal, r) in enumerate(rows, 1):
        ret = r["returns"]
        vmap = {"PENCILS": ("PENCILS", "#2e7d32"), "MARGINAL": ("MARGINAL", "#9a7235"), "DOES NOT PENCIL": ("NO-GO", "#b3261e")}
        vt, vc = vmap[r["verdict"]]
        top = ' class="top1"' if i == 1 else ""
        trs += (f'<tr{top}><td class="rk">{i}</td><td class="nm">{name}</td>'
                f'<td style="color:{vc};font-weight:700;">{vt}</td>'
                f'<td class="big">{ret["development_spread_bps"]:.0f} bps</td>'
                f'<td>{pct(ret["yield_on_cost"])}</td><td>{pct(ret["return_on_equity"])}</td>'
                f'<td>{pct(ret["cash_on_cash"])}</td><td>{ret["dscr"]:.2f}x</td>'
                f'<td class="dollar">{money(r["sources"]["equity_required"])}</td>'
                f'<td class="dollar">{money(r["value"]["value_created"])}</td></tr>')
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Groundwork — Deal Comparison</title>
<style>
:root{{--forest:#2e4a3f;--sage:#7a9b76;--paper:#f7f5f0;--ink:#2b2b2b;--muted:#5d6b62;--gold:#b08641;--line:#e3ded3;}}
*{{box-sizing:border-box;margin:0;padding:0;}} body{{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--paper);color:var(--ink);padding:24px;line-height:1.5;}}
.sheet{{max-width:920px;margin:0 auto;background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 8px 30px rgba(46,74,63,.08);}}
.top{{background:var(--forest);color:#fff;padding:24px 28px;}} .brand{{font-family:Georgia,serif;letter-spacing:3px;font-size:14px;color:var(--sage);}}
.top h1{{font-family:Georgia,serif;font-size:23px;margin-top:6px;}} .top .sub{{font-size:13px;color:#bcccc2;margin-top:4px;}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;}} th,td{{padding:11px 12px;text-align:right;border-bottom:1px solid var(--line);}}
th{{background:#243a31;color:#fff;font-family:Georgia,serif;font-weight:normal;font-size:12px;letter-spacing:.4px;}}
td.rk,th:first-child{{text-align:center;}} td.nm,th:nth-child(2),td:nth-child(3),th:nth-child(3){{text-align:left;}}
td.nm{{font-weight:700;color:var(--forest);}} td.rk{{font-family:Georgia,serif;color:var(--muted);}}
td.big{{font-family:Georgia,serif;font-size:18px;color:var(--gold);font-weight:700;}} td.dollar{{color:var(--muted);}}
tr.top1 td{{background:#eef6ee;}} tr.top1 td.rk{{color:var(--forest);font-weight:700;}}
.note{{padding:16px 28px;font-size:12.5px;color:var(--muted);}}
.foot{{background:#faf8f3;padding:14px 28px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--line);font-style:italic;}}
</style></head><body><div class="sheet">
<div class="top"><div class="brand">GROUNDWORK · DEAL COMPARISON</div><h1>Ranked by {label} — best return first</h1>
<div class="sub">The way deals actually get chosen: the stronger return profile wins the room.</div></div>
<table><thead><tr><th>#</th><th>Deal</th><th>Verdict</th><th>Dev spread</th><th>YoC</th><th>Return on equity</th><th>Cash-on-cash</th><th>DSCR</th><th>Equity in</th><th>Value created</th></tr></thead>
<tbody>{trs}</tbody></table>
<div class="note"><strong>Top pick by {label}: {rows[0][0]}.</strong> Return % is how the room decides — but it's leverage- and subsidy-sensitive, so the equity-in and value-created $ are shown alongside. A great % on a tiny equity check isn't always the bigger win.</div>
<div class="foot">Every figure an ESTIMATE pending verification against primary sources. Not tax, legal, or investment advice.</div>
</div></body></html>"""


SAMPLES = {
    "a": {"name": "Westside walk-up", "location": "Westside Atlanta", "units": 12, "property_type": "multifamily",
          "avg_rent_monthly": 1775, "acquisition_price": 1_020_000, "rehab_per_unit": 48_000, "exit_cap_rate": 0.065,
          "soft_money": 200_000, "tax_credit_equity": 150_000, "sqft": 13_500, "year_built": 1925, "historic": True},
    "b": {"name": "Eastside 8-unit", "location": "East Atlanta", "units": 8, "property_type": "multifamily",
          "avg_rent_monthly": 1250, "acquisition_price": 1_000_000, "rehab_per_unit": 60_000, "exit_cap_rate": 0.070,
          "soft_money": 100_000, "tax_credit_equity": 0, "sqft": 9_200, "year_built": 1972},
    "c": {"name": "Southside 15-unit", "location": "South Atlanta", "units": 15, "property_type": "multifamily",
          "avg_rent_monthly": 1600, "acquisition_price": 1_500_000, "rehab_per_unit": 50_000, "exit_cap_rate": 0.0625,
          "soft_money": 250_000, "tax_credit_equity": 200_000, "sqft": 16_800, "year_built": 1930, "historic": True},
    "d": {"name": "Grant Park 10-unit", "location": "Grant Park, Atlanta", "units": 10, "property_type": "multifamily",
          "avg_rent_monthly": 1800, "acquisition_price": 1_150_000, "rehab_per_unit": 52_000, "exit_cap_rate": 0.060,
          "soft_money": 175_000, "tax_credit_equity": 175_000, "sqft": 11_800, "year_built": 1928, "historic": True},
    "e": {"name": "Adair Park 11-unit", "location": "Adair Park, Atlanta", "units": 11, "property_type": "multifamily",
          "avg_rent_monthly": 1650, "acquisition_price": 1_150_000, "rehab_per_unit": 50_000, "exit_cap_rate": 0.0625,
          "soft_money": 200_000, "tax_credit_equity": 150_000, "sqft": 12_600, "year_built": 1931, "historic": True},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", choices=list(SAMPLES))
    ap.add_argument("--deal", help="path to a deal JSON file")
    ap.add_argument("--compare", help="'all' or comma list of samples to rank, e.g. a,c,d")
    ap.add_argument("--rank-by", choices=list(RANK_KEYS), default=None)
    ap.add_argument("--lens", choices=list(LENS), help="partner→rank by spread; flipper→rank by return-on-equity")
    ap.add_argument("--html", help="write a styled HTML report/comparison to this path")
    a = ap.parse_args()

    if a.compare:
        keys = list(SAMPLES) if a.compare == "all" else [k.strip() for k in a.compare.split(",")]
        deals = [(SAMPLES[k]["name"], SAMPLES[k]) for k in keys]
        rank_key = a.rank_by or (LENS[a.lens] if a.lens else "spread")
        label, rows = compare(deals, rank_key)
        print(format_compare(label, rows))
        if a.html:
            Path(a.html).write_text(render_compare_html(label, rows), encoding="utf-8")
            print(f"\n[comparison HTML -> {a.html}]")
    elif a.deal or a.sample:
        if a.deal:
            try:
                deal = json.loads(Path(a.deal).read_text())
            except FileNotFoundError:
                raise SystemExit(f"Error: deal file not found: {a.deal}")
        else:
            deal = SAMPLES[a.sample]
        r = underwrite(deal)
        print(format_report(deal, r, a.lens))
        if a.html:
            Path(a.html).write_text(render_html(deal, r), encoding="utf-8")
            print(f"\n[HTML report -> {a.html}]")
    else:
        ap.error("give --sample, --deal, or --compare")


if __name__ == "__main__":
    main()
