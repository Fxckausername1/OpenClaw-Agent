"""BT-1 pilot manifest schema + quality grading -- BOT_NEXUS_Options_Strategy_
Backtesting_Roadmap.pdf Section 3 / heff's BT-1 spec (2026-07-26).

Pure functions only, no I/O, no network -- mirrors this package's existing
split (features.py/aggregate.py compute, snapshot.py/collector.py do I/O).
This module answers one question per session/symbol: "was this pull
trustworthy enough to build on," not "is the underlying market interesting."
A session grades FAIL/PARTIAL exactly as often as the data genuinely
deserves it -- there is no bias here toward passing a pilot to declare BT-1
done, since the roadmap explicitly says not to begin BT-2 on an ungraded
pull.
"""

from __future__ import annotations

from typing import Optional

SCHEMA_VERSION = "bt1-pilot-1.0"

GRADE_PASS = "PASS"
GRADE_PARTIAL = "PARTIAL"
GRADE_FAIL = "FAIL"

# Initial research thresholds, not calibrated against any outcome data --
# same honesty convention as features.py's SMILE_JUMP_THRESHOLD. Revisit
# once real pilot data shows whether these are too strict/loose.
MIN_TRADE_ROWS_FOR_PASS = 50           # a session with fewer real trades than this can't ground a fill-simulator study
MAX_EMPTY_CORE_CHUNKS_FOR_PASS = 1     # empty 15-min windows during 10:00-15:45 ET (not the thin open/close)
MAX_EMPTY_CORE_CHUNKS_FOR_PARTIAL = 4
MAX_CROSSED_MARKET_FRACTION_FOR_PASS = 0.02   # >2% crossed/locked quotes suggests a real feed problem, not normal noise
MIN_CLASSIFICATION_COVERAGE_FOR_PASS = 0.50   # directional (ASK/BID) share of all classified trades
MIN_CLASSIFICATION_COVERAGE_FOR_PARTIAL = 0.25
MIN_BAR_COVERAGE_FOR_PASS = 0.90        # underlying 1-min bars received / expected minutes in the session
MIN_BAR_COVERAGE_FOR_PARTIAL = 0.60


def core_hour_chunks(chunk_labels: list[str], core_start: str = "10:00", core_end: str = "15:45") -> list[str]:
    """Filters chunk labels (e.g. "09:30-09:45") down to ones that start
    within the core session -- the thin first/last 30-45min windows on a
    0DTE chain routinely have real gaps for far-OTM strikes and shouldn't be
    penalized the same as a gap at 11:00."""
    out = []
    for label in chunk_labels:
        start = label.split("-")[0]
        if core_start <= start < core_end:
            out.append(label)
    return out


def build_session_manifest(
    *,
    symbol: str,
    date: str,
    calendar: dict,
    requested: dict,
    received: dict,
    missing: dict,
    rejected: dict,
    retrieval: dict,
    response_metadata: dict,
    integrity: dict,
) -> dict:
    """Assembles one session's manifest record and grades it. All inputs are
    plain dicts the orchestration layer (bt1_pilot.py) already has on hand
    after a pull -- this function only ever reads them, never re-derives
    anything from raw data itself (that would duplicate normalize.py's own
    logic and risk the two disagreeing)."""
    grade, reasons = grade_session(received=received, missing=missing, rejected=rejected, integrity=integrity)
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "date": date,
        "calendar": calendar,
        "requested": requested,
        "received": received,
        "missing": missing,
        "rejected": rejected,
        "retrieval": retrieval,
        "response_metadata": response_metadata,
        "integrity": integrity,
        "quality_grade": grade,
        "quality_reasons": reasons,
    }


