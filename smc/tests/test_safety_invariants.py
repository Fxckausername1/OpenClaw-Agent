"""Reporting isolation, ownership, reconciliation, corruption, calendar and risk
gates (required tests 3, 4, 6, 7, 15, 17, 18, plus the Phase 6 gates).

Theme: none of the REPORTING or convenience layers may ever be able to interfere
with risk management, and no ambiguity may ever be resolved by assuming the
favourable interpretation.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from smc import calendar as smc_calendar
from smc.calendar import SessionSchedule, must_flatten, session_schedule
from smc.lifecycle import EXIT_STOP, execute_exit, submit_entry
from smc.reconcile import assert_occ_free_for_entry, foreign_occ_owners, reconcile
from smc.risk import check_entry_allowed, consecutive_losses, daily_realized_pnl, flatten_watchdog
from smc.state import (
    CLOSED, OPEN, RECON_MISMATCH, SmcStateError, SmcStateStore,
)
from smc.tests.harness import (
    FILL_IMMEDIATE, FILL_NEVER, OCC, OTHER_OCC, FakeBroker, FakeClock, add_foreign_position,
    make_config, make_dashboard, make_store, make_tmpdir,
)

ET = ZoneInfo("America/New_York")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmpdir()
        self.config = make_config(self.tmp)
        self.store = make_store(self.config)
        self.broker = FakeBroker()
        self.broker.set_quote(OCC, bid=0.50, ask=0.52)
        self.clock = FakeClock()
        self.dashboard = make_dashboard(self.tmp)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open_position(self, occ=OCC, qty=1, entry_price=0.80, signal_key="s:1:long"):
        intent = self.store.create_entry_intent(
            signal_key=signal_key, occ=occ, underlying="QQQ", contract_right="C",
            signal_side="long", intended_qty=qty, limit_price=entry_price,
            order_type="limit", signal_ts="2026-07-31T14:00:00+00:00")
        self.store.mark_order_submitted(intent["client_order_id"])
        self.store.record_broker_ack(intent["client_order_id"],
                                      f"bkr-{intent['position_id']}")
        self.store.record_order_fill(intent["client_order_id"], qty, entry_price, OPEN)
        self.store.record_entry_filled(intent["position_id"], qty, entry_price, True)
        self.broker.set_position(occ, qty)
        return self.store.get_position(intent["position_id"])


# ============================================================== scenarios 3, 4
class ReportingIsolationTest(_Base):
    def test_dashboard_write_failure_does_not_prevent_a_protective_exit(self):
        """Required test 3. The dashboard is REPORTING ONLY. A ledger write blowing
        up must not stop a stop-loss from executing."""
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig

        pos = self._open_position(entry_price=0.80)
        self.broker.set_quote(OCC, bid=0.60, ask=0.62)  # -25%: through the -20% stop
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60

        def exploding_dashboard(**kwargs):
            raise sqlite3.OperationalError("database is locked")

        pipeline = SmcPipeline(
            self.store, self.broker, self.config, detect_fn=lambda: [],
            select_fn=lambda t: None, exit_config=ExitConfig(),
            dashboard_sync_fn=exploding_dashboard, dashboard_db=self.dashboard,
            clock=self.clock)

        report = pipeline.supervise_once(arm=True)

        self.assertEqual(report.exits_executed, 1)
        self.assertEqual(self.broker.positions[OCC], 0,
                         "the position MUST be flat despite the dashboard failure")
        self.assertEqual(self.store.get_position(pos["position_id"])["state"], CLOSED)

    def test_stale_quote_cannot_trigger_a_protective_price_decision(self):
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig
        self._open_position(entry_price=0.80)
        self.broker.set_quote(OCC, bid=0.60, ask=0.62)
        self.clock.advance(30)
        pipeline = SmcPipeline(
            self.store, self.broker, self.config, detect_fn=lambda: [],
            select_fn=lambda trigger: None, exit_config=ExitConfig(),
            dashboard_db=self.dashboard, clock=self.clock)
        report = pipeline.supervise_once(arm=True)
        self.assertEqual(report.exits_executed, 0)
        self.assertEqual(self.broker.positions[OCC], 1)
        self.assertTrue(any("fresh quote" in note for note in report.notes))

    def test_dashboard_failure_is_recorded_but_not_fatal_on_entry(self):
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig
        from smc.tests.harness import make_contract, make_trigger

        self.broker.fill_policy = FILL_IMMEDIATE

        def exploding_dashboard(**kwargs):
            raise RuntimeError("ledger unavailable")

        pipeline = SmcPipeline(
            self.store, self.broker, self.config,
            detect_fn=lambda: [make_trigger(signal_ts=self.clock.now().isoformat())],
            select_fn=lambda t: make_contract(), exit_config=ExitConfig(),
            dashboard_sync_fn=exploding_dashboard, dashboard_db=self.dashboard,
            clock=self.clock)

        report = pipeline.run_signal_cycle(arm=True)

        self.assertEqual(report.entries_filled, 1, "entry must succeed despite reporting failure")
        pos = self.store.open_positions()[0]
        self.assertEqual(pos["dashboard_synced"], 0, "the sync failure must be recorded")

    def test_telegram_failure_never_blocks_or_unwinds_state(self):
        """Required test 4. The old code used a BLOCKING subprocess.run(timeout=150)
        inline in the exit path on a box where the CLI cold-starts in 33-41s."""
        from smc import notify
        pos = self._open_position(entry_price=0.80)
        self.broker.set_quote(OCC, bid=0.60, ask=0.62)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60

        original = notify.subprocess.Popen
        calls = {"n": 0}

        def exploding_popen(*a, **k):
            calls["n"] += 1
            raise OSError("openclaw binary wedged")

        notify.subprocess.Popen = exploding_popen
        try:
            outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                                    arm=True, clock=self.clock)
            sent = notify.send("test", make_config(self.tmp, notifications_enabled=True))
            self.assertFalse(sent, "a failed launch must report False, not raise")
        finally:
            notify.subprocess.Popen = original

        self.assertEqual(outcome.state, CLOSED)
        self.assertEqual(self.broker.positions[OCC], 0)

    def test_notify_refuses_to_announce_before_state_commit(self):
        from smc import notify
        original = notify.subprocess.Popen
        launched = {"n": 0, "argv": None}

        def counting_popen(*a, **k):
            launched["n"] += 1
            launched["argv"] = a[0]

            class _P:  # noqa: D401
                pass
            return _P()

        notify.subprocess.Popen = counting_popen
        try:
            self.assertFalse(notify.notify_after_commit(False, "premature", self.config))
            self.assertEqual(launched["n"], 0, "must not announce uncommitted state")
            enabled = make_config(self.tmp, notifications_enabled=True)
            self.assertTrue(notify.notify_after_commit(True, "committed", enabled))
            self.assertEqual(launched["n"], 1)
            self.assertEqual(launched["argv"][0], notify.TIMEOUT_BIN)
            self.assertEqual(launched["argv"][1], "5.000s")
        finally:
            notify.subprocess.Popen = original


# ============================================================== scenarios 6, 7

class EntryQuoteSafetyTest(_Base):
    def _pipeline(self, selected_ask=0.80):
        from smc.pipeline import SmcPipeline
        from smc.tests.harness import make_contract, make_trigger
        from thetadata_pipeline.bt2_exits import ExitConfig
        return SmcPipeline(
            self.store, self.broker, self.config,
            detect_fn=lambda: [make_trigger(signal_ts=self.clock.now().isoformat())],
            select_fn=lambda trigger: make_contract(ask=selected_ask),
            exit_config=ExitConfig(), dashboard_db=self.dashboard, clock=self.clock,
        )

    # ---- Entry quote PROVENANCE policy.
    #
    # This originally asserted that an indicative quote could never price an entry.
    # Changed 2026-08-01 by explicit decision, after measuring that this account has
    # no OPRA entitlement (HTTP 403 "OPRA agreement is not signed"), which blocked
    # 4/4 liquid near-money QQQ contracts and made entry structurally impossible.
    #
    # The safety property is NARROWED, not dropped, and the tests below pin every
    # edge of it: indicative is permitted ONLY in paper mode AND only when the flag
    # is on; live money still refuses it unconditionally; and freshness/two-sidedness
    # are still enforced regardless of feed. Rationale for why this is the right
    # place to loosen is in SmcConfig.allow_indicative_entry_quotes_in_paper.

    def test_indicative_quote_is_accepted_for_entry_in_paper(self):
        from smc.broker import FEED_INDICATIVE
        self.broker.fill_policy = FILL_NEVER
        self.broker.set_quote(OCC, bid=0.50, ask=0.52, feed=FEED_INDICATIVE)
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(report.signals_gated, 0)
        self.assertEqual(report.entries_submitted, 1)
        self.assertEqual(report.entry_quote_feeds.get(FEED_INDICATIVE), 1,
                         "the feed that priced the entry must be recorded, not implicit")

    def test_indicative_quote_refused_for_entry_when_allowance_is_off(self):
        from smc.broker import FEED_INDICATIVE
        self.config = make_config(self.tmp, allow_indicative_entry_quotes_in_paper=False)
        self.broker.set_quote(OCC, bid=0.50, ask=0.52, feed=FEED_INDICATIVE)
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(report.signals_gated, 1)
        self.assertEqual(self.broker.buy_orders(), [])
        self.assertTrue(any("fresh OPRA" in n for n in report.notes))

    def test_live_mode_refuses_indicative_entry_even_with_allowance_on(self):
        """The load-bearing assertion: the paper allowance must be incapable of
        leaking into real money."""
        from smc.broker import FEED_INDICATIVE
        from smc.config import MODE_LIVE_APPROVED
        self.config = make_config(self.tmp, mode=MODE_LIVE_APPROVED,
                                  allow_indicative_entry_quotes_in_paper=True)
        allowed, reason = self.config.entry_quote_policy()
        self.assertFalse(allowed)
        self.assertIn("paper-only", reason)

        self.broker.set_quote(OCC, bid=0.50, ask=0.52, feed=FEED_INDICATIVE)
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(report.signals_gated, 1)
        self.assertEqual(self.broker.buy_orders(), [],
                         "a live-money entry must never be priced from indicative data")

    def test_stale_indicative_is_still_refused_in_paper(self):
        """The allowance relaxes PROVENANCE only. Staleness is a separate risk and
        must still fail closed."""
        from smc.broker import FEED_INDICATIVE, Quote
        stale_ts = (self.clock.now() - dt.timedelta(seconds=120)).isoformat()
        self.broker.quotes[OCC] = Quote(OCC, 0.50, 0.52, 50, 50, stale_ts, FEED_INDICATIVE)
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(report.signals_gated, 1)
        self.assertEqual(self.broker.buy_orders(), [])
        self.assertTrue(any("fresh timestamp" in n for n in report.notes))

    def test_one_sided_indicative_is_still_refused_in_paper(self):
        from smc.broker import FEED_INDICATIVE
        self.broker.set_quote(OCC, bid=0.0, ask=0.52, feed=FEED_INDICATIVE)
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(report.signals_gated, 1)
        self.assertEqual(self.broker.buy_orders(), [])

    def test_missing_opra_timestamp_fails_closed(self):
        from smc.broker import FEED_OPRA, Quote
        self.broker.quotes[OCC] = Quote(OCC, 0.50, 0.52, 50, 50, None, FEED_OPRA)
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(report.signals_gated, 1)
        self.assertEqual(self.broker.buy_orders(), [])

    def test_entry_limit_is_repriced_from_fresh_opra_not_selected_indicative_ask(self):
        self.broker.fill_policy = FILL_NEVER
        report = self._pipeline(selected_ask=0.80).run_signal_cycle(arm=True)
        self.assertEqual(report.entries_submitted, 1)
        self.assertEqual(self.broker.buy_orders()[0]["limit_price"], "0.52")

    # ------------------------------------------------- latency telemetry (re-arm gate)
    def test_latency_legs_are_recorded_for_a_submitted_entry(self):
        """The re-arm gate requires MEASURED signal-to-quote, quote-to-decision and
        broker-ack latency -- not one aggregate number."""
        self.broker.fill_policy = FILL_NEVER
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(len(report.latency_legs), 1)
        leg = report.latency_legs[0]
        for key in ("signal_to_quote_s", "quote_to_decision_s",
                     "decision_to_ack_s", "signal_to_ack_s"):
            self.assertIsNotNone(leg[key], f"{key} must be measured")
            self.assertGreaterEqual(leg[key], 0.0)
        self.assertEqual(leg["quote_feed"], "opra")
        d = report.as_dict()
        self.assertIn("latency_signal_to_ack_s", d)
        self.assertEqual(d["latency_signal_to_ack_s"]["n"], 1)

    def test_latency_leg_recorded_even_when_quote_gate_rejects(self):
        """A gated signal must still produce a signal-to-quote measurement, or a
        shadow session where everything is gated would yield no telemetry at all --
        which is exactly the situation an unentitled OPRA feed creates."""
        from smc.broker import FEED_INDICATIVE
        self.config = make_config(self.tmp, allow_indicative_entry_quotes_in_paper=False)
        self.broker.set_quote(OCC, bid=0.50, ask=0.52, feed=FEED_INDICATIVE)
        report = self._pipeline().run_signal_cycle(arm=True)
        self.assertEqual(report.signals_gated, 1)
        self.assertEqual(len(report.latency_legs), 1)
        self.assertIsNotNone(report.latency_legs[0]["signal_to_quote_s"])

class SupervisionThrottleTest(_Base):
    """Reconciliation on EVERY supervision pass measured 3 broker calls/pass, which
    at 1Hz with 2 open positions is ~360 req/min vs Alpaca's ~200/min ceiling -- the
    protective path would start erroring under the load it exists to handle."""

    def _pipeline(self):
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig
        return SmcPipeline(self.store, self.broker, self.config, detect_fn=lambda: [],
                            select_fn=lambda t: None, exit_config=ExitConfig(),
                            dashboard_db=self.dashboard, clock=self.clock)

    def _count_position_reads(self, pipeline, passes):
        calls = {"n": 0}
        original = self.broker.list_option_positions
        self.broker.list_option_positions = lambda: (
            calls.__setitem__("n", calls["n"] + 1) or original())
        try:
            for _ in range(passes):
                pipeline.supervise_once(arm=False)
                self.clock.sleep(self.config.supervisor_poll_seconds)
        finally:
            self.broker.list_option_positions = original
        return calls["n"]

    def test_first_supervision_pass_always_reconciles(self):
        """A restart must never begin by trusting local state."""
        p = self._pipeline()
        self.assertTrue(p._reconcile_due())
        r = p.supervise_once(arm=False)
        self.assertTrue(r.reconciled)

    def test_reconciliation_is_throttled_across_rapid_passes(self):
        cfg = make_config(self.tmp, reconcile_interval_seconds=15.0,
                          supervisor_poll_seconds=1.0)
        self.config = cfg
        p = self._pipeline()
        reads = self._count_position_reads(p, passes=10)
        self.assertEqual(reads, 1,
                         f"10 passes inside one 15s window must reconcile once, got {reads}")

    def test_reconciliation_resumes_after_the_interval_elapses(self):
        import time as _time
        cfg = make_config(self.tmp, reconcile_interval_seconds=0.0001,
                          supervisor_poll_seconds=1.0)
        self.config = cfg
        p = self._pipeline()
        p.supervise_once(arm=False)
        _time.sleep(0.01)  # real monotonic time, the throttle does not use the fake clock
        self.assertTrue(p._reconcile_due(),
                        "throttle must expire so orphans are still caught")

    def test_signal_cycle_always_reconciles_regardless_of_throttle(self):
        """The throttle is supervision-only. No entry may ever be submitted against
        unreconciled state."""
        from smc.pipeline import SmcPipeline
        from smc.tests.harness import make_contract, make_trigger
        from thetadata_pipeline.bt2_exits import ExitConfig
        p = SmcPipeline(
            self.store, self.broker, self.config,
            detect_fn=lambda: [make_trigger(signal_ts=self.clock.now().isoformat())],
            select_fn=lambda t: make_contract(), exit_config=ExitConfig(),
            dashboard_db=self.dashboard, clock=self.clock)
        p.supervise_once(arm=False)          # consumes the throttle window
        r = p.run_signal_cycle(arm=False)
        self.assertTrue(r.reconciled, "signal cycle must reconcile even when throttled")


class OpraCacheTest(unittest.TestCase):
    """OPRA is permanently unavailable on this account (403 "agreement is not
    signed"), so an uncached negative probe means every quote -- including every
    sub-second urgent-exit poll -- pays a doomed round-trip."""

    def _broker(self, reprobe=600.0):
        from smc.broker import AlpacaBroker
        b = AlpacaBroker.__new__(AlpacaBroker)   # skip __init__/network
        b._opra_available = None
        b._opra_checked_at = None
        b._opra_reprobe_seconds = reprobe
        return b

    def test_negative_probe_is_cached_so_opra_is_not_retried(self):
        from smc.broker import FEED_INDICATIVE, FEED_OPRA
        import time as _time
        b = self._broker(reprobe=600.0)
        self.assertEqual(b._feed_order(), (FEED_OPRA, FEED_INDICATIVE))
        b._opra_available = False
        b._opra_checked_at = _time.monotonic()
        self.assertEqual(b._feed_order(), (FEED_INDICATIVE,),
                         "a fresh negative probe must skip the doomed OPRA call")

    def test_negative_cache_expires_so_a_new_entitlement_is_picked_up(self):
        from smc.broker import FEED_INDICATIVE, FEED_OPRA
        import time as _time
        b = self._broker(reprobe=0.0001)
        b._opra_available = False
        b._opra_checked_at = _time.monotonic()
        _time.sleep(0.01)
        self.assertEqual(b._feed_order(), (FEED_OPRA, FEED_INDICATIVE),
                         "TTL must expire so signing OPRA takes effect without a restart")

    def test_positive_probe_keeps_opra_preferred(self):
        from smc.broker import FEED_INDICATIVE, FEED_OPRA
        import time as _time
        b = self._broker()
        b._opra_available = True
        b._opra_checked_at = _time.monotonic()
        self.assertEqual(b._feed_order(), (FEED_OPRA, FEED_INDICATIVE))


class OwnershipTest(_Base):
    def test_two_overlapping_signals_on_same_occ_only_one_enters(self):
        """Required test 6 -- the exact 2026-07-31 bug: three signals chose the same
        contract and the OCC-keyed dict overwrote the earlier records."""
        self.broker.fill_policy = FILL_IMMEDIATE

        first = submit_entry(
            self.store, self.broker, self.config, signal_key="sig-A", occ=OCC,
            underlying="QQQ", contract_right="C", signal_side="long", intended_qty=1,
            limit_price=0.52, signal_ts=self.clock.now().isoformat(), arm=True,
            clock=self.clock)
        self.assertEqual(first.state, OPEN)

        allowed, reason = assert_occ_free_for_entry(self.store, self.broker, OCC,
                                                     self.dashboard)
        self.assertFalse(allowed, "the second signal must be blocked on the same OCC")
        self.assertIn("already has a non-terminal position", reason)

        gate = check_entry_allowed(self.store, self.broker, self.config, occ=OCC,
                                    entry_premium=0.52, intended_qty=1,
                                    dashboard_db=self.dashboard,
                                    now_et=dt.datetime(2026, 7, 31, 11, 0, tzinfo=ET),
                                    schedule=_regular_schedule())
        self.assertFalse(gate.allowed)

    def test_occ_owned_by_another_strategy_blocks_entry(self):
        """Required test 7. The account is shared; another strategy's leg on the same
        OCC must block SMC, and SMC must never assume the broker qty is its own."""
        add_foreign_position(self.dashboard, "S4_IRON_CONDOR", OCC)
        allowed, reason = assert_occ_free_for_entry(self.store, self.broker, OCC,
                                                     self.dashboard)
        self.assertFalse(allowed)
        self.assertIn("owned by another strategy", reason)
        self.assertIn("S4_IRON_CONDOR", reason)

    def test_shared_occ_flags_mismatch_and_refuses_to_claim_broker_qty(self):
        self._open_position(occ=OCC, qty=1)
        add_foreign_position(self.dashboard, "S7_CALENDAR", OCC)
        self.broker.set_position(OCC, 5)  # 1 ours? 4 theirs? unknowable

        result = reconcile(self.store, self.broker, self.config, self.dashboard)

        self.assertTrue(result.ambiguous)
        self.assertIn("S7_CALENDAR", result.ambiguous[0]["foreign_strategies"])
        pos = [p for p in self.store.positions_in_mismatch()]
        self.assertTrue(pos, "an ambiguous OCC must be flagged RECON_MISMATCH")
        self.assertIsNotNone(self.store.active_halt())

    def test_unreadable_dashboard_blocks_entry_rather_than_assuming_no_owners(self):
        missing = self.tmp / "does_not_exist.db"
        allowed, reason = assert_occ_free_for_entry(self.store, self.broker, OCC, missing)
        self.assertFalse(allowed)
        self.assertIn("cannot verify foreign ownership", reason)

    def test_foreign_owners_excludes_smc_itself(self):
        add_foreign_position(self.dashboard, "SMC_TRIANGLE", OCC)
        mapping, readable = foreign_occ_owners(self.dashboard)
        self.assertTrue(readable)
        self.assertNotIn(OCC, mapping, "SMC's own rows are not 'foreign'")

    def test_non_smc_underlying_orphan_is_not_our_business(self):
        """NCLH/PSKY legs live in this same account. They must NOT trigger an SMC
        halt -- only exposure on an underlying SMC actually trades."""
        self.broker.set_position("NCLH260731C00020500", 3)
        result = reconcile(self.store, self.broker, self.config, self.dashboard)
        self.assertFalse(result.orphans, "another strategy's symbol must not be flagged as ours")


    def test_unattributed_working_qqq_order_halts_before_it_can_fill_later(self):
        self.broker.fill_policy = FILL_NEVER
        self.broker.submit_order({
            "symbol": OCC, "qty": "1", "side": "buy", "type": "limit",
            "limit_price": "0.50", "client_order_id": "foreign-unknown-order"})
        result = reconcile(self.store, self.broker, self.config, self.dashboard)
        self.assertTrue(result.orphan_orders)
        self.assertIsNotNone(self.store.active_halt())



# ================================================================ scenario 18
class QuantityMismatchTest(_Base):
    def test_broker_qty_greater_than_local_is_adopted_as_authoritative(self):
        pos = self._open_position(qty=1)
        self.broker.set_position(OCC, 3)  # broker says 3, we thought 1

        reconcile(self.store, self.broker, self.config, self.dashboard)

        refreshed = self.store.get_position(pos["position_id"])
        self.assertEqual(refreshed["filled_qty"], 3,
                         "broker quantity is authoritative and must overwrite local")

    def test_broker_flat_while_local_open_is_a_loud_mismatch(self):
        pos = self._open_position(qty=2)
        self.broker.set_position(OCC, 0)

        result = reconcile(self.store, self.broker, self.config, self.dashboard)

        self.assertEqual(self.store.get_position(pos["position_id"])["state"], RECON_MISMATCH)
        self.assertTrue(result.halt_reasons)
        self.assertIsNotNone(self.store.active_halt())

    def test_exit_sizes_off_reconciled_quantity(self):
        pos = self._open_position(qty=1)
        self.broker.set_position(OCC, 3)
        reconcile(self.store, self.broker, self.config, self.dashboard)
        refreshed = self.store.get_position(pos["position_id"])
        self.broker.set_quote(OCC, bid=0.60, ask=0.65)
        self.broker.fill_policy = FILL_IMMEDIATE
        execute_exit(self.store, self.broker, self.config, refreshed, EXIT_STOP,
                     arm=True, clock=self.clock)
        self.assertEqual(int(self.broker.sell_orders()[0]["qty"]), 3)


    def test_recon_mismatch_remains_in_protective_supervision(self):
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig
        pos = self._open_position(entry_price=0.80)
        self.store.set_position_state(pos["position_id"], RECON_MISMATCH, "test ambiguity")
        self.broker.set_quote(OCC, bid=0.60, ask=0.62)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60
        pipeline = SmcPipeline(
            self.store, self.broker, self.config, detect_fn=lambda: [],
            select_fn=lambda trigger: None, exit_config=ExitConfig(),
            dashboard_db=self.dashboard, clock=self.clock)
        report = pipeline.supervise_once(arm=True)
        self.assertEqual(report.exits_executed, 1)
        self.assertEqual(self.broker.positions[OCC], 0)
        self.assertEqual(self.store.get_position(pos["position_id"])["state"], CLOSED)


# ================================================================ scenario 15
class CorruptStateTest(_Base):
    def test_corrupt_database_raises_and_never_reads_as_no_positions(self):
        """Required test 15. The single most dangerous silent failure: a broken
        control plane that looks like an empty one."""
        self.store.close()
        Path(self.config.db_path).write_bytes(b"this is not a sqlite database at all")

        with self.assertRaises(SmcStateError) as ctx:
            SmcStateStore(self.config.db_path)
        self.assertIn("Halting new entries", str(ctx.exception))
        self.store = make_store(make_config(self.tmp / "fresh")) if False else None

    def tearDown(self):
        if getattr(self, "store", None) is not None:
            try:
                self.store.close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_query_failure_raises_rather_than_returning_empty(self):
        store = self.store
        store.conn.execute("DROP TABLE smc_positions")
        with self.assertRaises(SmcStateError):
            store.open_positions()

    def test_supervision_halts_loudly_when_state_unreadable(self):
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig

        pipeline = SmcPipeline(self.store, self.broker, self.config, detect_fn=lambda: [],
                                select_fn=lambda t: None, exit_config=ExitConfig(),
                                dashboard_db=self.dashboard, clock=self.clock)
        self.store.conn.execute("DROP TABLE smc_positions")
        report = pipeline.supervise_once(arm=True)
        self.assertTrue(report.halted)
        self.assertTrue(report.halt_reason)

    def test_failed_startup_reconciliation_blocks_detection_and_submission(self):
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig
        calls = {"detect": 0}
        def detect():
            calls["detect"] += 1
            return []
        self.broker.position_read_timeout = True
        pipeline = SmcPipeline(
            self.store, self.broker, self.config, detect_fn=detect,
            select_fn=lambda trigger: None, exit_config=ExitConfig(),
            dashboard_db=self.dashboard, clock=self.clock)
        report = pipeline.run_signal_cycle(arm=True)
        self.assertTrue(report.halted)
        self.assertEqual(calls["detect"], 0)
        self.assertFalse(self.broker.submitted)

# ================================================================ scenario 17
class EarlyCloseTest(unittest.TestCase):
    """The old code hard-coded 15:30 ET as forced-close. On a 13:00 early close that
    is 2.5 HOURS after the venue shut -- a 0DTE long would have been impossible to
    exit and would expire against settlement."""

    def setUp(self):
        self.tmp = make_tmpdir()
        self.config = make_config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sched(self, close_time, degraded=False, trading=True):
        d = dt.date(2026, 11, 27)
        return SessionSchedule(
            session_date=d, is_trading_day=trading,
            open_et=dt.datetime.combine(d, dt.time(9, 30), tzinfo=ET),
            close_et=dt.datetime.combine(d, close_time, tzinfo=ET) if trading else None,
            is_early_close=close_time < dt.time(16, 0), degraded=degraded, source="test")

    def test_early_close_flattens_well_before_1300(self):
        sched = self._sched(dt.time(13, 0))
        now = dt.datetime.combine(sched.session_date, dt.time(12, 40), tzinfo=ET)
        should, reason = must_flatten(now, sched, self.config)
        self.assertTrue(should, "must flatten before a 13:00 early close")
        self.assertIn("early-close", reason)

    def test_early_close_does_not_flatten_too_early_in_the_morning(self):
        sched = self._sched(dt.time(13, 0))
        now = dt.datetime.combine(sched.session_date, dt.time(10, 0), tzinfo=ET)
        should, _ = must_flatten(now, sched, self.config)
        self.assertFalse(should)

    def test_hardcoded_1530_would_have_been_after_the_early_close(self):
        """Documents the actual defect: the old constant sits past the venue close."""
        sched = self._sched(dt.time(13, 0))
        old_constant = dt.datetime.combine(sched.session_date, dt.time(15, 30), tzinfo=ET)
        self.assertGreater(old_constant, sched.close_et)
        should, _ = must_flatten(old_constant, sched, self.config)
        self.assertTrue(should)

    def test_regular_session_flattens_at_1530(self):
        sched = self._sched(dt.time(16, 0))
        now = dt.datetime.combine(sched.session_date, dt.time(15, 30), tzinfo=ET)
        should, reason = must_flatten(now, sched, self.config)
        self.assertTrue(should)
        self.assertIn("regular-close", reason)

    def test_degraded_schedule_flattens_even_earlier(self):
        """An unconfirmed calendar must fail toward flattening, never toward assuming
        a full session."""
        sched = self._sched(dt.time(16, 0), degraded=True)
        now = dt.datetime.combine(sched.session_date, dt.time(15, 20), tzinfo=ET)
        should, reason = must_flatten(now, sched, self.config)
        self.assertTrue(should)
        self.assertIn("DEGRADED", reason)

    def test_non_trading_day_always_flattens(self):
        sched = self._sched(dt.time(16, 0), trading=False)
        now = dt.datetime.combine(sched.session_date, dt.time(11, 0), tzinfo=ET)
        should, _ = must_flatten(now, sched, self.config)
        self.assertTrue(should)

    def test_weekend_resolves_as_non_trading_without_network(self):
        sched = session_schedule(dt.date(2026, 8, 1), sessions={})  # a Saturday
        self.assertFalse(sched.is_trading_day)
        self.assertFalse(sched.degraded)

    def test_unknown_weekday_is_degraded_not_silently_assumed(self):
        sched = session_schedule(dt.date(2026, 8, 3), sessions={})
        self.assertTrue(sched.is_trading_day)
        self.assertTrue(sched.degraded, "an unverified session must be marked degraded")

    def test_real_calendar_row_is_used_when_present(self):
        sessions = {"2026-11-27": {"open": "09:30", "close": "13:00"}}
        sched = session_schedule(dt.date(2026, 11, 27), sessions=sessions)
        self.assertTrue(sched.is_early_close)
        self.assertFalse(sched.degraded)
        self.assertEqual(sched.close_et.time(), dt.time(13, 0))


# ================================================================ Phase 6 gates
class RiskGateTest(_Base):
    def _gate(self, config=None, occ=OTHER_OCC, premium=0.50, qty=1):
        return check_entry_allowed(
            self.store, self.broker, config or self.config, occ=occ,
            entry_premium=premium, intended_qty=qty, dashboard_db=self.dashboard,
            now_et=dt.datetime(2026, 7, 31, 11, 0, tzinfo=ET), schedule=_regular_schedule())

    def test_clean_state_allows_entry(self):
        self.assertTrue(self._gate().allowed)

    def test_daily_loss_limit_blocks(self):
        cfg = make_config(self.tmp, max_daily_realized_loss=50.0)
        pos = self._open_position(signal_key="loser:1")
        self.store.record_position_closed(pos["position_id"], exit_fill_price=0.10,
                                          closed_qty=1, realized_pnl=-80.0,
                                          exit_reason=EXIT_STOP)
        gate = self._gate(cfg)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("daily realized loss" in r for r in gate.reasons))

    def test_max_open_premium_blocks(self):
        cfg = make_config(self.tmp, max_open_premium_at_risk=100.0)
        self._open_position(qty=1, entry_price=0.80, signal_key="prem:1")
        gate = self._gate(cfg, premium=0.90, qty=1)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("open premium" in r for r in gate.reasons))

    def test_max_concurrent_positions_blocks(self):
        cfg = make_config(self.tmp, max_concurrent_positions=1)
        self._open_position(signal_key="conc:1")
        gate = self._gate(cfg)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("concurrent" in r for r in gate.reasons))

    def test_correlated_qqq_exposure_blocks(self):
        cfg = make_config(self.tmp, max_correlated_qqq_contracts=1)
        self._open_position(qty=1, signal_key="corr:1")
        gate = self._gate(cfg)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("correlated QQQ" in r for r in gate.reasons))

    def test_entry_rate_limit_blocks(self):
        cfg = make_config(self.tmp, max_entries_per_window=1, entry_window_minutes=30)
        self._open_position(signal_key="rate:1")
        gate = self._gate(cfg)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("rate limited" in r for r in gate.reasons))

    def test_consecutive_loss_breaker_blocks_and_resets_on_a_win(self):
        cfg = make_config(self.tmp, max_consecutive_losses=2)
        for i, pnl in enumerate([-10.0, -12.0]):
            p = self._open_position(signal_key=f"streak:{i}", occ=f"QQQ2607{i}1C00690000")
            self.store.record_position_closed(p["position_id"], exit_fill_price=0.1,
                                              closed_qty=1, realized_pnl=pnl,
                                              exit_reason=EXIT_STOP)
        self.assertEqual(consecutive_losses(self.store), 2)
        self.assertFalse(self._gate(cfg).allowed)

        win = self._open_position(signal_key="streak:win", occ="QQQ260791C00690000")
        self.store.record_position_closed(win["position_id"], exit_fill_price=1.5,
                                          closed_qty=1, realized_pnl=40.0,
                                          exit_reason="TARGET")
        self.assertEqual(consecutive_losses(self.store), 0, "a win must reset the streak")

    def test_execution_failure_breaker_blocks(self):
        from smc.risk import record_execution_failure
        cfg = make_config(self.tmp, max_execution_failures=2)
        record_execution_failure(self.store, "submit blew up")
        record_execution_failure(self.store, "cancel blew up")
        gate = self._gate(cfg)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("execution failures" in r for r in gate.reasons))

    def test_active_halt_blocks_everything(self):
        self.store.set_halt("manual halt for test")
        gate = self._gate()
        self.assertFalse(gate.allowed)
        self.assertTrue(any("ACTIVE HALT" in r for r in gate.reasons))

    def test_recon_mismatch_blocks_entries(self):
        pos = self._open_position(signal_key="mm:1")
        self.store.set_position_state(pos["position_id"], RECON_MISMATCH, "test mismatch")
        gate = self._gate()
        self.assertFalse(gate.allowed)
        self.assertTrue(any("RECON_MISMATCH" in r for r in gate.reasons))

    def test_gates_never_block_an_exit(self):
        """Withhold-only: a fully halted, breaker-tripped strategy must STILL be able
        to reduce risk."""
        pos = self._open_position(entry_price=0.80)
        self.store.set_halt("everything is halted")
        self.broker.set_quote(OCC, bid=0.60, ask=0.62)
        self.broker.fill_policy = FILL_IMMEDIATE
        self.broker.fill_price = 0.60
        outcome = execute_exit(self.store, self.broker, self.config, pos, EXIT_STOP,
                                arm=True, clock=self.clock)
        self.assertEqual(outcome.state, CLOSED)
        self.assertEqual(self.broker.positions[OCC], 0)

    def test_flatten_watchdog_is_independent_of_exit_rules(self):
        self._open_position()
        should, reason, positions = flatten_watchdog(
            self.store, self.config,
            now_et=dt.datetime(2026, 7, 31, 15, 45, tzinfo=ET),
            schedule=_regular_schedule())
        self.assertTrue(should)
        self.assertEqual(len(positions), 1)

    def test_all_blocking_reasons_are_reported_together(self):
        cfg = make_config(self.tmp, max_concurrent_positions=0, max_entries_per_window=0)
        gate = self._gate(cfg)
        self.assertFalse(gate.allowed)
        self.assertGreaterEqual(len(gate.reasons), 2,
                                "operator must see every reason, not just the first")

    def test_entry_halt_does_not_stop_the_supervisor_loop(self):
        from smc.pipeline import SmcPipeline
        from thetadata_pipeline.bt2_exits import ExitConfig
        self.broker.set_position(OCC, 1)  # orphan => entry halt on every pass
        pipeline = SmcPipeline(
            self.store, self.broker, self.config, detect_fn=lambda: [],
            select_fn=lambda trigger: None, exit_config=ExitConfig(),
            dashboard_db=self.dashboard, clock=self.clock)
        report = pipeline.supervise_for(3.0, arm=False)
        self.assertTrue(report.halted)
        self.assertGreaterEqual(self.clock.slept, 3.0)



def _regular_schedule():
    d = dt.date(2026, 7, 31)
    return SessionSchedule(
        session_date=d, is_trading_day=True,
        open_et=dt.datetime.combine(d, dt.time(9, 30), tzinfo=ET),
        close_et=dt.datetime.combine(d, dt.time(16, 0), tzinfo=ET),
        is_early_close=False, degraded=False, source="test")


if __name__ == "__main__":
    unittest.main()
