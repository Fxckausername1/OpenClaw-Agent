#!/usr/bin/env python3
"""gw_buildwatch.py — Groundwork's Build-Watcher agent.

A deterministic auditor for AIA-style G702/G703 pay applications (draw requests)
on a rehab. This is owner's-rep work: catch the arithmetic and the games BEFORE
a draw gets certified. Pure stdlib, fully auditable — every flag cites the line
and the numbers, so Rey can confirm in seconds.

What it checks (the math a GC's bookkeeping sometimes "rounds"):
  G703 (continuation sheet), per line item:
    • completed_to_date  == from_previous + this_period + materials_stored
    • completed_to_date  <= scheduled_value           (no overbilling a line)
    • percent_complete    = completed_to_date / scheduled_value
    • balance_to_finish   = scheduled_value - completed_to_date
  G702 (summary), reconciled against the rolled-up G703:
    • sum(scheduled_value)          == contract_sum (+ approved change orders)
    • sum(completed_to_date)        == total_completed_and_stored
    • retainage                     == retainage_pct * total_completed_and_stored
    • total_earned_less_retainage   == total_completed_and_stored - retainage
    • current_payment_due           == total_earned_less_retainage - previous_payments
  Risk heuristics (warn, not fail):
    • front-loading: a line's % complete running far ahead of the project %
    • stored materials as a large share of a line (verify delivery/insurance)

Usage:
  ./venv/bin/python gw_buildwatch.py --sample clean
  ./venv/bin/python gw_buildwatch.py --sample flagged    # planted errors
  ./venv/bin/python gw_buildwatch.py --draw draw3.json
  ./venv/bin/python gw_buildwatch.py --draw draw3.json --json
"""
import argparse
import json
import sys
from pathlib import Path

TOL = 1.0                  # dollars; treat sub-$1 gaps as rounding, not errors
FRONTLOAD_FLAG = 0.25      # a line >25 pts ahead of project % gets a front-load warn
STORED_SHARE_FLAG = 0.40   # stored materials >40% of a line's billed work -> verify


def money(x):
    return f"${x:,.2f}"


def near(a, b, tol=TOL):
    return abs(a - b) <= tol


