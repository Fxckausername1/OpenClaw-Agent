#!/usr/bin/env python3
"""gw_intake.py — guided Deal-Intake wizard for the Groundwork suite.

Walks you through the facts of a deal in plain language, validates and cleans
every entry, fills anything you skip with deal_snapshot.py's own DEFAULTS (single
source of truth — imported, not copied), saves a timestamped JSON to deals/, and
then runs gw_packet.py to produce the combined review HTML for Rey.

Foolproof by design: prompts show the default in [brackets]; blank = take it.
Money accepts "$1,150,000" or "1150000"; rates accept "6.5" (=6.5%) or "0.065".
Pure stdlib — no install needed.

Usage:
  ./venv/bin/python gw_intake.py                 # full wizard -> JSON + packet
  ./venv/bin/python gw_intake.py --no-packet     # just build & save the JSON
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from deal_snapshot import DEFAULTS, underwrite

DEALS_DIR = Path("deals")


# ---------- prompt helpers (validate, clean, default) -----------------------
_EOF = object()


def _read(prompt):
    try:
        return input(prompt)
    except EOFError:           # end of piped input — distinct from a blank line
        return _EOF


def _clean_num(raw):
    s = raw.strip().replace(",", "").replace("$", "").replace("%", "").replace(" ", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    val = float(s)
    return -val if neg else val


def _fmt_default(d, pct):
    if d is None:
        return ""
    if pct:
        return f"{d*100:g}%"
    if isinstance(d, float) and d.is_integer():
        return f"{int(d):,}"
    if isinstance(d, (int, float)):
        return f"{d:,}" if d >= 1000 else f"{d:g}"
    return str(d)


def ask_num(label, default=None, kind=float, required=False, pct=False, hint=""):
    suffix = f" [{_fmt_default(default, pct)}]" if default is not None else ""
    h = f" ({hint})" if hint else ""
    while True:
        raw = _read(f"  {label}{h}{suffix}: ")
        if raw is _EOF:
            if default is not None:
                return default
            if required:
                sys.exit("\nInput ended before a required field was provided.")
            return None
        if not raw.strip():
            if default is not None:
                return default
            if not required:
                return None
            print("    → required, please enter a value.")
            continue
        try:
            v = _clean_num(raw)
        except ValueError:
            print("    → not a number, try again (e.g. 1150000 or $1,150,000).")
            continue
        if v is None:
            continue
        if pct and v > 1:           # "6.5" -> 0.065
            v = v / 100.0
        return int(round(v)) if kind is int else v


def ask_str(label, default=None, required=False):
    suffix = f" [{default}]" if default else ""
    while True:
        raw = _read(f"  {label}{suffix}: ")
        if raw is _EOF:
            if default is not None:
                return default
            if required:
                sys.exit("\nInput ended before a required field was provided.")
            return None
        raw = raw.strip()
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return None
        print("    → required.")


def ask_bool(label, default=True):
    d = "Y/n" if default else "y/N"
    raw = _read(f"  {label} [{d}]: ")
    if raw is _EOF:
        return default
    raw = raw.strip().lower()
    if not raw:
        return default
    return raw[0] == "y"


def section(title):
    print(f"\n\033[1m{title}\033[0m")


# ---------- the wizard ------------------------------------------------------
def run_wizard():
    print("=" * 64)
    print("  GROUNDWORK · DEAL INTAKE  — answer in plain terms, blank = default")
    print("=" * 64)
    deal = {}

    section("Property")
    deal["name"] = ask_str("Deal name", default="Untitled deal")
    deal["location"] = ask_str("Location / neighborhood", default="Atlanta")
    z = ask_str("Property ZIP (routes HUD market data)")
    if z:
        deal["zip"] = z

    section("The numbers (required)")
    deal["units"] = ask_num("Units", kind=int, required=True)
    deal["avg_rent_monthly"] = ask_num("Average rent / unit / month", required=True,
                                       hint="in-place or projected")
    deal["acquisition_price"] = ask_num("Acquisition / asking price", required=True)

    section("Scope & rehab")
    deal["rehab"] = ask_bool("Is this a value-add rehab?", default=True)
    if deal["rehab"]:
        total = ask_num("Total rehab budget", hint=f"blank = ${DEFAULTS['rehab_per_unit']:,.0f}/unit est")
        if total is not None:
            deal["rehab_total"] = total

    section("Financing & returns")
    deal["exit_cap_rate"] = ask_num("Exit cap rate", default=DEFAULTS["exit_cap_rate"], pct=True)
    rate = ask_num("Loan interest rate", default=DEFAULTS["loan"]["interest_rate"], pct=True)
    if rate != DEFAULTS["loan"]["interest_rate"]:
        deal.setdefault("loan", {})["interest_rate"] = rate
    sm = ask_num("Soft money (grants/HOME/CDBG)", default=DEFAULTS["soft_money"])
    if sm:
        deal["soft_money"] = sm
    tc = ask_num("Tax-credit equity (HTC/LIHTC)", default=DEFAULTS["tax_credit_equity"])
    if tc:
        deal["tax_credit_equity"] = tc

    section("Incentive & compliance context (optional)")
    sqft = ask_num("Building sq ft", hint="for CBEEO ≥25k flag")
    if sqft:
        deal["sqft"] = sqft
    yb = ask_num("Year built", kind=int, hint="pre-1936 → historic-credit flag")
    if yb:
        deal["year_built"] = yb
    if ask_bool("Certified/likely historic?", default=False):
        deal["historic"] = True
    deal["typical_bedrooms"] = ask_num("Typical unit bedrooms", default=2, kind=int)

    if ask_bool("\nCustomize advanced assumptions (vacancy, opex, loan terms)?", default=False):
        section("Advanced assumptions")
        deal["vacancy_rate"] = ask_num("Vacancy rate", default=DEFAULTS["vacancy_rate"], pct=True)
        deal["opex_ratio"] = ask_num("Operating-expense ratio", default=DEFAULTS["opex_ratio"], pct=True)
        deal["pm_fee_pct"] = ask_num("PM fee", default=DEFAULTS["pm_fee_pct"], pct=True)
        deal["soft_cost_pct"] = ask_num("Soft costs (% of rehab)", default=DEFAULTS["soft_cost_pct"], pct=True)
        deal["closing_pct"] = ask_num("Closing (% of acquisition)", default=DEFAULTS["closing_pct"], pct=True)
        deal["other_income_per_unit_monthly"] = ask_num(
            "Other income / unit / month", default=DEFAULTS["other_income_per_unit_monthly"])
        deal["reserves_per_unit_annual"] = ask_num(
            "Reserves / unit / year", default=DEFAULTS["reserves_per_unit_annual"])
        loan = deal.setdefault("loan", {})
        loan["ltc"] = ask_num("Max loan-to-cost", default=DEFAULTS["loan"]["ltc"], pct=True)
        loan["ltv"] = ask_num("Max loan-to-value", default=DEFAULTS["loan"]["ltv"], pct=True)
        loan["amort_years"] = ask_num("Amortization (years, 0 = interest-only)",
                                      default=DEFAULTS["loan"]["amort_years"], kind=int)
        loan["min_dscr"] = ask_num("Minimum DSCR", default=DEFAULTS["loan"]["min_dscr"])
    return deal


# ---------- save + run packet ----------------------------------------------
def _slug(deal):
    base = deal.get("name") or deal.get("location") or "deal"
    return (re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "deal")[:40]


def save_deal(deal):
    DEALS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = DEALS_DIR / f"{_slug(deal)}_{ts}.json"
    json_path.write_text(json.dumps(deal, indent=2), encoding="utf-8")
    return json_path, DEALS_DIR / f"{_slug(deal)}_{ts}.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-packet", action="store_true", help="save the JSON but don't build the packet")
    a = ap.parse_args()

    deal = run_wizard()

    # quick verdict preview so the user sees the result before files are written
    try:
        r = underwrite(deal)
        print(f"\n  Preview verdict: {r['verdict']}  ·  dev spread "
              f"{r['returns']['development_spread_bps']:.0f} bps  ·  equity gap "
              f"${r['sources']['equity_required']:,.0f}")
    except (KeyError, ZeroDivisionError) as e:
        sys.exit(f"\nCouldn't underwrite — missing/invalid field: {e}")

    if not ask_bool("\nSave this deal and build the packet?", default=True):
        sys.exit("Aborted — nothing written.")

    json_path, html_path = save_deal(deal)
    print(f"\n[deal saved -> {json_path}]")
    if a.no_packet:
        return

    import gw_packet
    html_path.write_text(gw_packet.build_packet(deal, gw_packet.PM_SAMPLE), encoding="utf-8")
    print(f"[deal packet -> {html_path}]")
    print(f"\n✓ Done. Open the packet for Rey:\n    {html_path}")


if __name__ == "__main__":
    main()
