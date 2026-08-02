"""options_confluence_tag.py -- INFORMATIONAL tagging only (2026-07-05, heff's ask:
"can we find confluence in the option strategy it uses based on the GEX pipeline?").

Attaches a compact read of the box's separate GEX/confluence pipeline
(live_gex_snapshot.json, confluence_score.json) to each options-tournament trade
record at entry time, for FUTURE analysis via options_confluence_outcome.py.

Deliberately NOT a filter, gate, size-reducer, or re-orderer of candidate
selection. Same "diagnostic, not a validated edge" posture already established
elsewhere in this codebase (confluence_score.py's audit notes: dark-pool/Ghost-Wall
agreement was only 56% across 133 real hits -- "a prompt to look closer, not proof
of anything"; advanced_gex.py's honesty flags; wall_proximity_alert.py's
next_move_read() framed as "a plain summary ... not a new signal or a trade call").
The options tournament has ~1 real trade ever -- nowhere near enough history to
validate a filter against. Gating live paper-capital allocation on an unproven
signal here would be a real regression against that discipline, not an improvement.
This module ONLY records; options_orchestrator.py must never branch on its output.

Usage:
    from options_confluence_tag import build_confluence_tag
    tag = build_confluence_tag(ticker, direction, signal)   # dict, never raises
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LIVE_GEX_PATH = DATA / "live_gex_snapshot.json"
CONFLUENCE_PATH = DATA / "confluence_score.json"

# Same order-of-magnitude staleness posture as catalyst_alert.py's STALE_HOURS (30) /
# wall_proximity_alert.py's "a wall that's a few hours stale is still the right level
# to act on" framing -- these snapshots refresh a few times/day, not every tick, so a
# multi-hour age is normal and NOT itself grounds to null the read out. Only an
# absent/unreadable file or an absent ticker row nulls the tag (see _safe_load below).
_NULL_TAG = {
    "gex_regime": None,
    "gex_regime_agrees": None,
    "wss_flag": None,
    "p_c": None,
    "confluence_lean": None,
    "confluence_agrees": None,
    "n_bull": None,
    "n_bear": None,
    "n_total": None,
}


def _safe_load(path):
    """Fail-open: missing file / malformed JSON -> None. Never raises."""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _find_gex_row(ticker):
    doc = _safe_load(LIVE_GEX_PATH)
    if not doc:
        return None
    for r in doc.get("results", []):
        if r.get("ticker") == ticker and not r.get("error"):
            return r
    return None


def _find_confluence_row(ticker):
    doc = _safe_load(CONFLUENCE_PATH)
    if not doc:
        return None
    for r in doc.get("rows", []):
        if r.get("ticker") == ticker:
            return r
    return None


def _gex_regime_agrees(regime, direction, signal):
    """Direction-aware agreement, the subtle part -- get the signed convention right.

    ORB is a momentum/continuation signal: it's betting the move CONTINUES in
    `direction`. A NEGATIVE gamma regime means dealers are short gamma and must
    hedge WITH price (buy as it rises, sell as it falls), which AMPLIFIES moves --
    that mechanically agrees with a continuation bet, regardless of which way
    `direction` points. A POSITIVE gamma regime means dealers hedge AGAINST price
    (mean-reverting hedging flow), which DAMPENS moves -- that disagrees with
    continuation. So for ORB, regime agreement does NOT depend on the sign of
    `direction` at all, only on the regime's own amplify/dampen character.

    MR is a mean-reversion signal: it's betting price snaps back, i.e. betting on
    exactly the dampening behavior a POSITIVE gamma regime produces. So for MR the
    agreement mapping is the mirror image of ORB's: POS agrees, NEG disagrees.

    This mirrors the signed-convention discipline already documented for Flip/
    Regime/Net-GEX elsewhere in this pipeline (dashboard's regime-sign comments,
    confluence_score.py's Ghost Wall / put-wall-bearish / call-wall-bullish
    convention) -- get the sign meaning right and write down WHY, don't just wire
    a field through.
    """
    if regime not in ("positive", "negative"):
        return None
    if signal == "ORB":
        return regime == "negative"
    if signal == "MR":
        return regime == "positive"
    return None


def build_confluence_tag(ticker, direction, signal):
    """(ticker, direction in {1,-1}, signal in {"ORB","MR"}) -> compact tag dict.

    Fail-open on ANY missing file / malformed JSON / missing ticker row: returns
    the all-null tag, never raises, never blocks the caller. Read-only, $0 cost,
    no network calls -- both source files are already-written local snapshots.
    """
    ticker = (ticker or "").upper()
    tag = dict(_NULL_TAG)
    try:
        gex_row = _find_gex_row(ticker)
        if gex_row is not None:
            regime = gex_row.get("regime")
            tag["gex_regime"] = regime
            tag["gex_regime_agrees"] = _gex_regime_agrees(regime, direction, signal)
            tag["wss_flag"] = gex_row.get("wss_flag")
            tag["p_c"] = gex_row.get("p_c")

        conf_row = _find_confluence_row(ticker)
        # Same "don't fabricate a lean when there isn't one" honesty as
        # confluence_score.py / wall_proximity_alert.py's null-safe reads: a row
        # with n_total == 0 (no signals fired either way) has nothing to compare
        # against direction, so leave confluence_agrees null rather than calling
        # it a false "disagree".
        if conf_row is not None and conf_row.get("n_total"):
            lean = conf_row.get("lean")
            tag["confluence_lean"] = lean
            tag["n_bull"] = conf_row.get("n_bull")
            tag["n_bear"] = conf_row.get("n_bear")
            tag["n_total"] = conf_row.get("n_total")
            if lean == "BULLISH":
                tag["confluence_agrees"] = (direction == 1)
            elif lean == "BEARISH":
                tag["confluence_agrees"] = (direction == -1)
            # any other lean value (e.g. "NEUTRAL") -> confluence_agrees stays None
    except Exception:
        # Belt-and-suspenders: any unexpected shape/bug in the source files must
        # never propagate into the orchestrator's hot entry path. Fail open.
        return dict(_NULL_TAG)
    return tag