def audit(draw):
    lines = draw["line_items"]
    retainage_pct = draw.get("retainage_pct", 0.10)
    contract_sum = draw["contract_sum"] + draw.get("change_orders", 0.0)
    previous_payments = draw.get("previous_payments", 0.0)

    errors, warns = [], []

    # ---- per-line G703 checks ----
    sum_scheduled = sum_completed = 0.0
    line_rows = []
    for li in lines:
        n = li.get("item", "?")
        sv = li["scheduled_value"]
        prev = li.get("from_previous", 0.0)
        this = li.get("this_period", 0.0)
        stored = li.get("materials_stored", 0.0)
        ctd_stated = li.get("completed_to_date")
        ctd_calc = prev + this + stored

        if ctd_stated is not None and not near(ctd_stated, ctd_calc):
            errors.append(f"Line {n}: completed-to-date stated {money(ctd_stated)} "
                          f"!= prev+this+stored {money(ctd_calc)} "
                          f"(off by {money(ctd_stated - ctd_calc)}).")
        ctd = ctd_calc
        if ctd - sv > TOL:
            errors.append(f"Line {n}: OVERBILLED — completed {money(ctd)} exceeds "
                          f"scheduled value {money(sv)} by {money(ctd - sv)}.")
        pct = ctd / sv if sv else 0.0
        bal_stated = li.get("balance_to_finish")
        bal_calc = sv - ctd
        if bal_stated is not None and not near(bal_stated, bal_calc):
            errors.append(f"Line {n}: balance-to-finish stated {money(bal_stated)} "
                          f"!= {money(bal_calc)}.")
        if sv and stored > STORED_SHARE_FLAG * sv:
            warns.append(f"Line {n}: stored materials {money(stored)} are "
                         f"{stored / sv * 100:.0f}% of the line — verify delivery, "
                         f"site security, and insurance before paying on stored goods.")
        sum_scheduled += sv
        sum_completed += ctd
        line_rows.append((n, sv, ctd, pct, bal_calc, stored))

    project_pct = sum_completed / sum_scheduled if sum_scheduled else 0.0

    # ---- front-loading heuristic (needs the project % first) ----
    for (n, sv, ctd, pct, _bal, _st) in line_rows:
        if sv and pct - project_pct > FRONTLOAD_FLAG:
            warns.append(f"Line {n}: {pct*100:.0f}% complete vs project {project_pct*100:.0f}% "
                         f"— possible front-loading; confirm work is actually in place.")

    # ---- G702 summary reconciliation ----
    if not near(sum_scheduled, contract_sum):
        errors.append(f"G702: schedule-of-values total {money(sum_scheduled)} "
                      f"!= contract sum + COs {money(contract_sum)} "
                      f"(off by {money(sum_scheduled - contract_sum)}).")

    tcs_stated = draw.get("total_completed_and_stored")
    if tcs_stated is not None and not near(tcs_stated, sum_completed):
        errors.append(f"G702 line 4 (total completed & stored) stated {money(tcs_stated)} "
                      f"!= sum of G703 {money(sum_completed)}.")
    tcs = sum_completed

    retainage_calc = retainage_pct * tcs
    ret_stated = draw.get("retainage")
    if ret_stated is not None and not near(ret_stated, retainage_calc):
        errors.append(f"G702: retainage stated {money(ret_stated)} != "
                      f"{retainage_pct*100:.0f}% of {money(tcs)} = {money(retainage_calc)}.")
    retainage = retainage_calc

    earned_less_ret_calc = tcs - retainage
    elr_stated = draw.get("total_earned_less_retainage")
    if elr_stated is not None and not near(elr_stated, earned_less_ret_calc):
        errors.append(f"G702 line 6: total earned less retainage stated "
                      f"{money(elr_stated)} != {money(earned_less_ret_calc)}.")
    earned_less_ret = earned_less_ret_calc

    due_calc = earned_less_ret - previous_payments
    due_stated = draw.get("current_payment_due")
    if due_stated is not None and not near(due_stated, due_calc):
        errors.append(f"G702 line 9: CURRENT PAYMENT DUE stated {money(due_stated)} "
                      f"!= {money(earned_less_ret)} - {money(previous_payments)} "
                      f"prior = {money(due_calc)}.")

    if due_calc < -TOL:
        warns.append(f"Current payment due is NEGATIVE ({money(due_calc)}) — prior "
                     f"payments exceed earned-less-retainage; likely an overpayment.")

    verdict = "DISCREPANCIES FOUND" if errors else ("REVIEW FLAGS" if warns else "CLEAN")
    return {
        "verdict": verdict, "errors": errors, "warns": warns,
        "project_percent_complete": project_pct,
        "totals": {
            "schedule_of_values": sum_scheduled, "contract_sum": contract_sum,
            "total_completed_and_stored": tcs, "retainage": retainage,
            "total_earned_less_retainage": earned_less_ret,
            "previous_payments": previous_payments, "current_payment_due": due_calc,
        },
        "lines": [{"item": n, "scheduled_value": sv, "completed_to_date": ctd,
                   "percent": pct, "balance_to_finish": bal, "materials_stored": st}
                  for (n, sv, ctd, pct, bal, st) in line_rows],
    }


def format_report(draw, r):
    mark = {"CLEAN": "✓ CLEAN — math reconciles",
            "REVIEW FLAGS": "~ REVIEW FLAGS — arithmetic ok, judgment items",
            "DISCREPANCIES FOUND": "✗ DISCREPANCIES FOUND — do not certify as-is"}[r["verdict"]]
    t = r["totals"]
    L = ["=" * 74,
         "  GROUNDWORK · BUILD-WATCHER — Pay Application Audit  [DRAFT]",
         f"  {draw.get('project', '(project)')} · Application #{draw.get('application_no', '?')}",
         f"  Project {r['project_percent_complete']*100:.1f}% complete",
         "=" * 74,
         f"\n  VERDICT:  {mark}"]
    if r["errors"]:
        L.append("\n  ARITHMETIC / CONTRACT ERRORS  [must resolve before certifying]")
        for e in r["errors"]:
            L.append(f"     ✗ {e}")
    if r["warns"]:
        L.append("\n  JUDGMENT FLAGS  [owner's-rep review]")
        for w in r["warns"]:
            L.append(f"     ! {w}")
    if not r["errors"] and not r["warns"]:
        L.append("     - All line and summary figures reconcile within tolerance.")
    L.append("\n  G702 SUMMARY (recomputed from the G703)")
    for lab, key in [("Schedule-of-values total", "schedule_of_values"),
                     ("Contract sum (+ COs)", "contract_sum"),
                     ("Total completed & stored", "total_completed_and_stored"),
                     ("Retainage", "retainage"),
                     ("Total earned less retainage", "total_earned_less_retainage"),
                     ("Less previous payments", "previous_payments"),
                     ("CURRENT PAYMENT DUE", "current_payment_due")]:
        L.append(f"     {lab:<30} {money(t[key])}")
    L.append("\n  " + "-" * 70)
    L.append("  Recomputed independently from the line items. Confirm scope, lien")
    L.append("  waivers, and stored-materials documentation separately. Not a")
    L.append("  certification — supports the owner's-rep's signature.")
    L.append("=" * 74)
    return "\n".join(L)


