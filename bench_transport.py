"""READ-ONLY latency benchmark: bare `requests` module (a fresh TLS handshake
per call, which is what smc/broker.py does today) vs a pooled, pre-warmed
requests.Session. Uses GET /v2/clock only -- no order is placed."""
import statistics as st
import time

import requests
from requests.adapters import HTTPAdapter

import options_orchestrator as oo
from smc.paper_guard import enforce_paper_mode

enforce_paper_mode(oo.PAPER, (oo.H or {}).get("APCA-API-KEY-ID"), exit_on_violation=False)
URL = oo.PAPER + "/v2/clock"
N = 12


def timeit(fn):
    out = []
    for _ in range(N):
        t0 = time.perf_counter()
        r = fn()
        r.raise_for_status()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


cold = timeit(lambda: requests.get(URL, headers=oo.H, timeout=20))

s = requests.Session()
s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))
s.headers.update(oo.H)
s.get(URL, timeout=20)          # pre-warm: pay the handshake once, up front
warm = timeit(lambda: s.get(URL, timeout=20))


def show(label, xs):
    xs_sorted = sorted(xs)
    p95 = xs_sorted[int(0.95 * (len(xs_sorted) - 1))]
    print(f"{label:<34} median {st.median(xs):7.1f} ms   p95 {p95:7.1f} ms   "
          f"min {min(xs):6.1f}  max {max(xs):6.1f}")


show("bare requests (handshake/call)", cold)
show("pooled+prewarmed Session", warm)
print(f"\nmedian saved per call: {st.median(cold) - st.median(warm):.1f} ms")
