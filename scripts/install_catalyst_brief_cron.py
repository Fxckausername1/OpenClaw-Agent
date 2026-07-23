#!/usr/bin/env python3
"""Install idempotent DST-safe Catalyst Brief cron entries."""

import subprocess

LINES = [
    "36 12,13 * * 1-5 /home/heff/.openclaw/workspace/scripts/catalyst_news_refresh_wrapper.sh  # independent free catalyst refresh, ET-gated to 08:36",
    "52 13,14 * * 1-5 /home/heff/.openclaw/workspace/scripts/catalyst_brief_wrapper.sh build  # build/publish 09:55 ET Catalyst Brief; ET-gated",
    "6 14,15 * * 1-5 /home/heff/.openclaw/workspace/scripts/catalyst_brief_wrapper.sh hinge  # 10:06 ET hinge status; ET-gated",
    "10 20,21 * * 1-5 /home/heff/.openclaw/workspace/scripts/catalyst_brief_wrapper.sh score  # 16:10 ET post-close score; ET-gated",
]


def main():
    current = subprocess.run(["crontab", "-l"], text=True, capture_output=True).stdout.splitlines()
    kept = [line for line in current if "catalyst_brief_wrapper.sh" not in line and "catalyst_news_refresh_wrapper.sh" not in line]
    updated = "\n".join(kept + LINES).rstrip() + "\n"
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    print("installed Catalyst Brief cron entries:")
    for line in LINES:
        print(line)


if __name__ == "__main__":
    main()
