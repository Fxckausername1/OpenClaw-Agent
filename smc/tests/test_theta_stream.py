import datetime as dt
import json
import time
import unittest

from smc.theta_stream import (
    StreamQuote, ThetaStreamClient, _ms_of_day_to_utc, _occ_from_contract, _yyyymmdd_to_date,
)


class YyyymmddToDateTests(unittest.TestCase):
    def test_int_format(self):
        self.assertEqual(_yyyymmdd_to_date(20260803), dt.date(2026, 8, 3))

    def test_iso_string_format(self):
        self.assertEqual(_yyyymmdd_to_date("2026-08-03"), dt.date(2026, 8, 3))

    def test_invalid_returns_none(self):
        self.assertIsNone(_yyyymmdd_to_date("not-a-date"))
        self.assertIsNone(_yyyymmdd_to_date(None))


class MsOfDayToUtcTests(unittest.TestCase):
    def test_market_open_930_et(self):
        # 09:30:00 ET = 34,200,000 ms since midnight ET
        result = _ms_of_day_to_utc(20260803, 34_200_000)
        et = result.astimezone(dt.timezone(dt.timedelta(hours=-4)))  # EDT in August
        self.assertEqual(et.hour, 9)
        self.assertEqual(et.minute, 30)

    def test_is_timezone_aware_utc(self):
        result = _ms_of_day_to_utc(20260803, 0)
        self.assertEqual(result.tzinfo, dt.timezone.utc)


class OccFromContractTests(unittest.TestCase):
    def test_builds_correct_occ(self):
        contract = {"root": "QQQ", "expiration": 20260803, "strike": 605000, "right": "C"}
        self.assertEqual(_occ_from_contract(contract), "QQQ260803C00605000")

    def test_handles_put(self):
        contract = {"root": "QQQ", "expiration": 20260803, "strike": 605000, "right": "PUT"}
        self.assertEqual(_occ_from_contract(contract), "QQQ260803P00605000")

    def test_missing_field_returns_none(self):
        self.assertIsNone(_occ_from_contract({"root": "QQQ"}))


class StreamQuoteTests(unittest.TestCase):
    def test_two_sided_valid_quote(self):
        q = StreamQuote(occ="QQQ260803C00605000", root="QQQ", expiration=dt.date(2026, 8, 3),
                        strike=605.0, right="C", bid=0.50, ask=0.55, bid_size=10, ask_size=10,
                        exchange_ts=None, receipt_ts=dt.datetime.now(dt.timezone.utc),
                        receipt_monotonic=time.monotonic())
        self.assertTrue(q.two_sided)

    def test_crossed_quote_not_two_sided(self):
        q = StreamQuote(occ="x", root="QQQ", expiration=None, strike=605.0, right="C",
                        bid=0.55, ask=0.50, bid_size=10, ask_size=10, exchange_ts=None,
                        receipt_ts=dt.datetime.now(dt.timezone.utc), receipt_monotonic=time.monotonic())
        self.assertFalse(q.two_sided)

    def test_missing_bid_not_two_sided(self):
        q = StreamQuote(occ="x", root="QQQ", expiration=None, strike=605.0, right="C",
                        bid=None, ask=0.55, bid_size=None, ask_size=10, exchange_ts=None,
                        receipt_ts=dt.datetime.now(dt.timezone.utc), receipt_monotonic=time.monotonic())
        self.assertFalse(q.two_sided)

    def test_age_seconds_computed_from_monotonic(self):
        t0 = time.monotonic()
        q = StreamQuote(occ="x", root="QQQ", expiration=None, strike=605.0, right="C",
                        bid=0.5, ask=0.55, bid_size=1, ask_size=1, exchange_ts=None,
                        receipt_ts=dt.datetime.now(dt.timezone.utc), receipt_monotonic=t0)
        self.assertAlmostEqual(q.age_seconds(now_monotonic=t0 + 3.5), 3.5, places=3)


