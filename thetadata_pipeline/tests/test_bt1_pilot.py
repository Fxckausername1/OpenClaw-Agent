import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import thetadata_pipeline.bt1_pilot as bt1
from thetadata_pipeline.bt1_manifest import GRADE_FAIL, GRADE_PARTIAL, GRADE_PASS


def _fake_trade_quote_df(n=10, crossed=0):
    """n simple ASK-side trades (price AT the ask -- classify_trades' edge-
    proximity tolerance is tight, exactly-at-midpoint would grade MID, not
    ASK) plus `crossed` crossed-market rows, columns matching normalize.py's
    documented option_history_trade_quote shape."""
    now = pd.Timestamp("2026-07-24 10:00:00", tz="UTC")
    rows = []
    for i in range(n):
        rows.append(dict(
            symbol="SPY", expiration="2026-07-24", strike=745.0, right="C",
            trade_timestamp=now + pd.Timedelta(seconds=i), quote_timestamp=now + pd.Timedelta(seconds=i),
            sequence=i, condition=0, size=1, exchange="X", price=3.55,
            bid_size=5, bid_exchange="X", bid=3.45, bid_condition=0,
            ask_size=5, ask_exchange="X", ask=3.55, ask_condition=0,
        ))
    for j in range(crossed):
        rows.append(dict(
            symbol="SPY", expiration="2026-07-24", strike=745.0, right="C",
            trade_timestamp=now + pd.Timedelta(seconds=1000 + j), quote_timestamp=now + pd.Timedelta(seconds=1000 + j),
            sequence=1000 + j, condition=0, size=1, exchange="X", price=3.50,
            bid_size=5, bid_exchange="X", bid=3.60, bid_condition=0,  # bid > ask: crossed
            ask_size=5, ask_exchange="X", ask=3.55, ask_condition=0,
        ))
    return pd.DataFrame(rows)


def _fake_greeks_df(n=5):
    return pd.DataFrame({"delta": [0.5] * n, "implied_vol": [0.2] * n})


def _fake_oi_df(n=3):
    return pd.DataFrame({"strike": [745.0] * n, "right": ["C"] * n, "open_interest": [100] * n,
                          "expiration": ["2026-07-24"] * n})