# ---- samples ---------------------------------------------------------------
def _clean_sample():
    # 10-unit rehab, Application #3, 10% retainage. All math reconciles.
    lines = [
        {"item": "1 General conditions", "scheduled_value": 90000, "from_previous": 36000, "this_period": 18000, "materials_stored": 0},
        {"item": "2 Demolition", "scheduled_value": 60000, "from_previous": 60000, "this_period": 0, "materials_stored": 0},
        {"item": "3 Framing & carpentry", "scheduled_value": 140000, "from_previous": 42000, "this_period": 35000, "materials_stored": 0},
        {"item": "4 Plumbing", "scheduled_value": 110000, "from_previous": 22000, "this_period": 33000, "materials_stored": 11000},
        {"item": "5 Electrical", "scheduled_value": 95000, "from_previous": 19000, "this_period": 28500, "materials_stored": 0},
        {"item": "6 HVAC", "scheduled_value": 85000, "from_previous": 17000, "this_period": 17000, "materials_stored": 0},
        {"item": "7 Finishes", "scheduled_value": 120000, "from_previous": 12000, "this_period": 24000, "materials_stored": 0},
    ]
    for li in lines:  # fill the stated fields exactly (clean)
        ctd = li["from_previous"] + li["this_period"] + li["materials_stored"]
        li["completed_to_date"] = ctd
        li["balance_to_finish"] = li["scheduled_value"] - ctd
    sov = sum(li["scheduled_value"] for li in lines)
    tcs = sum(li["completed_to_date"] for li in lines)
    ret = round(0.10 * tcs, 2)
    elr = tcs - ret
    prev = 200000.0
    return {
        "project": "Grant Park 10-unit rehab", "application_no": 3, "retainage_pct": 0.10,
        "contract_sum": sov, "change_orders": 0.0, "previous_payments": prev,
        "total_completed_and_stored": tcs, "retainage": ret,
        "total_earned_less_retainage": elr, "current_payment_due": elr - prev,
        "line_items": lines,
    }


def _flagged_sample():
    # Same job with PLANTED problems (audit-game style):
    #   • Line 3 completed-to-date doesn't add up
    #   • Line 4 overbilled past its scheduled value
    #   • Line 7 front-loaded (88% complete on a ~40% job)
    #   • G702 current-payment-due understates retainage
    d = _clean_sample()
    d["project"] = "Adair Park 10-unit rehab (audit drill)"
    L = d["line_items"]
    L[2]["completed_to_date"] = 90000          # stated; real add-up is 77000  -> error
    L[3]["this_period"] = 80000                # 22k+80k+11k = 113k > 110k SV  -> overbill
    L[6]["this_period"] = 96000                # 12k+96k = 108k of 120k = 90%  -> front-load
    # recompute summary off the (wrong) stated/again to plant a retainage slip
    tcs = sum(li["from_previous"] + li["this_period"] + li["materials_stored"] for li in L)
    d["total_completed_and_stored"] = tcs
    d["retainage"] = round(0.05 * tcs, 2)      # billed 5% but contract says 10% -> error
    d["total_earned_less_retainage"] = tcs - d["retainage"]
    d["current_payment_due"] = d["total_earned_less_retainage"] - d["previous_payments"]
    return d


SAMPLES = {"clean": _clean_sample, "flagged": _flagged_sample}


