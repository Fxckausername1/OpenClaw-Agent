#!/usr/bin/env python3
"""Idempotently install the sealed premarket collector's DST-paired cron entries."""

import argparse
import subprocess


MARKER = "premarket_forward_collector_wrapper.sh"
LINES = [
    "15 13,14 * * 1-5 /home/heff/.openclaw/workspace/scripts/premarket_forward_collector_wrapper.sh capture  # sealed 09:15 ET IEX capture; wrapper ET-gates DST pair",
    "30 20,21 * * 1-5 /home/heff/.openclaw/workspace/scripts/premarket_forward_collector_wrapper.sh sip-backfill  # sealed 16:30 ET SIP audit backfill; wrapper ET-gates DST pair",
]


def updated_crontab(current: str) -> str:
    kept = [line for line in current.splitlines() if MARKER not in line]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join([*kept, *LINES]) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        current = subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        current = ""
    result = updated_crontab(current)
    if args.dry_run:
        print("\n".join(LINES))
        return
    subprocess.run(["crontab", "-"], input=result, text=True, check=True)
    print("installed 2 sealed premarket collector entries")


if __name__ == "__main__":
    main()
