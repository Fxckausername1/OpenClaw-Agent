import unittest

from smc import notify
from smc.config import SmcConfig


class Response:
    ok = True
    content = b"{}"

    def json(self):
        return {"ok": True, "result": {"message_id": 1}}


class Session:
    def __init__(self):
        self.calls = []

    def post(self, url, data, timeout):
        self.calls.append((url, data, timeout))
        return Response()


class DirectTelegramTests(unittest.TestCase):
    def test_direct_send_uses_https_and_waits_for_ack(self):
        session = Session()
        config = SmcConfig(telegram_target="123", telegram_timeout_seconds=0.5)
        self.assertTrue(notify.send_direct(
            "paper test", config, session=session, token="test-token"))
        self.assertEqual(1, len(session.calls))
        url, data, timeout = session.calls[0]
        self.assertEqual("https://api.telegram.org/bottest-token/sendMessage", url)
        self.assertEqual({"chat_id": "123", "text": "paper test"}, data)
        self.assertEqual(0.5, timeout)

    def test_transport_failure_returns_false_without_raising(self):
        class Broken:
            def post(self, *args, **kwargs):
                raise TimeoutError("contains potentially sensitive URL")

        self.assertFalse(notify.send_direct(
            "paper test", SmcConfig(), session=Broken(), token="secret"))


if __name__ == "__main__":
    unittest.main()
