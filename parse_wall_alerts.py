import re
import json
from pathlib import Path

LOG = Path("logs/wall_proximity_alert.log")
text = LOG.read_text()

# Split into per-tick blocks, each starting with "WALL ALERT (...):"
blocks = re.split(r"(?=WALL ALERT \()", text)

alert_re = re.compile(
    r"^([A-Z.]+): \$([\d.]+) within ([\d.]+)% of (put|call) wall \$([\d.]+) \((positive|negative)\)\n"
    r"\s+(wall holding|wall cracking)[^(]*\(([A-Z]+) ([+-]?\d+), P\(C\) (\d+)%\)[^\n]*\n"
    r"\s+Confluence: (.+)",
    re.MULTILINE,
)

ts_re = re.compile(r"WALL ALERT \((\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ET\):")

events = []
for block in blocks:
    ts_m = ts_re.search(block)
    if not ts_m:
        continue
    ts = ts_m.group(1)
    for m in alert_re.finditer(block):
        ticker, price, dist_pct, wall_type, wall_level, regime, verdict, wss_flag, wss_score, p_c, confluence = m.groups()
        events.append({
            "ts": ts, "ticker": ticker, "price": float(price), "dist_pct": float(dist_pct),
            "wall_type": wall_type, "wall_level": float(wall_level), "regime": regime,
            "verdict": "holding" if verdict == "wall holding" else "cracking",
            "wss_flag": wss_flag, "wss_score": int(wss_score), "p_c": int(p_c),
            "confluence": confluence.strip(),
        })

print(f"total alert events parsed: {len(events)}")
print(f"unique tickers: {len(set(e['ticker'] for e in events))}")
print(f"unique tick-times: {len(set(e['ts'] for e in events))}")

out = Path("data/wall_alert_events_parsed.json")
out.write_text(json.dumps(events, indent=2))
print(f"wrote {out}")
