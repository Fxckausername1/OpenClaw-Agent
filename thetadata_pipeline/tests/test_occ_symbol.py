import datetime as dt
import unittest

from thetadata_pipeline.schemas import occ_symbol


class OccSymbolTests(unittest.TestCase):
    def test_known_value(self):
        self.assertEqual(
            occ_symbol("SPY", dt.date(2026, 7, 24), 750.0, "C"),
            "SPY260724C00750000",
        )

    def test_put_and_fractional_strike(self):
        self.assertEqual(
            occ_symbol("QQQ", dt.date(2026, 7, 31), 689.5, "PUT"),
            "QQQ260731P00689500",
        )

    def test_matches_journal_saves_own_validation_regex(self):
        import re
        OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
        self.assertTrue(OCC_RE.match(occ_symbol("SPY", dt.date(2026, 7, 24), 750.0, "C")))

    def test_out_of_range_strike_raises(self):
        with self.assertRaises(ValueError):
            occ_symbol("SPY", dt.date(2026, 7, 24), 999999.999, "C")


if __name__ == "__main__":
    unittest.main()
