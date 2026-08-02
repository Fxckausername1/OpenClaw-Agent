"""Tests for smc/redact.py and its wiring into trade_updates."""
from __future__ import annotations

import json
import unittest

from smc.redact import REDACTED, redact, retain_order_fields, scrub_text
from smc.trade_updates import AlpacaTradeUpdatesClient

PAPER = "https://paper-api.alpaca.markets"


class RedactTests(unittest.TestCase):
    def test_secret_keys_masked(self):
        out = redact({"key_id": "PKABC", "secret_key": "s3cr3t",
                      "authorization": "Bearer x", "session_token": "t"})
        self.assertTrue(all(v == REDACTED for v in out.values()))

    def test_business_ids_preserved(self):
        out = redact({"client_order_id": "smc-1", "order_id": "brk-1",
                      "replaced_by": "brk-2"})
        self.assertEqual(out["client_order_id"], "smc-1")
        self.assertEqual(out["order_id"], "brk-1")
        self.assertEqual(out["replaced_by"], "brk-2")

    def test_nested_and_list_structures(self):
        out = redact({"a": [{"api_key": "x"}, {"symbol": "QQQ"}]})
        self.assertEqual(out["a"][0]["api_key"], REDACTED)
        self.assertEqual(out["a"][1]["symbol"], "QQQ")

    def test_structure_preserved_for_diagnosability(self):
        out = redact({"data": {"order": {"symbol": "QQQ", "token": "t"}}})
        self.assertEqual(out["data"]["order"]["symbol"], "QQQ")
        self.assertEqual(out["data"]["order"]["token"], REDACTED)

    def test_does_not_mutate_input(self):
        src = {"secret_key": "s"}
        redact(src)
        self.assertEqual(src["secret_key"], "s")


class RetainTests(unittest.TestCase):
    def _msg(self):
        return {"stream": "trade_updates",
                "data": {"event": "fill", "timestamp": "t", "price": "0.93",
                         "qty": "1", "position_qty": "1",
                         "order": {"id": "brk-1", "client_order_id": "smc-1",
                                   "symbol": "QQQ260803C00580000", "side": "buy",
                                   "status": "filled", "filled_avg_price": "0.93",
                                   "secret_key": "LEAK", "auth_token": "LEAK2",
                                   "some_future_field": "dropped"}}}

    def test_keeps_reconstruction_fields(self):
        out = retain_order_fields(self._msg())
        o = out["data"]["order"]
        for f in ("id", "client_order_id", "symbol", "side", "status", "filled_avg_price"):
            self.assertIn(f, o)
        self.assertEqual(out["data"]["price"], "0.93")

    def test_drops_unlisted_fields_entirely(self):
        out = retain_order_fields(self._msg())
        self.assertNotIn("some_future_field", out["data"]["order"])

    def test_secrets_cannot_survive_the_projection(self):
        blob = json.dumps(retain_order_fields(self._msg()))
        self.assertNotIn("LEAK", blob)
        self.assertNotIn("secret_key", blob)

    def test_handles_non_dict_and_empty(self):
        self.assertEqual(retain_order_fields(None), {})
        self.assertEqual(retain_order_fields("x"), {})
        self.assertIn("data", retain_order_fields({"stream": "trade_updates"}))


class ScrubTextTests(unittest.TestCase):
    def test_masks_alpaca_key_shapes(self):
        self.assertIn(REDACTED, scrub_text("using PKABCDEFGHIJKL now"))
        self.assertIn(REDACTED, scrub_text("using AKABCDEFGHIJKL now"))

    def test_masks_bearer_and_assignments(self):
        self.assertIn(REDACTED, scrub_text("Authorization: Bearer abcdef1234567890"))
        self.assertIn(REDACTED, scrub_text('{"secret_key": "supersecretvalue"}'))

    def test_leaves_ordinary_text_alone(self):
        msg = "filled 1 QQQ260803C00580000 @ 0.93"
        self.assertEqual(scrub_text(msg), msg)


class WiringTests(unittest.TestCase):
    """The raw payload actually retained on a TradeUpdate must be scrubbed."""

    def test_stored_raw_is_projected_and_redacted(self):
        c = AlpacaTradeUpdatesClient(PAPER, "PKTEST", "sec")
        c._handle_message(json.dumps({
            "stream": "trade_updates",
            "data": {"event": "fill", "price": "0.93",
                     "order": {"id": "brk-1", "client_order_id": "coid",
                               "symbol": "QQQ260803C00580000", "side": "buy",
                               "secret_key": "LEAKED", "junk": "x"}}}))
        u = c.latest("coid")
        blob = json.dumps(u.raw)
        self.assertNotIn("LEAKED", blob)
        self.assertNotIn("junk", blob)
        self.assertIn("client_order_id", blob)
        self.assertEqual(u.event_price, 0.93)   # parsing still works


if __name__ == "__main__":
    unittest.main()