def render_html(draw, r):
    import gw_report as R
    t = r["totals"]
    badge = {"CLEAN": ("✓ CLEAN — math reconciles", "green"),
             "REVIEW FLAGS": ("~ REVIEW FLAGS — judgment items", "amber"),
             "DISCREPANCIES FOUND": ("✗ DISCREPANCIES — do not certify as-is", "red")}[r["verdict"]]
    body = ""
    if r["errors"]:
        body += R.section("Arithmetic / contract errors — must resolve before certifying",
                          R.ul(r["errors"], cls="err"))
    if r["warns"]:
        body += R.section("Judgment flags — owner's-rep review", R.ul(r["warns"], cls="warn"))
    if not r["errors"] and not r["warns"]:
        body += R.section("Result", R.ul(["All line and summary figures reconcile within tolerance."]))
    pairs = [("Schedule-of-values total", f"${t['schedule_of_values']:,.2f}"),
             ("Contract sum (+ COs)", f"${t['contract_sum']:,.2f}"),
             ("Total completed & stored", f"${t['total_completed_and_stored']:,.2f}"),
             ("Retainage", f"${t['retainage']:,.2f}"),
             ("Total earned less retainage", f"${t['total_earned_less_retainage']:,.2f}"),
             ("Less previous payments", f"${t['previous_payments']:,.2f}"),
             ("CURRENT PAYMENT DUE", f"${t['current_payment_due']:,.2f}", "tot")]
    body += R.section("G702 summary (recomputed from the G703)", R.kv_rows(pairs))
    lrows = [[(li["item"], "nm"), f"${li['scheduled_value']:,.0f}", f"${li['completed_to_date']:,.0f}",
              f"{li['percent']*100:.0f}%", f"${li['balance_to_finish']:,.0f}"] for li in r["lines"]]
    body += R.section("Continuation sheet (G703)",
                      R.table([("Item", "l"), ("Scheduled", "r"), ("Completed", "r"),
                               ("%", "r"), ("Balance", "r")], lrows))
    kpis = [("Verdict", r["verdict"].split()[0]),
            ("Project complete", f"{r['project_percent_complete']*100:.1f}%"),
            ("Payment due", f"${t['current_payment_due']:,.0f}")]
    return R.page("GROUNDWORK · BUILD-WATCHER", "Pay Application Audit",
                  f"{draw.get('project','(project)')} · Application #{draw.get('application_no','?')}",
                  body, badge=badge, kpis=kpis,
                  foot="Recomputed independently from the line items. Confirm scope, lien waivers, "
                       "and stored-materials docs separately. Supports — does not replace — the "
                       "owner's-rep certification. " + R.DEFAULT_FOOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draw", help="path to a pay-application JSON")
    ap.add_argument("--file", help="path to a raw pay-application file (.xlsx/.csv/.pdf)")
    ap.add_argument("--sample", choices=list(SAMPLES))
    ap.add_argument("--retainage-pct", type=float, default=0.10,
                    help="retainage % for --file (from the G702; default 0.10)")
    ap.add_argument("--previous-payments", type=float, default=0.0,
                    help="prior certificates for --file (from the G702)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", help="write a styled HTML review doc to this path")
    a = ap.parse_args()
    if a.file:
        try:
            import gw_parser
        except ImportError:
            sys.exit("gw_parser.py must sit alongside gw_buildwatch.py to use --file.")
        if not Path(a.file).exists():
            sys.exit(f"Error: pay-application file not found: {a.file}")
        draw = gw_parser.parse_file(a.file, retainage_pct=a.retainage_pct,
                                    previous_payments=a.previous_payments)
        if not a.json:
            print(f"[ingested {len(draw['line_items'])} line items from {a.file} · "
                  f"contract sum ${draw['contract_sum']:,.0f}]\n")
    elif a.draw:
        try:
            draw = json.loads(Path(a.draw).read_text())
        except FileNotFoundError:
            sys.exit(f"Error: pay-application file not found: {a.draw}")
    elif a.sample:
        draw = SAMPLES[a.sample]()
    else:
        ap.error("give --file <pdf/xlsx/csv>, --draw <file.json>, or --sample {clean,flagged}")
    r = audit(draw)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(format_report(draw, r))
    if a.html:
        Path(a.html).write_text(render_html(draw, r), encoding="utf-8")
        print(f"\n[HTML review doc -> {a.html}]")
    sys.exit(1 if r["verdict"] == "DISCREPANCIES FOUND" else 0)


if __name__ == "__main__":
    main()
