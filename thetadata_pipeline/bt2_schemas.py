"""BT-2 canonical schemas -- BOT_NEXUS_Options_Strategy_Backtesting_Roadmap.pdf
Section 4, narrowed/extended per BT0_CHARTER.md Section 3 (frozen 2026-07-26).

Two record shapes: a strategy specification (what a backtest run commits to
before seeing results) and a simulated trade ledger row (one row per
signal -- PASS-gated trades AND WAIT/FAIL no-fill outcomes alike, per the
roadmap's explicit "no signal is a discarded outcome" rule -- BT-0 Section
2's "what fraction of signals produce a contract that even passes the
viability gate" question depends on WAIT/FAIL rows staying in the ledger,
not being dropped).

Pure schema/builder functions only, no I/O -- mirrors bt1_manifest.py's own
split (schema+grading logic here, orchestration in bt2_simulator.py).

entry_quote_ts/entry_bid/entry_ask/entry_fill/quantity and their exit_*
counterparts are ARRAYS from the start (roadmap Section 4's own explicit
instruction, "to support future partial/scaled entries even though BT-2's
first pass only needs one entry and one exit") -- v1 only ever populates a
single element, but the shape never needs a future migration to support
partial fills.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "bt2-1.0"

# BT0_CHARTER.md Section 3's restated strategy-spec field set: replaces the
# roadmap's own direction_rules/brief_gate/dashboard_gate/indicator_gate/
# option_selector with a single direction_gate (this codebase's real
# control_map_verdict) plus contract_gate (this codebase's real CB-V4
# Section 8 PASS/WAIT/FAIL vocabulary) -- both are BT-0's real additions
# tying the frozen roadmap schema to code that actually exists today.
# option_selector's role is carried by parameter_set_id -> a SelectorConfig
# lookup, not a separate top-level field.
STRATEGY_SPEC_FIELDS = (
    "strategy_id", "version", "symbols", "session_window", "direction_gate",
    "contract_gate", "fill_model", "position_size", "exit_rules",
    "event_blackouts", "parameter_set_id", "created_before_test_date",
)

BT0_ALLOWED_SYMBOLS = ("SPY", "QQQ")

# Roadmap Section 4's own trade-ledger field list verbatim, plus BT-0's
# contract_gate addition (CB-V4 Section 8 vocabulary -- only PASS trades
# enter the "traded" ledger as a real fill attempt; WAIT/FAIL are logged as
# no-fill outcomes here too, never discarded).
TRADE_LEDGER_FIELDS = (
    "trade_id", "session", "symbol", "signal_ts", "decision_ts", "contract_id",
    "entry_quote_ts", "entry_bid", "entry_ask", "entry_fill", "quantity",
    "context_snapshot_id", "contract_gate", "target", "stop", "invalidation",
    "exit_ts", "exit_bid", "exit_ask", "exit_fill", "exit_reason",
    "gross_pnl", "fees", "slippage", "net_pnl", "mae", "mfe",
    "rule_flags", "data_quality", "experiment_id",
)

DIRECTION_CALL_WATCH = "CALL WATCH"
DIRECTION_PUT_WATCH = "PUT WATCH"
VALID_DIRECTION_GATES = (DIRECTION_CALL_WATCH, DIRECTION_PUT_WATCH)
DIRECTION_TO_RIGHT = {DIRECTION_CALL_WATCH: "C", DIRECTION_PUT_WATCH: "P"}

GATE_PASS, GATE_WAIT, GATE_FAIL = "PASS", "WAIT", "FAIL"
VALID_GATES = (GATE_PASS, GATE_WAIT, GATE_FAIL)


def build_strategy_spec(
    *, strategy_id: str, version: str, symbols: tuple, session_window: tuple,
    direction_gate: str, contract_gate: str, fill_model: str, position_size: int,
    exit_rules: dict, event_blackouts: list, parameter_set_id: str,
    created_before_test_date: str,
) -> dict:
    """BT-0 Section 3: symbols narrowed to SPY/QQQ only for v1, direction_gate
    restricted to the two real signal states (TWO-SIDED/WAIT/NO TRADE all
    mean "no signal," never a coin-flip direction -- callers must filter
    those out before ever constructing a spec, not represent them here)."""
    for s in symbols:
        if s not in BT0_ALLOWED_SYMBOLS:
            raise ValueError(f"BT-0 v1 narrows symbols to SPY/QQQ only, got {s!r}")
    if direction_gate not in VALID_DIRECTION_GATES:
        raise ValueError(f"direction_gate must be one of {VALID_DIRECTION_GATES}, got {direction_gate!r}")
    return {
        "strategy_id": strategy_id, "version": version, "symbols": list(symbols),
        "session_window": list(session_window), "direction_gate": direction_gate,
        "contract_gate": contract_gate, "fill_model": fill_model, "position_size": position_size,
        "exit_rules": exit_rules, "event_blackouts": list(event_blackouts),
        "parameter_set_id": parameter_set_id, "created_before_test_date": created_before_test_date,
        "schema_version": SCHEMA_VERSION,
    }


def as_fill_array(value: Any) -> list:
    """Wraps a single scalar fill observation into the one-element ARRAY
    shape every entry_*/exit_* ledger field requires from the start
    (roadmap Section 4). None becomes an empty array (no fill happened),
    never a list containing None -- downstream sum()/mean() callers should
    never need a None-filter."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def build_ledger_row(**fields) -> dict:
    """Assembles one ledger row. Every TRADE_LEDGER_FIELDS name must be
    supplied by the caller (bt2_simulator.py) -- this function does not
    default missing fields to None, since a silently-missing field is
    exactly the kind of "discarded outcome" bug this schema exists to
    prevent. Extra keys beyond the frozen field set (e.g. `friction_detail`)
    are allowed through untouched -- same convention as bt1_manifest.py's
    own manifest rows, which add quality_grade/quality_reasons on top of a
    minimum required set rather than treating the schema as a strict
    maximum."""
    missing = [f for f in TRADE_LEDGER_FIELDS if f not in fields]
    if missing:
        raise ValueError(f"trade ledger row missing required fields: {missing}")
    row = dict(fields)
    row["schema_version"] = SCHEMA_VERSION
    return row


def validate_ledger_row(row: dict) -> list:
    """Returns a list of problems (empty = clean). Checked at write time by
    bt2_simulator's ledger writer, and directly by tests -- never silently
    accepts a malformed row."""
    problems = []
    for field in TRADE_LEDGER_FIELDS:
        if field not in row:
            problems.append(f"missing field: {field}")
    array_fields = (
        "entry_quote_ts", "entry_bid", "entry_ask", "entry_fill", "quantity",
        "exit_bid", "exit_ask", "exit_fill",
    )
    for field in array_fields:
        if field in row and not isinstance(row[field], list):
            problems.append(f"field {field} must be an array (Section 4's array-of-fills rule), got {type(row[field]).__name__}")
    if row.get("contract_gate") not in VALID_GATES:
        problems.append(f"contract_gate must be one of {VALID_GATES}, got {row.get('contract_gate')!r}")
    return problems
