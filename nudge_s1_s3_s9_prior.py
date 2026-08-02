#!/usr/bin/env python3
"""
One-off script: nudge S1/S3/S9's Thompson-sampler beta_param prior in tournament_state to
reflect their CONFIRMED 6/30 forensics loss history, pre-dating the 7/2 uniform reset.
Leaves alpha_param=1.0 unchanged for all rows; touches ONLY beta_param for S1/S3/S9.

beta_param=3.0 chosen: with alpha=1.0, expected win rate = alpha/(alpha+beta) = 1/4 = 25%,
a moderate pessimistic prior (vs. the uniform 50%) that still yields to real data fast --
effective prior "sample size" is alpha+beta-2 = 2 pseudo-observations, so it takes only a
handful of actual live wins to pull the posterior mean back up if 6/30's pattern doesn't
repeat. Not so extreme (e.g. beta=9+ -> ~10% implied win rate) that it would take dozens of
real wins to recover -- the whole point of Thompson sampling is to keep it recoverable.

Run: ./venv/bin/python nudge_s1_s3_s9_prior.py            # dry-run, prints before/after, no write
     ./venv/bin/python nudge_s1_s3_s9_prior.py --commit    # actually writes
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "options_eval.db"
TARGETS = ("S1", "S3", "S9")
NEW_BETA = 3.0


def dump(conn):
    cur = conn.execute(
        "SELECT strategy_id, status, alpha_param, beta_param, trade_count, dsr_score, "
        "psr_score, last_eval_time FROM tournament_state ORDER BY strategy_id"
    )
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return cols, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    a = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    cols, before = dump(conn)
    print("BEFORE:")
    for r in before:
        print(" ", dict(zip(cols, r)))

    if a.commit:
        for sid in TARGETS:
            conn.execute(
                "UPDATE tournament_state SET beta_param = ? WHERE strategy_id = ?",
                (NEW_BETA, sid),
            )
        conn.commit()

    _, after = dump(conn)
    print("AFTER:" if a.commit else "AFTER (dry-run, unchanged):")
    for r in after:
        print(" ", dict(zip(cols, r)))
    conn.close()

    if a.commit:
        # verify: only beta_param on S1/S3/S9 changed, everything else byte-identical
        changed = []
        for b, af in zip(before, after):
            bd, ad = dict(zip(cols, b)), dict(zip(cols, af))
            diffs = {k: (bd[k], ad[k]) for k in cols if bd[k] != ad[k]}
            if diffs:
                changed.append((bd["strategy_id"], diffs))
        print("\nROWS CHANGED:")
        for sid, diffs in changed:
            print(f"  {sid}: {diffs}")
        unexpected = [sid for sid, diffs in changed
                      if sid not in TARGETS or set(diffs) != {"beta_param"}]
        if unexpected:
            print(f"\n!! UNEXPECTED CHANGES: {unexpected}")
            sys.exit(1)
        if {sid for sid, _ in changed} != set(TARGETS):
            print(f"\n!! Expected exactly {TARGETS} to change, got {[sid for sid, _ in changed]}")
            sys.exit(1)
        print("\nOK: only S1/S3/S9 beta_param changed; all other fields/rows untouched.")


if __name__ == "__main__":
    main()
