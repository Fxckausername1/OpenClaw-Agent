---
name: deal-snapshot
description: Run a Groundwork Deal Snapshot — underwrite a small multifamily / value-add deal from a pasted deal file (JSON) or the intake fields, and return the verdict + return profile + funding gap. Use whenever the user pastes a Groundwork deal or says "run a deal snapshot."
user-invocable: true
---

# Deal Snapshot (Groundwork)

Underwrite a small value-add multifamily deal and return the result. Triggered when the
user pastes a deal (the JSON from the Groundwork outreach intake form) or gives the intake
fields in plain text.

## Steps
1. Assemble the deal JSON.
   - If the user pasted JSON, use it verbatim.
   - If plain text, map to keys. REQUIRED: `units`, `avg_rent_monthly` (renovated rent/unit/mo),
     `acquisition_price`, `rehab_per_unit`. OPTIONAL: `name`, `location`, `property_type`
     (multifamily|mixed-use|commercial), `sqft`, `year_built`, `historic` (true),
     `soft_money`, `tax_credit_equity`, `exit_cap_rate` (decimal, e.g. 0.065).
   - If any REQUIRED field is missing, ASK for it — never invent numbers.
2. Write the JSON to a temp file (do NOT put deal values in the shell command itself):
   write the JSON content to `/tmp/gw_deal.json`.
3. Run the deterministic engine:
   `cd /home/heff/.openclaw/workspace && ./venv/bin/python deal_snapshot.py --deal /tmp/gw_deal.json --html /tmp/gw_snapshot.html`
   - Add `--lens partner` (Rey is co-principal → ranks/leads by development spread) or
     `--lens flipper` (advising an investor → return-on-equity) if the user specifies the seat.
4. Reply with: the VERDICT line, the full RETURN PROFILE block, and the equity/gap. Note the
   styled one-pager is saved at `/tmp/gw_snapshot.html`.

## Rules
- Every figure is an ESTIMATE — say so. Rey verifies before anything is client-facing.
- The engine is deterministic. Report its numbers exactly; do not adjust or "improve" them.
- This is internal. Never reference Rey's day-job employer.
