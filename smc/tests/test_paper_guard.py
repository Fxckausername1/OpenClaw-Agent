"""Tests for smc/paper_guard.py.

Covers heff's prelaunch requirements (paper hostname enforcement, live
hostname rejection, credential-is-paper check) AND the revised termination
policy: the guard raises a fatal typed exception; only the top-level entry
point converts that into SystemExit, and os._exit is never used.

unittest, no network.
"""
from __future__ import annotations

import unittest

from smc.paper_guard import (
    EXIT_CODE_PAPER_VIOLATION, PAPER_HOST, PaperGuardViolation,
    assert_paper_credentials, assert_paper_endpoint, enforce_paper_mode,
    fatal_guard, is_paper, resolved_host,
)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class EndpointTests(unittest.TestCase):
    def test_paper_endpoint_accepted(self):
        self.assertEqual(assert_paper_endpoint(PAPER_URL), PAPER_HOST)

    def test_paper_endpoint_accepted_with_path_and_whitespace(self):
        self.assertEqual(
            assert_paper_endpoint("  https://paper-api.alpaca.markets/v2  "), PAPER_HOST)

    def test_live_endpoint_rejected(self):
        with self.assertRaises(PaperGuardViolation) as ctx:
            assert_paper_endpoint(LIVE_URL)
        self.assertIn("LIVE", str(ctx.exception))

    def test_lookalike_hosts_rejected(self):
        """Exactly the strings a substring check would wrongly accept."""
        for url in ("https://api.alpaca.markets/?redirect=paper-api.alpaca.markets",
                    "https://paper-api.alpaca.markets.evil.com",
                    "https://evil.com/paper-api.alpaca.markets"):
            with self.subTest(url=url):
                with self.assertRaises(PaperGuardViolation):
                    assert_paper_endpoint(url)

    def test_non_https_rejected(self):
        with self.assertRaises(PaperGuardViolation):
            assert_paper_endpoint("http://paper-api.alpaca.markets")

    def test_userinfo_rejected(self):
        with self.assertRaises(PaperGuardViolation):
            assert_paper_endpoint("https://user:pw@paper-api.alpaca.markets")

    def test_missing_or_unparseable_rejected(self):
        for url in (None, "", "not a url", "https://"):
            with self.subTest(url=url):
                with self.assertRaises(PaperGuardViolation):
                    assert_paper_endpoint(url)

    def test_resolved_host_strips_port(self):
        self.assertEqual(resolved_host("https://paper-api.alpaca.markets:443/v2"), PAPER_HOST)


class CredentialTests(unittest.TestCase):
    def test_live_key_rejected(self):
        with self.assertRaises(PaperGuardViolation) as ctx:
            assert_paper_credentials("AKEXAMPLE1234567890")
        self.assertIn("LIVE", str(ctx.exception))

    def test_paper_key_accepted(self):
        self.assertIsNone(assert_paper_credentials("PKEXAMPLE1234567890"))

    def test_unrecognized_prefix_warns_but_does_not_block(self):
        """Asymmetric by design: refuse what is positively live, never claim
        to positively identify paper."""
        with self.assertLogs("smc.paper_guard", level="WARNING") as logs:
            self.assertIsNone(assert_paper_credentials("XYEXAMPLE123"))
        self.assertTrue(any("unrecognized prefix" in m for m in logs.output))

    def test_missing_key_rejected(self):
        for key in (None, "", 123):
            with self.subTest(key=key):
                with self.assertRaises(PaperGuardViolation):
                    assert_paper_credentials(key)


class TerminationPolicyTests(unittest.TestCase):
    def test_enforce_raises_and_never_exits(self):
        """The guard itself must not kill the process -- os._exit after
        initialization would bypass SQLite commit and log flushing."""
        with self.assertRaises(PaperGuardViolation):
            enforce_paper_mode(LIVE_URL, "PKABC123")

    def test_enforce_happy_path_returns_host(self):
        self.assertEqual(enforce_paper_mode(PAPER_URL, "PKABC123"), PAPER_HOST)

    def test_fatal_guard_exits_nonzero_at_entry_point(self):
        with self.assertRaises(SystemExit) as ctx:
            fatal_guard(LIVE_URL, "PKABC123")
        self.assertEqual(ctx.exception.code, EXIT_CODE_PAPER_VIOLATION)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_fatal_guard_passes_through_on_paper(self):
        self.assertEqual(fatal_guard(PAPER_URL, "PKABC123"), PAPER_HOST)

    def test_mixed_paper_endpoint_with_live_key_refused(self):
        """The dangerous mixed case."""
        with self.assertRaises(PaperGuardViolation) as ctx:
            enforce_paper_mode(PAPER_URL, "AKABC123")
        self.assertIn("LIVE", str(ctx.exception))


class RuntimeRecheckTests(unittest.TestCase):
    """An invalid endpoint must be caught before init, after a config
    reload, and on a reconnect -- a runtime config change must never migrate
    a PAPER process onto a live endpoint."""

    def test_before_initialization(self):
        with self.assertRaises(SystemExit):
            fatal_guard(LIVE_URL, "PKABC123")

    def test_after_config_reload(self):
        reloaded = {"base_url": LIVE_URL}
        self.assertFalse(is_paper(reloaded["base_url"], "PKABC123"))

    def test_during_reconnect(self):
        self.assertFalse(is_paper(LIVE_URL))
        self.assertTrue(is_paper(PAPER_URL))

    def test_is_paper_never_raises(self):
        for url in (None, "", "http://x", LIVE_URL, "https://evil.com"):
            with self.subTest(url=url):
                self.assertFalse(is_paper(url))


if __name__ == "__main__":
    unittest.main()
