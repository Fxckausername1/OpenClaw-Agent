import tempfile
import unittest
from pathlib import Path

from premarket_forward_collector import seal_dataset
from premarket_forward_evaluator import evaluate_archive, verify_outputs


def minute(symbol, timestamp, open_price, high, low, close, volume, feed="sip"):
    return {
        "record_type": "bar",
        "feed": feed,
        "symbol": symbol,
        "bar": {
            "t": timestamp,
            "o": open_price,
            "h": high,
            "l": low,
            "c": close,
            "v": volume,
            "vw": close,
        },
    }


def snapshot(symbol, prior_close, latest, bid, ask, feed):
    return {
        "record_type": "snapshot",
        "feed": feed,
        "symbol": symbol,
        "snapshot": {
            "prevDailyBar": {"c": prior_close},
            "latestTrade": {"p": latest},
            "latestQuote": {"bp": bid, "ap": ask},
        },
    }


class EndToEndTests(unittest.TestCase):
    def test_sealed_pair_builds_scored_observation_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive"
            output = Path(directory) / "evaluation"
            sector_map = Path(directory) / "sector_map.json"
            sector_map.write_text('{"AAPL":"XLK"}')
            session = archive / "2026-07-14"
            symbols = ["AAPL", "SPY", "XLK"]
            capture = []
            for symbol, prior, latest in [("AAPL", 99.0, 100.0), ("SPY", 600.0, 601.0), ("XLK", 250.0, 251.0)]:
                capture.extend([
                    minute(symbol, "2026-07-14T12:29:00Z", latest - 0.2, latest, latest - 0.3, latest - 0.1, 1000),
                    minute(symbol, "2026-07-14T12:59:00Z", latest - 0.1, latest + 0.1, latest - 0.2, latest, 2000),
                    snapshot(symbol, prior, latest, latest - 0.02, latest + 0.02, "delayed_sip"),
                    snapshot(symbol, prior, latest, 0.0, 0.0, "iex"),
                ])
            seal_dataset(session, "iex_capture_0915", capture, {
                "protocol_version": "premarket-forward-2026-07-13.4",
                "session_date": "2026-07-14",
                "collected_at": "2026-07-14T13:15:00Z",
                "decision_available_preopen": True,
                "outcome_available_postclose": False,
                "symbols": symbols,
            })

            regular = [
                minute("AAPL", "2026-07-14T13:30:00Z", 100.0, 100.3, 99.9, 100.1, 100),
                minute("AAPL", "2026-07-14T13:35:00Z", 100.1, 100.4, 100.0, 100.2, 100),
                minute("AAPL", "2026-07-14T13:40:00Z", 100.2, 100.5, 100.1, 100.3, 100),
                minute("AAPL", "2026-07-14T13:45:00Z", 100.3, 100.9, 100.2, 100.8, 1200),
                minute("AAPL", "2026-07-14T13:50:00Z", 100.9, 101.1, 100.6, 101.0, 500),
                minute("AAPL", "2026-07-14T19:59:00Z", 101.0, 101.3, 100.9, 101.2, 500),
            ]
            regular[3]["bar"]["vw"] = 100.5
            seal_dataset(session, "sip_session_backfill_1600", regular, {
                "protocol_version": "premarket-forward-2026-07-13.4",
                "session_date": "2026-07-14",
                "decision_available_preopen": False,
                "outcome_available_postclose": True,
                "symbols": symbols,
            })

            status, observations = evaluate_archive(archive, output, write=True, sector_map_path=sector_map)
            self.assertEqual(status["sealed_scored_market_days"], 1)
            self.assertEqual(status["data_quality"]["quote_coverage"], 1.0)
            self.assertEqual(status["candidates"]["orb_forward_baseline"]["observations"], 1)
            self.assertEqual(status["candidates"]["orb_pm_gap003"]["observations"], 1)
            self.assertGreater(observations[0]["net_r_6bp"], -2.0)
            self.assertEqual(verify_outputs(output)["days"], 1)


if __name__ == "__main__":
    unittest.main()
