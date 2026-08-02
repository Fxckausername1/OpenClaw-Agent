import json

rows = [json.loads(l) for l in open("data/wall_alert_ledger.jsonl") if l.strip()]
hi = [r for r in rows if r["wss_score"] > 12]
hi.sort(key=lambda r: -r["wss_score"])
print(f"{len(hi)} alert(s) with wss_score > 12 out of {len(rows)} total scored")
print()
for r in hi:
    print(f"{r['ticker']:6s} {r['date']} {r['time']} ET  wss_score={r['wss_score']:+.0f}  "
          f"{r['wall_type']} wall ${r['wall_level']:.2f}  verdict={r['verdict']:9s} "
          f"actual={r['actual']:7s} correct={r['correct']}")
