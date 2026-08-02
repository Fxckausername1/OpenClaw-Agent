import unusualwhales_client as uw

cases = [
    ("XLU", "2026-07-31", 42.3, 45.75),
    ("XLRE", "2026-07-17", 42.83, 44.69),
]

for ticker, our_exp, our_flip, our_spot in cases:
    print(f"=== {ticker} (our box used expiry {our_exp}, our flip={our_flip}, spot={our_spot}) ===")
    code, body = uw._get(f"/api/stock/{ticker}/greek-exposure/expiry")
    rows = body["data"]
    total_call, total_put = 0.0, 0.0
    near_dated_net = None
    for row in rows:
        cg, pg = float(row["call_gex"]), float(row["put_gex"])
        total_call += cg
        total_put += pg
        net = cg + pg
        flag = "  <-- our box's expiration" if row["expiry"] == our_exp else ""
        print(f"  expiry={row['expiry']:12} dte={row['dte']:4} call_gex={cg:>14,.0f} put_gex={pg:>14,.0f} net={net:>14,.0f}{flag}")
        if row["expiry"] == our_exp:
            near_dated_net = net
    full_agg = total_call + total_put
    print(f"  FULL TERM-STRUCTURE AGGREGATE: call={total_call:,.0f} put={total_put:,.0f} net={full_agg:,.0f} -> {'POSITIVE' if full_agg > 0 else 'NEGATIVE'}")
    if near_dated_net is not None:
        print(f"  NEAR-DATED SLICE ONLY ({our_exp}): net={near_dated_net:,.0f} -> {'POSITIVE' if near_dated_net > 0 else 'NEGATIVE'}")
    else:
        print(f"  NEAR-DATED SLICE ({our_exp}): not present in UW's per-expiry data")
    print()
