import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("macro_news_pull.py")
SPEC = importlib.util.spec_from_file_location("macro_news_pull", MODULE_PATH)
mn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mn)


class MacroNewsTests(unittest.TestCase):
    def test_classifies_geopolitical_energy(self):
        topic, hits = mn.classify("Oil jumps as Iran conflict threatens shipping")
        self.assertEqual(topic, "energy_geopolitics")
        self.assertGreaterEqual(hits, 2)

    def test_normalize_requires_relevant_recent_story(self):
        now = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        item = mn.normalize_item({
            "headline": "Federal Reserve watches inflation and Treasury yields",
            "datetime": int(now.timestamp()),
            "source": "Reuters",
            "url": "https://example.test/story",
        }, "Finnhub", now)
        self.assertIsNotNone(item)
        self.assertEqual(item["topic"], "rates_inflation")
        self.assertEqual(item["source_type"], "secondary")

    def test_deduplicate_headlines(self):
        base = {
            "headline": "Fed policy shifts market expectations",
            "url": "https://example.test/1",
            "relevance_score": 8,
            "published_at": "2026-07-24T12:00:00+00:00",
        }
        duplicate = dict(base, url="https://example.test/2", relevance_score=6)
        self.assertEqual(len(mn.deduplicate([base, duplicate])), 1)


if __name__ == "__main__":
    unittest.main()
