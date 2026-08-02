import json
from pathlib import Path
from collections import defaultdict

scored = json.loads(Path("data/wall_alert_scored.json").read_text())
print(f"n={len(scored)} scored events\n")

# 1. Does |wss_score| magnitude correlate with accuracy? (tests whether a buffer/dead-zone
#    near 0 would help, i.e. is the current exact-zero cutoff too eager to commit)
print("--- accuracy by |wss_score| magnitude bucket ---")
buckets = defaultdict(list)
for r in scored:
    mag = abs(r["wss_score"])
    if mag < 3:
        k = "0-2 (near the cutoff)"
    elif mag < 6:
        k = "3-5"
    elif mag < 10:
        k = "6-9"
    else:
        k = "10+"
    buckets[k].append(r)
for k in ["0-2 (near the cutoff)", "3-5", "6-9", "10+"]:
    rs = buckets.get(k, [])
    if not rs:
        continue
    acc = sum(1 for r in rs if r["correct"]) / len(rs)
    print(f"  {k:25s} n={len(rs):3d}  accuracy={acc:.1%}")

# 2. Threshold sweep: what if we required |wss_score| >= T to issue ANY confident verdict,
#    treating anything inside the buffer as "no strong lean" (excluded from scoring)?
#    Does raising T improve accuracy on what's left, and how much coverage do we lose?
print("\n--- buffer-threshold sweep (exclude |wss_score| < T, score the rest) ---")
for T in [0, 2, 4, 6, 8, 10, 12]:
    kept = [r for r in scored if abs(r["wss_score"]) >= T]
    if not kept:
        print(f"  T={T:3d}  n=0 (nothing left)")
        continue
    acc = sum(1 for r in kept if r["correct"]) / len(kept)
    coverage = len(kept) / len(scored)
    print(f"  T={T:3d}  n={len(kept):3d}  coverage={coverage:.0%}  accuracy={acc:.1%}")

# 3. Does a P(C) floor as an ADDITIONAL filter (currently unused in the verdict at all)
#    improve precision specifically on "cracking" calls (the higher-stakes, rarer verdict)?
print("\n--- P(C) floor as an additional filter on 'cracking' calls only ---")
cracking = [r for r in scored if r["verdict"] == "cracking"]
for pc_min in [0, 10, 20, 30]:
    kept = [r for r in cracking if r["p_c"] >= pc_min]
    if not kept:
        continue
    acc = sum(1 for r in kept if r["correct"]) / len(kept)
    print(f"  P(C)>={pc_min:3d}%  n={len(kept):3d}  accuracy={acc:.1%}")

# 4. Same buffer-threshold sweep but split by verdict type, since 'holding' (n=52, the
#    underperforming majority) and 'cracking' (n=29) may need different fixes.
print("\n--- buffer-threshold sweep, split by verdict ---")
for verdict in ["holding", "cracking"]:
    subset = [r for r in scored if r["verdict"] == verdict]
    print(f" verdict={verdict}")
    for T in [0, 3, 6, 9, 12]:
        kept = [r for r in subset if abs(r["wss_score"]) >= T]
        if not kept:
            print(f"    T={T:3d}  n=0")
            continue
        acc = sum(1 for r in kept if r["correct"]) / len(kept)
        print(f"    T={T:3d}  n={len(kept):3d}  accuracy={acc:.1%}")