def _fake_bars_df(n=390):
    idx = pd.date_range("2026-07-24 13:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"t": idx, "o": 700.0, "h": 700.5, "l": 699.5, "c": 700.1, "v": 1000})


SESSION = {"date": "2026-07-24", "open": "09:30", "close": "16:00", "is_early_close": False}


class PullBt1SessionTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.option_history_trade_quote.return_value = _fake_trade_quote_df()
        self.client.option_history_greeks_first_order.return_value = _fake_greeks_df()
        self.client.option_history_open_interest.return_value = _fake_oi_df()

        patches = [
            mock.patch.object(bt1, "get_client", return_value=self.client),
            mock.patch.object(bt1, "active_expirations", return_value=[dt.date(2026, 7, 24)]),
            mock.patch.object(bt1, "get_spot_price", return_value=745.0),
            mock.patch.object(bt1, "strike_window", return_value=[740.0, 745.0, 750.0]),
            mock.patch.object(bt1, "fetch_underlying_bars", return_value=_fake_bars_df()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_clean_session_grades_pass(self):
        row = bt1.pull_bt1_session("SPY", SESSION)
        self.assertEqual(row["symbol"], "SPY")
        self.assertGreater(row["received"]["option_trade_quote_rows"], 0)
        self.assertGreater(row["received"]["option_greeks_rows"], 0)
        self.assertEqual(row["received"]["open_interest_contracts"], 3)
        self.assertEqual(row["received"]["underlying_bars_count"], 390)
        self.assertEqual(row["quality_grade"], GRADE_PASS)
        self.assertEqual(row["received"]["distinct_occ_symbols"], 1)  # one strike/right combo in the fixture

    def test_occ_symbols_are_real_occ_format(self):
        # pull_bt1_session only reports a count, but exercise the internal
        # occ_symbol() call path indirectly by checking it doesn't raise for
        # this fixture's strike/right combination.
        from thetadata_pipeline.schemas import occ_symbol
        self.assertEqual(occ_symbol("SPY", dt.date(2026, 7, 24), 745.0, "C"), "SPY260724C00745000")

    def test_crossed_market_rows_are_detected_and_counted(self):
        self.client.option_history_trade_quote.return_value = _fake_trade_quote_df(n=10, crossed=5)
        row = bt1.pull_bt1_session("SPY", SESSION)
        self.assertGreater(row["rejected"]["crossed_or_locked_quote_rows"], 0)

    def test_no_expiration_is_unrecoverable_and_fails(self):
        with mock.patch.object(bt1, "active_expirations", return_value=[]):
            row = bt1.pull_bt1_session("SPY", SESSION)
        self.assertEqual(row["quality_grade"], GRADE_FAIL)
        self.assertTrue(any("no active expiration" in r for r in row["missing"]["unrecoverable_errors"]))

    def test_thetadata_error_is_recorded_not_silently_swallowed(self):
        self.client.option_history_trade_quote.side_effect = bt1.ThetaDataUnavailable("boom")
        # bounded_call's real retry/backoff sleeps for real seconds on each
        # of the 26 chunks' failures -- irrelevant to what this test checks
        # (that a failure is recorded, not silently dropped), so disable the
        # sleep only, not the retry logic itself.
        with mock.patch("thetadata_pipeline.client.time.sleep"):
            row = bt1.pull_bt1_session("SPY", SESSION)
        self.assertGreater(row["response_metadata"]["thetadata_errors"], 0)
        self.assertEqual(row["quality_grade"], GRADE_FAIL)

    def test_zero_bars_fails_even_with_good_options_data(self):
        with mock.patch.object(bt1, "fetch_underlying_bars", return_value=_fake_bars_df(n=0)):
            row = bt1.pull_bt1_session("SPY", SESSION)
        self.assertEqual(row["quality_grade"], GRADE_FAIL)


class RunBt1PilotTests(unittest.TestCase):
    def test_end_to_end_writes_manifest_and_returns_overall(self):
        # REAL BUG this isolation fixes, found live 2026-07-26: this test used
        # to call run_bt1_pilot() with bt1.BT1_DIR/BT1_MANIFEST_PATH left
        # pointed at their real module-level defaults
        # (data/thetadata/bt1_pilot/bt1_pilot_manifest.json), which is the
        # SAME file the real, non-mocked 5-session pilot run writes to.
        # Running this test suite after that real pilot run silently
        # clobbered heff's actual 10-session (5 sessions x SPY/QQQ) graded
        # manifest with this test's synthetic 2-session fixture data -- a
        # real production-data loss caught only by cross-checking
        # logs/bt1_pilot_run.log's untouched real output. Every path this
        # test's call chain can write to must be patched to a throwaway
        # TemporaryDirectory, never the real data/thetadata/bt1_pilot/ dir.
        client = mock.Mock()
        client.option_history_trade_quote.return_value = _fake_trade_quote_df()
        client.option_history_greeks_first_order.return_value = _fake_greeks_df()
        client.option_history_open_interest.return_value = _fake_oi_df()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp) / "bt1_pilot"
            tmp_manifest = tmp_dir / "bt1_pilot_manifest.json"
            with mock.patch.object(bt1, "get_client", return_value=client), \
                 mock.patch.object(bt1, "active_expirations", return_value=[dt.date(2026, 7, 24)]), \
                 mock.patch.object(bt1, "get_spot_price", return_value=745.0), \
                 mock.patch.object(bt1, "strike_window", return_value=[740.0, 745.0, 750.0]), \
                 mock.patch.object(bt1, "fetch_underlying_bars", return_value=_fake_bars_df()), \
                 mock.patch.object(bt1, "most_recent_complete_sessions", return_value=[SESSION]), \
                 mock.patch.object(bt1, "BT1_DIR", tmp_dir), \
                 mock.patch.object(bt1, "BT1_MANIFEST_PATH", tmp_manifest):
                result = bt1.run_bt1_pilot(symbols=("SPY", "QQQ"), sessions=1)

            self.assertEqual(len(result["sessions"]), 2)  # SPY + QQQ, 1 session each
            self.assertEqual(result["overall"]["sessions_total"], 2)
            self.assertTrue(tmp_manifest.exists())
            import json
            on_disk = json.loads(tmp_manifest.read_text())
            self.assertEqual(on_disk["overall"]["sessions_total"], 2)


if __name__ == "__main__":
    unittest.main()