def grade_session(*, received: dict, missing: dict, rejected: dict, integrity: dict) -> tuple[str, list[str]]:
    """(grade, reasons) -- reasons is always populated for PARTIAL/FAIL (never
    silently downgraded without saying why) and empty for a clean PASS."""
    reasons: list[str] = []

    trade_rows = received.get("option_trade_quote_rows", 0) or 0
    bars_count = received.get("underlying_bars_count", 0) or 0
    expected_bars = received.get("underlying_bars_expected") or 0
    bar_coverage = (bars_count / expected_bars) if expected_bars else None

    empty_core_chunks = len(missing.get("empty_trade_quote_core_chunks", []))
    crossed_fraction = integrity.get("crossed_market_fraction")
    coverage = integrity.get("trade_classification_coverage")
    thetadata_errors = response_metadata_errors(missing)

    # --- Hard failures: the pull is not usable as a foundation for anything ---
    if trade_rows == 0:
        reasons.append("zero option trade/quote rows received for this session")
    if bars_count == 0:
        reasons.append("zero underlying bars received for this session")
    if missing.get("unrecoverable_errors"):
        reasons.append(f"unrecoverable pull errors: {missing['unrecoverable_errors']}")
    if crossed_fraction is not None and crossed_fraction > MAX_CROSSED_MARKET_FRACTION_FOR_PASS * 5:
        reasons.append(f"crossed/locked quote fraction {crossed_fraction:.2%} far exceeds a sane bound")
    if reasons:
        return GRADE_FAIL, reasons

    # --- Degraded but usable-with-caveats ---
    partial_reasons: list[str] = []
    if trade_rows < MIN_TRADE_ROWS_FOR_PASS:
        partial_reasons.append(f"only {trade_rows} trade rows (floor {MIN_TRADE_ROWS_FOR_PASS})")
    if empty_core_chunks > MAX_EMPTY_CORE_CHUNKS_FOR_PASS:
        if empty_core_chunks > MAX_EMPTY_CORE_CHUNKS_FOR_PARTIAL:
            reasons.append(f"{empty_core_chunks} empty core-hour trade/quote chunks (fail floor {MAX_EMPTY_CORE_CHUNKS_FOR_PARTIAL})")
        else:
            partial_reasons.append(f"{empty_core_chunks} empty core-hour trade/quote chunks (pass ceiling {MAX_EMPTY_CORE_CHUNKS_FOR_PASS})")
    if coverage is not None and coverage < MIN_CLASSIFICATION_COVERAGE_FOR_PASS:
        if coverage < MIN_CLASSIFICATION_COVERAGE_FOR_PARTIAL:
            reasons.append(f"directional classification coverage {coverage:.2%} below fail floor {MIN_CLASSIFICATION_COVERAGE_FOR_PARTIAL:.0%}")
        else:
            partial_reasons.append(f"directional classification coverage {coverage:.2%} below pass floor {MIN_CLASSIFICATION_COVERAGE_FOR_PASS:.0%}")
    if bar_coverage is not None and bar_coverage < MIN_BAR_COVERAGE_FOR_PASS:
        if bar_coverage < MIN_BAR_COVERAGE_FOR_PARTIAL:
            reasons.append(f"underlying bar coverage {bar_coverage:.2%} below fail floor {MIN_BAR_COVERAGE_FOR_PARTIAL:.0%}")
        else:
            partial_reasons.append(f"underlying bar coverage {bar_coverage:.2%} below pass floor {MIN_BAR_COVERAGE_FOR_PASS:.0%}")
    if crossed_fraction is not None and crossed_fraction > MAX_CROSSED_MARKET_FRACTION_FOR_PASS:
        partial_reasons.append(f"crossed/locked quote fraction {crossed_fraction:.2%} above pass ceiling {MAX_CROSSED_MARKET_FRACTION_FOR_PASS:.0%}")
    if received.get("open_interest_contracts", 0) == 0:
        partial_reasons.append("no open-interest contracts received")
    if received.get("option_greeks_rows", 0) == 0:
        partial_reasons.append("no point-in-time Greeks rows received")

    if reasons:
        return GRADE_FAIL, reasons
    if partial_reasons:
        return GRADE_PARTIAL, partial_reasons
    return GRADE_PASS, []


def response_metadata_errors(missing: dict) -> int:
    return len(missing.get("unrecoverable_errors") or [])


def overall_summary(session_manifests: list[dict]) -> dict:
    counts = {GRADE_PASS: 0, GRADE_PARTIAL: 0, GRADE_FAIL: 0}
    for row in session_manifests:
        counts[row["quality_grade"]] = counts.get(row["quality_grade"], 0) + 1
    return {
        "sessions_total": len(session_manifests),
        "sessions_pass": counts[GRADE_PASS],
        "sessions_partial": counts[GRADE_PARTIAL],
        "sessions_fail": counts[GRADE_FAIL],
        "usable_for_bt2": counts[GRADE_FAIL] == 0 and counts[GRADE_PASS] > 0,
    }
