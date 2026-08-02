import unittest

from thetadata_pipeline.bt2_schemas import (
    GATE_FAIL, GATE_PASS, TRADE_LEDGER_FIELDS, as_fill_array, build_ledger_row,
    build_strategy_spec, validate_ledger_row,
)


def _clean_row(**overrides):
    base = {f: None for f in TRADE_LEDGER_FIELDS}
    base.update({
        "trade_id": "t1", "session": "2026-07-24", "symbol": "SPY",
        "entry_quote_ts": ["2026-07-24T14:00:00+00:00"], "entry_bid": [0.30], "entry_ask": [0.32],
        "entry_fill": [0.32], "quantity": [1],
        "exit_bid": [0.40], "exit_ask": [0.42], "exit_fill": [0.40],
        "contract_gate": GATE_PASS,
    })
    base.update(overrides)
    return base


class StrategySpecTests(unittest.TestCase):
    def _spec(self, **overrides):
        kwargs = dict(
            strategy_id="s1", version="1", symbols=("SPY",), session_window=("10:00", "15:30"),
            direction_gate="CALL WATCH", contract_gate="PASS", fill_model="base_realistic",
            position_size=1, exit_rules={}, event_blackouts=[], parameter_set_id="p1",
            created_before_test_date="2026-07-26",
        )
        kwargs.update(overrides)
        return kwargs

    def test_builds_clean_spec(self):
        spec = build_strategy_spec(**self._spec())
        self.assertEqual(spec["symbols"], ["SPY"])
        self.assertEqual(spec["direction_gate"], "CALL WATCH")

    def test_rejects_symbol_outside_spy_qqq(self):
        with self.assertRaises(ValueError):
            build_strategy_spec(**self._spec(symbols=("TSLA",)))

    def test_rejects_invalid_direction_gate(self):
        with self.assertRaises(ValueError):
            build_strategy_spec(**self._spec(direction_gate="TWO-SIDED"))

    def test_qqq_allowed(self):
        spec = build_strategy_spec(**self._spec(symbols=("QQQ",)))
        self.assertEqual(spec["symbols"], ["QQQ"])


class AsFillArrayTests(unittest.TestCase):
    def test_none_becomes_empty_list(self):
        self.assertEqual(as_fill_array(None), [])

    def test_scalar_becomes_single_element_list(self):
        self.assertEqual(as_fill_array(0.32), [0.32])

    def test_list_passes_through(self):
        self.assertEqual(as_fill_array([0.32, 0.33]), [0.32, 0.33])


class LedgerRowTests(unittest.TestCase):
    def test_build_requires_every_field(self):
        with self.assertRaises(ValueError):
            build_ledger_row(trade_id="t1")

    def test_build_succeeds_with_all_fields(self):
        row = build_ledger_row(**_clean_row())
        for field in TRADE_LEDGER_FIELDS:
            self.assertIn(field, row)
        self.assertEqual(validate_ledger_row(row), [])

    def test_validate_flags_missing_field(self):
        row = build_ledger_row(**_clean_row())
        del row["mae"]
        problems = validate_ledger_row(row)
        self.assertTrue(any("mae" in p for p in problems))

    def test_validate_flags_non_array_fill_field(self):
        row = build_ledger_row(**_clean_row())
        row["entry_bid"] = 0.30  # should be [0.30]
        problems = validate_ledger_row(row)
        self.assertTrue(any("entry_bid" in p and "array" in p for p in problems))

    def test_validate_flags_bad_contract_gate(self):
        row = build_ledger_row(**_clean_row(contract_gate="MAYBE"))
        problems = validate_ledger_row(row)
        self.assertTrue(any("contract_gate" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
