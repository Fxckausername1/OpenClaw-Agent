from __future__ import annotations

import datetime as dt
import json
import threading
import time
import unittest

from smc import events as ev
from smc.assembly import build_pipeline
from smc.events import make_event
from smc.quote_worker import QuoteExitWorker
from smc.readiness import GATES, PASS
from smc.runner import PaperRunner
from smc.theta_stream import StreamQuote, ThetaStreamClient


OCC = "QQQ260803C00605000"


def quote(occ=OCC, generation=1):
    now = dt.datetime.now(dt.timezone.utc)
    return StreamQuote(
        occ=occ, root="QQQ", expiration=dt.date(2026, 8, 3), strike=605.0,
        right="C", bid=0.50, ask=0.55, bid_size=10, ask_size=10,
        exchange_ts=now, receipt_ts=now, receipt_monotonic=time.monotonic(),
        generation=generation)


class ThetaCallbackRegressionTests(unittest.TestCase):
    def test_real_quote_invokes_callback_after_cache_commit(self):
        client = ThetaStreamClient()
        with client._lock:
            client._generation = 1
        seen = []
        client.on_quote = lambda q: seen.append((q, client.get_quote(q.occ)))
        now_et = dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))
        ms = ((now_et.hour * 60 + now_et.minute) * 60 + now_et.second) * 1000
        msg = {
            "header": {"type": "QUOTE"},
            "contract": {"security_type": "OPTION", "root": "QQQ",
                         "expiration": 20260803, "strike": 605000, "right": "C"},
            "quote": {"ms_of_day": ms, "bid_size": 7, "bid": 0.50,
                      "ask_size": 7, "ask": 0.55,
                      "date": int(now_et.strftime("%Y%m%d"))},
        }
        client._handle_message(json.dumps(msg))
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], seen[0][1])


class QuoteWorkerIsolationTests(unittest.TestCase):
    def test_reader_never_inherits_broker_latency(self):
        for delay in (0.010, 0.050, 0.100, 0.500):
            with self.subTest(delay=delay):
                entered = threading.Event()

                def slow(_q):
                    entered.set()
                    time.sleep(delay)

                worker = QuoteExitWorker(handle_quote=slow, maxsize=8)
                worker.start()
                self.assertTrue(worker.submit(quote()))
                self.assertTrue(entered.wait(1.0))
                t0 = time.monotonic()
                for _ in range(100):
                    worker.submit(quote())
                reader_ms = (time.monotonic() - t0) * 1000.0
                worker.stop(drain=True, timeout=3.0)
                health = worker.health()
                self.assertLess(reader_ms, 50.0)
                self.assertLess(health["reader_callback_ms"]["p95"], 5.0)
                self.assertGreaterEqual(health["coalesced"], 90)
                self.assertEqual(health["dropped_non_position"], 0)

    def test_timeout_does_not_kill_worker(self):
        calls = []

        def flaky(q):
            calls.append(q.occ)
            if len(calls) == 1:
                raise TimeoutError("simulated broker timeout")

        worker = QuoteExitWorker(handle_quote=flaky, maxsize=8)
        worker.start()
        worker.submit(quote("QQQ260803C00605000"))
        deadline = time.monotonic() + 1.0
        while len(calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        worker.submit(quote("QQQ260803P00605000"))
        worker.stop(drain=True)
        self.assertEqual(worker.health()["errors"], 1)
        self.assertEqual(len(calls), 2)

    def test_position_quote_displaces_non_position_at_capacity(self):
        protected = "QQQ260803C00610000"
        worker = QuoteExitWorker(handle_quote=lambda q: None,
                                 is_protected_occ=lambda occ: occ == protected,
                                 maxsize=2)
        worker.submit(quote("QQQ260803C00600000"))
        worker.submit(quote("QQQ260803P00600000"))
        self.assertTrue(worker.submit(quote(protected)))
        health = worker.health()
        self.assertEqual(health["depth"], 2)
        self.assertEqual(health["evicted_non_position"], 1)


class ModeGateRegressionTests(unittest.TestCase):
    def test_connectivity_mode_cannot_reach_book_or_broker_when_green(self):
        runner = PaperRunner()
        for gate in GATES:
            runner.readiness.set(gate, PASS)
        built = []
        submitted = []
        build_pipeline(
            runner=runner, entry_enabled=lambda: False,
            build_universe=lambda sig: built.append(sig),
            select_contract=lambda book, sig: object(),
            entry_builder=lambda *a: object(),
            entry_submitter=lambda *a: submitted.append(a))
        sig = type("S", (), {"side": "long", "trigger": "MSS"})()
        runner.bus.publish(make_event(
            ev.EV_SIGNAL, signal_id="s1",
            payload={"signal": sig, "identity": {"signal_key": "s1"}}))
        runner.run(max_events=1, idle_timeout=0.1)
        self.assertEqual(built, [])
        self.assertEqual(submitted, [])


class LiveReadinessRegressionTests(unittest.TestCase):
    def test_real_current_generation_quote_updates_live_gates(self):
        stream = ThetaStreamClient()
        with stream._lock:
            stream._generation = 1
            stream._connected = True
            stream._desired = {OCC}
            stream._subscribed = {OCC}

        class Greeks:
            def greek_snapshot(self, occs):
                return {"deltas": {OCC: {"delta": 0.35,
                                          "source_monotonic": time.monotonic()}}}

            def cache_status(self):
                return {"2026-08-03": {"n_rows": 1, "age_seconds": 0.1}}

        runner = PaperRunner(theta_stream=stream, greek_cache=Greeks())
        runner.market_closed = True
        runner.observe_live_quote(quote())
        self.assertEqual(runner.readiness.gates["theta_quote_parser_verified"].status,
                         PASS)
        self.assertEqual(runner.readiness.gates["theta_live_quotes_fresh"].status,
                         PASS)
        self.assertEqual(runner.readiness.gates["candidate_universe_ready"].status,
                         PASS)
        self.assertFalse(runner.market_closed)


if __name__ == "__main__":
    unittest.main()
