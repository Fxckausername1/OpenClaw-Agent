import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SPEC = importlib.util.spec_from_file_location("calendar_pull", Path(__file__).with_name("official_calendar_pull.py"))
cal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cal)


class CalendarTests(unittest.TestCase):
    def test_unfold_and_parse(self):
        text = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;TZID=America/New_York:20260723T100000\nSUMMARY:Consumer Price Index\\, Test\nURL:https://www.bls.gov/test\nEND:VEVENT\nEND:VCALENDAR\n"""
        rows = cal.parse_ics(text, datetime(2026, 7, 22, 9, 0, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["impact"], "high")
        self.assertEqual(rows[0]["event"], "Consumer Price Index, Test")


if __name__ == "__main__":
    unittest.main()
