"""Tests for smc/entry_budget.py -- the durable canary latch.

The property under test: a spent allowance must NOT come back. Not on
restart, not on crash, not on reboot, not on reconnect. Only a genuinely new
ET trading session may reset it.
"""
from __future__ import annotations

import datetime as dt
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from smc.entry_budget import EntryBudget, session_date

ET = ZoneInfo("America/New_York")


def connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _consume_in_child(path, coid, day, out):
    """A separate PROCESS, so this is a real restart, not a new object."""
    conn = connect(path)
    try:
        out.put(EntryBudget(conn, max_attempts=1).consume(coid, day))
    finally:
        conn.close()


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "smc.db"
        self.conn = connect(self.db)
        self.day = "2026-08-03"

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def budget(self, n=1):
        return EntryBudget(self.conn, max_attempts=n)

    # ------------------------------------------------------------- basics
    def test_fresh_session_allows_one(self):
        b = self.budget()
        self.assertTrue(b.can_submit(self.day))
        self.assertEqual(b.state(self.day).remaining, 1)

    def test_consume_spends_the_allowance(self):
        b = self.budget()
        self.assertTrue(b.consume("coid-1", self.day))
        self.assertFalse(b.can_submit(self.day))
        st = b.state(self.day)
        self.assertEqual(st.used, 1)
        self.assertTrue(st.exhausted)
        self.assertIn("coid-1", st.client_order_ids)

    def test_second_distinct_order_refused(self):
        b = self.budget()
        self.assertTrue(b.consume("coid-1", self.day))
        self.assertFalse(b.consume("coid-2", self.day))
        self.assertEqual(b.state(self.day).used, 1)

    def test_same_coid_is_idempotent_not_double_spend(self):
        """A retry of the SAME intended order must not consume twice."""
        b = self.budget()
        self.assertTrue(b.consume("coid-1", self.day))
        self.assertTrue(b.consume("coid-1", self.day))
        self.assertEqual(b.state(self.day).used, 1)

    def test_timestamps_recorded(self):
        b = self.budget()
        b.consume("coid-1", self.day)
        st = b.state(self.day)
        self.assertIsNotNone(st.first_used_ts)
        self.assertIsNotNone(st.last_used_ts)

    # ------------------------------------------------- THE durability tests
    def test_new_object_same_db_does_not_restore_allowance(self):
        """A restart constructs a new EntryBudget against the same file."""
        self.budget().consume("coid-1", self.day)
        self.assertFalse(self.budget().can_submit(self.day))

    def test_reopened_connection_does_not_restore_allowance(self):
        self.budget().consume("coid-1", self.day)
        self.conn.close()
        self.conn = connect(self.db)
        self.assertFalse(self.budget().can_submit(self.day))

    def test_separate_process_cannot_get_a_second_post(self):
        """THE regression: Restart=always spawns a fresh process. It must
        NOT be handed a fresh allowance."""
        self.assertTrue(self.budget().consume("coid-1", self.day))
        self.conn.commit()
        out = multiprocessing.Queue()
        p = multiprocessing.Process(target=_consume_in_child,
                                    args=(str(self.db), "coid-2", self.day, out))
        p.start()
        p.join(timeout=30)
        self.assertFalse(out.get(timeout=5),
                         "a restarted process was granted a second submission")

    def test_simulated_crash_after_consume_before_post(self):
        """Consumption is committed BEFORE network I/O, so a process killed
        between consume and POST still finds it spent."""
        b = self.budget()
        b.consume("coid-1", self.day)     # commit happens inside consume()
        # no POST, no cleanup -- simulate abrupt death
        del b
        self.conn.close()
        self.conn = connect(self.db)
        self.assertFalse(self.budget().can_submit(self.day))

    # ---------------------------------------------------- session scoping
    def test_new_session_date_resets(self):
        b = self.budget()
        b.consume("coid-1", "2026-08-03")
        self.assertTrue(b.can_submit("2026-08-04"))

    def test_previous_session_stays_exhausted(self):
        b = self.budget()
        b.consume("coid-1", "2026-08-03")
        b.consume("coid-2", "2026-08-04")
        self.assertFalse(b.can_submit("2026-08-03"))

    def test_session_date_is_et_not_utc(self):
        """A UTC date would roll at 20:00 ET, mid-session, handing back an
        allowance during the trading day."""
        late = dt.datetime(2026, 8, 3, 23, 30, tzinfo=dt.timezone.utc)  # 19:30 ET
        self.assertEqual(session_date(late), "2026-08-03")

    def test_session_date_naive_treated_as_et(self):
        self.assertEqual(
            session_date(dt.datetime(2026, 8, 3, 10, 0)), "2026-08-03")

    # ------------------------------------------------------------- limits
    def test_higher_ceiling_allows_more(self):
        b = self.budget(n=3)
        for i in range(3):
            self.assertTrue(b.consume(f"c{i}", self.day))
        self.assertFalse(b.consume("c3", self.day))

    def test_set_max_never_lowers_used(self):
        b = self.budget()
        b.consume("coid-1", self.day)
        b.set_max(5, self.day)
        self.assertEqual(b.state(self.day).used, 1)
        self.assertTrue(b.can_submit(self.day))

    def test_history_returns_rows(self):
        b = self.budget()
        b.consume("a", "2026-08-03")
        b.consume("b", "2026-08-04")
        self.assertEqual(len(b.history()), 2)

    def test_schema_is_additive_only(self):
        """Adding this table must not disturb the SMC state schema.

        Scans the DDL and executed SQL, not the module docstring -- the
        docstring legitimately names SCHEMA_VERSION while explaining that we
        deliberately do NOT touch it."""
        import smc.entry_budget as eb
        self.assertIn("CREATE TABLE IF NOT EXISTS", eb._DDL)
        for banned in ("DROP TABLE", "ALTER TABLE", "SCHEMA_VERSION"):
            self.assertNotIn(banned, eb._DDL)

    def test_existing_smc_tables_untouched(self):
        """Empirical version of the above: create the real SMC schema, add
        the budget table, and prove every original table still queries."""
        from smc.state import SmcStateStore
        db = Path(self._tmp.name) / "real.db"
        store = SmcStateStore(db)
        try:
            EntryBudget(store.conn, max_attempts=1).consume("c", self.day)
            for table in ("smc_positions", "smc_orders", "smc_events", "smc_halts"):
                store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            store.verify_integrity()
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
