import unittest

from smc.run_daemon import parse_args


class CanaryLatchArgumentTests(unittest.TestCase):
    def test_limit_must_be_positive(self):
        with self.assertRaises(SystemExit):
            parse_args(["--mode", "paper-forward",
                        "--max-entry-submissions", "0"])

    def test_one_is_accepted(self):
        args = parse_args(["--mode", "paper-forward",
                           "--max-entry-submissions", "1"])
        self.assertEqual(1, args.max_entry_submissions)


if __name__ == "__main__":
    unittest.main()
