#!/usr/bin/env python3
"""gw_packet.py — Groundwork combined Deal Packet for Rey's review.

Runs the three FEASIBILITY-stage agents for ONE deal and composes a single
branded HTML document so Rey reviews the whole picture in one file:

  1 · Feasibility & return profile     (Underwriter — deal_snapshot.underwrite)
  2 · Market grounding & rent check    (Researcher  — live HUD AMI/FMR)
  3 · Property management              (PM-Scout    — fee drag off THIS deal's EGI)

Numbers come straight from the canonical compute functions (single source of
truth); this module only lays them out. Build-Watcher (per-draw) and Door-Opener
(acquisition lead-gen) are separate workflow stages, not part of a feasibility
packet, so they keep their own standalone --html docs.

Usage:
  ./venv/bin/python gw_packet.py --sample d --html packet_d.html
  ./venv/bin/python gw_packet.py --deal mydeal.json --html packet.html
  ./venv/bin/python gw_packet.py --sample d --candidates pms.json --html out.html
"""
import argparse
import json
import sys
from pathlib import Path

import gw_report as R
from deal_snapshot import underwrite, SAMPLES, pct, money
from gw_researcher import build_context, check_deal
from gw_pmscout import rank as pm_rank, SAMPLE as PM_SAMPLE

BADGE = {"PENCILS": ("✓ PENCILS", "green"),
         "MARGINAL": ("~ MARGINAL", "amber"),
         "DOES NOT PENCIL": ("✗ DOES NOT PENCIL", "red")}


def snapshot_section(r):
    ret, u, s, val = r["returns"], r["uses"], r["sources"], r["value"]
    body = R.section("Why", R.ul(r["reasons"]))
    body += R.section("Value created", R.kv_rows([
        (f"Stabilized value @ {pct(val['exit_cap_rate'])}", money(val["stabilized_value"])),
        ("Value created over cost", f"{money(val['value_created'])} ({pct(ret['development_margin'])})")]))
    body += R.section("Sources & uses (EST)", R.kv_rows([
        ("Acquisition", money(u["acquisition"])), ("Rehab", money(u["rehab"])),
        ("Soft costs", money(u["soft_costs"])), ("Closing", money(u["closing"])),
        ("Total development cost", money(u["total_development_cost"]), "tot")]))
    body += R.section("Funding stack (EST)", R.kv_rows([
        (f"Senior debt ({r['debt']['binding_constraint']}-bound)", money(s["sized_loan"])),
        ("Soft money + credits", money(s["soft_money"] + s["tax_credit_equity"])),
        ("Equity / gap to fill", money(s["equity_required"]), "warn")]))
    return body


def researcher_section(deal, ctx, chk):
    names = {0: "Studio", 1: "1 BR", 2: "2 BR", 3: "3 BR", 4: "4 BR"}
    rows = []
    for br in range(5):
        b = ctx["rent_bands"][br]
        rows.append([(names[br], "nm"), f"${b['fmr']:,}", f"${b['max_rent_50']:,}",
                     f"${b['max_rent_60']:,}", f"${b['max_rent_80']:,}"])
    headers = [("Unit", "l"), ("FMR", "r"), ("50% AMI", "r"), ("60% AMI", "r"), ("80% AMI", "r")]
    body = R.section(f"Rent reality check (${deal['avg_rent_monthly']:,.0f}/mo, "
                     f"benchmarked {chk['benchmark_bedrooms']}BR)", R.ul(chk["flags"]))
    body += R.section("Fair market rent vs LIHTC-style max rent (gross, 1.5 persons/BR)",
                      R.table(headers, rows))
    body += R.section("Incentives & compliance — verify against primary sources",
                      R.ul([f"{i['name']}: {i['note']}" for i in ctx["incentives"]]))
    return body


def pm_section(deal, egi, candidates):
    rows = pm_rank(candidates, deal.get("units", 10), deal.get("location", ""), egi)
    headers = [("#", "r"), ("Manager", "l"), ("Score", "r"), ("Fee", "r"),
               ("Annual drag", "r"), ("AH?", "r")]
    trows = []
    for i, rw in enumerate(rows, 1):
        ah = "yes" if rw["raw"].get("affordable_experience") else "no"
        trows.append([i, (rw["name"], "nm"), f"{rw['score']:.0f}", f"{rw['fee_pct']*100:.1f}%",
                      f"${rw['fee_drag']:,.0f}", ah])
    top = rows[0]
    body = R.section(f"Property-manager shortlist (fee drag off this deal's EGI ${egi:,.0f})",
                     R.table(headers, trows, top1=True))
    body += R.section("Read", R.ul([
        f"Top fit: {top['name']} (score {top['score']:.0f}, {top['fee_pct']*100:.1f}% fee = "
        f"${top['fee_drag']:,.0f}/yr drag).",
        "PM candidates here are synthetic placeholders — replace with real diligence "
        "(licensing, AH compliance track record, references)."]))
    return body


def build_packet(deal, candidates):
    r = underwrite(deal)
    ctx = build_context(zip_code=deal.get("zip"))
    chk = check_deal(deal, ctx)
    egi = r["proforma"]["egi"]
    ret = r["returns"]
    kpis = [("Verdict", r["verdict"].split()[0]),
            ("Dev spread", f"{ret['development_spread_bps']:.0f} bps"),
            ("Yield-on-cost", pct(ret["yield_on_cost"])),
            ("Return on equity", pct(ret["return_on_equity"])),
            ("DSCR", f"{ret['dscr']:.2f}x"),
            ("Equity gap", money(r["sources"]["equity_required"]))]
    body = R.stage("1 · Feasibility & return profile") + snapshot_section(r)
    body += R.stage("2 · Market grounding (live HUD)") + researcher_section(deal, ctx, chk)
    body += R.stage("3 · Property management") + pm_section(deal, egi, candidates)
    foot = f"HUD data: {ctx['market']['source']}. " + R.DEFAULT_FOOT
    return R.page("GROUNDWORK · DEAL PACKET", deal.get("name", "(unnamed deal)"),
                  f"{deal.get('location', '')} · {deal['units']} units · combined feasibility review for Rey",
                  body, badge=BADGE[r["verdict"]], kpis=kpis, foot=foot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", choices=list(SAMPLES))
    ap.add_argument("--deal", help="path to a deal JSON file")
    ap.add_argument("--candidates", help="path to PM candidates JSON (defaults to sample list)")
    ap.add_argument("--html", required=True, help="write the combined packet HTML here")
    a = ap.parse_args()
    if a.deal:
        try:
            deal = json.loads(Path(a.deal).read_text())
        except FileNotFoundError:
            sys.exit(f"Error: deal file not found: {a.deal}")
    elif a.sample:
        deal = SAMPLES[a.sample]
    else:
        ap.error("give --sample or --deal")
    if a.candidates:
        try:
            candidates = json.loads(Path(a.candidates).read_text())
        except FileNotFoundError:
            sys.exit(f"Error: candidates file not found: {a.candidates}")
    else:
        candidates = PM_SAMPLE
    Path(a.html).write_text(build_packet(deal, candidates), encoding="utf-8")
    print(f"[deal packet -> {a.html}]")


if __name__ == "__main__":
    main()
