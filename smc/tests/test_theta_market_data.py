import datetime as dt
import unittest

import pandas as pd

from smc.theta_market_data import ThetaMarketDataCache


class SubscriptionUniverseTests(unittest.TestCase):
    """Streaming the whole chain saturated the single-core box: 212
    reconnects, 0/1170 subscriptions acked, nothing tradeable."""

    def _cache(self, spot=580.0, strikes=range(500, 660), **kw):
        cache = ThetaMarketDataCache(**kw)
        rows = [{"expiration": __import__("datetime").date(2026, 8, 3),
                 "strike": float(s), "right": r, "bid": 0.5, "ask": 0.6,
                 "underlying_price": spot}
                for s in strikes for r in ("C", "P")]
        entry = type("E", (), {"book": pd.DataFrame(rows)})()
        cache._entries = {"e": entry}
        return cache

    def test_full_chain_is_narrowed_to_the_money(self):
        full = len(self._cache(subscription_strikes_per_side=0).candidate_occs())
        narrowed = len(self._cache().candidate_occs())
        self.assertEqual(full, 320)
        self.assertLess(narrowed, full)
        self.assertGreater(narrowed, 0)

    def test_kept_strikes_straddle_spot(self):
        occs = self._cache().candidate_occs()
        strikes = sorted({int(o[-8:]) / 1000.0 for o in occs})
        self.assertLess(min(strikes), 580.0)
        self.assertGreater(max(strikes), 580.0)

    def test_band_bounds_the_distance_from_spot(self):
        occs = self._cache(subscription_strikes_per_side=999,
                           subscription_band_pct=0.01).candidate_occs()
        strikes = {int(o[-8:]) / 1000.0 for o in occs}
        self.assertTrue(all(abs(s - 580.0) <= 5.81 for s in strikes), sorted(strikes)[:5])

    def test_fails_open_when_spot_is_unknown(self):
        """A universe narrowed around an unknown centre could silently
        exclude the money -- worse than a wide one."""
        cache = self._cache(spot=float("nan"))
        for e in cache._entries.values():
            e.book["underlying_price"] = None
        self.assertEqual(len(cache.candidate_occs()), 320)

    def test_disabled_returns_the_whole_chain(self):
        self.assertEqual(
            len(self._cache(subscription_strikes_per_side=0).candidate_occs()), 320)

import pandas as pd

from smc.theta_market_data import (
    ThetaMarketDataCache, build_occ_symbol, parse_occ_symbol,
)


class ParseOccSymbolTests(unittest.TestCase):
    def test_round_trip_call(self):
        occ = build_occ_symbol("QQQ", dt.date(2026, 8, 3), 605.0, "C")
        self.assertEqual(occ, "QQQ260803C00605000")
        parsed = parse_occ_symbol(occ)
        self.assertEqual(parsed["root"], "QQQ")
        self.assertEqual(parsed["expiration"], dt.date(2026, 8, 3))
        self.assertEqual(parsed["strike"], 605.0)
        self.assertEqual(parsed["right"], "C")

    def test_round_trip_put_fractional_strike(self):
        occ = build_occ_symbol("QQQ", dt.date(2026, 8, 3), 604.5, "P")
        parsed = parse_occ_symbol(occ)
        self.assertEqual(parsed["strike"], 604.5)
        self.assertEqual(parsed["right"], "P")

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            parse_occ_symbol("not-an-occ-symbol")


class MergeTests(unittest.TestCase):
    def test_merge_joins_quotes_and_greeks_on_strike_right_expiration(self):
        quotes = pd.DataFrame([
            {"timestamp": "2026-07-31T16:14:18-04:00", "symbol": "QQQ", "expiration": dt.date(2026, 8, 3),
             "strike": 605.0, "right": "CALL", "bid_size": 9, "bid": 0.50, "ask_size": 11, "ask": 0.55},
        ])
        greeks = pd.DataFrame([
            {"symbol": "QQQ", "expiration": dt.date(2026, 8, 3), "strike": 605.0, "right": "CALL",
             "delta": 0.35, "underlying_price": 620.0},
        ])
        merged = ThetaMarketDataCache._merge(quotes, greeks)
        self.assertEqual(len(merged), 1)
        row = merged.iloc[0]
        self.assertEqual(row["right"], "C")
        self.assertEqual(row["delta"], 0.35)
        self.assertEqual(row["bid"], 0.50)

    def test_merge_without_greeks_gives_none_delta(self):
        quotes = pd.DataFrame([
            {"timestamp": "2026-07-31T16:14:18-04:00", "symbol": "QQQ", "expiration": dt.date(2026, 8, 3),
             "strike": 605.0, "right": "PUT", "bid_size": 1, "bid": 0.10, "ask_size": 1, "ask": 0.12},
        ])
        merged = ThetaMarketDataCache._merge(quotes, None)
        self.assertIsNone(merged.iloc[0]["delta"])


class CacheStructureTests(unittest.TestCase):
    def test_target_expirations_returns_sorted_unique_dates(self):
        cache = ThetaMarketDataCache(symbol="QQQ", dte_targets=(0, 1, 2))
        today = dt.date(2026, 8, 3)  # a Monday
        exps = cache.target_expirations(today)
        self.assertEqual(exps, sorted(set(exps)))
        self.assertEqual(exps[0], today)
        self.assertEqual(len(exps), 3)

    def test_get_book_empty_when_nothing_cached(self):
        cache = ThetaMarketDataCache()
        book = cache.get_book("C")
        self.assertEqual(len(book), 0)
        self.assertIn("quote_age_seconds", book.columns)

    def test_get_quote_row_returns_none_for_uncached_occ(self):
        cache = ThetaMarketDataCache()
        self.assertIsNone(cache.get_quote_row("QQQ260803C00605000"))

    def test_cache_status_empty_before_any_refresh(self):
        cache = ThetaMarketDataCache()
        self.assertEqual(cache.cache_status(), {})


if __name__ == "__main__":
    unittest.main()