class ThetaStreamClientMessageHandlingTests(unittest.TestCase):
    """Tests _handle_message and cache state directly -- no live WebSocket
    connection, no background thread started."""

    def setUp(self):
        self.client = ThetaStreamClient()

    def test_quote_message_populates_cache(self):
        msg = {
            "header": {"type": "QUOTE"},
            "contract": {"security_type": "OPTION", "root": "QQQ", "expiration": 20260803,
                        "strike": 605000, "right": "C"},
            "quote": {"ms_of_day": 34_200_000, "bid_size": 7, "bid": 0.50, "ask_size": 7,
                     "ask": 0.55, "date": 20260803},
        }
        self.client._handle_message(json.dumps(msg))
        q = self.client.get_quote("QQQ260803C00605000")
        self.assertIsNotNone(q)
        self.assertEqual(q.bid, 0.50)
        self.assertEqual(q.ask, 0.55)
        self.assertIsNotNone(q.exchange_ts)

    def test_status_heartbeat_does_not_populate_cache(self):
        msg = {"header": {"type": "STATUS", "status": "CONNECTED"}}
        self.client._handle_message(json.dumps(msg))
        health = self.client.health()
        self.assertEqual(health["n_cached_quotes"], 0)

    def test_subscribe_ack_marks_subscribed(self):
        # Verified live, 2026-08-01: the real ack carries NO `contract`
        # field, only `req_id` -- this test uses the ACTUAL observed shape,
        # not the (incorrect, on this point) public docs example. Using the
        # docs shape here previously masked a real bug where acks never
        # resolved to an occ at all.
        with self.client._lock:
            self.client._pending_subs[1] = "QQQ260803C00605000"
        msg = {"header": {"type": "REQ_RESPONSE", "status": "CONNECTED",
                          "response": "SUBSCRIBED", "req_id": 1}}
        self.client._handle_message(json.dumps(msg))
        with self.client._lock:
            self.assertIn("QQQ260803C00605000", self.client._subscribed)
            self.assertNotIn(1, self.client._pending_subs)

    def test_ack_for_unknown_req_id_does_not_crash(self):
        msg = {"header": {"type": "REQ_RESPONSE", "status": "CONNECTED",
                          "response": "SUBSCRIBED", "req_id": 999}}
        self.client._handle_message(json.dumps(msg))  # must not raise
        with self.client._lock:
            self.assertEqual(len(self.client._subscribed), 0)

    def test_malformed_json_counted_not_raised(self):
        self.client._handle_message("not valid json{{{")
        health = self.client.health()
        self.assertEqual(health["malformed_count"], 1)

    def test_missing_quote_field_counted_as_malformed(self):
        msg = {"header": {"type": "QUOTE"},
               "contract": {"root": "QQQ", "expiration": 20260803, "strike": 605000, "right": "C"}}
        self.client._handle_message(json.dumps(msg))
        health = self.client.health()
        self.assertEqual(health["malformed_count"], 1)


class ThetaStreamClientSubscriptionStateTests(unittest.TestCase):
    def setUp(self):
        self.client = ThetaStreamClient()

    def test_set_desired_universe_updates_state(self):
        occs = {"QQQ260803C00605000", "QQQ260803P00605000"}
        self.client.set_desired_universe(occs)
        with self.client._lock:
            self.assertEqual(self.client._desired, occs)

    def test_health_reports_desired_vs_subscribed_gap(self):
        self.client.set_desired_universe({"A", "B", "C"})
        health = self.client.health()
        self.assertEqual(health["n_desired"], 3)
        self.assertEqual(health["n_subscribed"], 0)
        self.assertFalse(health["connected"])

    def test_unknown_occ_returns_none_not_stale_data(self):
        self.assertIsNone(self.client.get_quote("NEVER_SUBSCRIBED"))


if __name__ == "__main__":
    unittest.main()
