import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

import requests
import scanner


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ODDS_API_KEY": "fake-odds-key",
            "TELEGRAM_BOT_TOKEN": " \n123456:fake_token\n ",
            "TELEGRAM_CHAT_ID": " 12345\n",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_token_copy_paste_formats(self):
        for value in ("123456:fake_\ntoken", " \n123456:fake_token\n ", '"123456:fake_token"', "bot123456:fake_token"):
            self.assertEqual(scanner.telegram_token(value), "123456:fake_token")

    def test_bad_token_never_echoed(self):
        for value in ("secret with spaces", "https://example.com/token", ""):
            with self.assertRaises(scanner.ScannerError) as error:
                scanner.telegram_token(value)
            self.assertNotIn(value or "nothing", str(error.exception))

    @patch("scanner.time.sleep")
    @patch("scanner.requests.post")
    @patch("scanner.candidates", return_value=[])
    def test_scan_and_alert(self, candidates, post, sleep):
        post.return_value = Mock(status_code=200, json=Mock(return_value={"ok": True}))
        scanner.main()
        self.assertIn("No matching open props", post.call_args.kwargs["json"]["text"])
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "12345")

    def test_exact_prop_and_complement_offer(self):
        m = {"id": "1", "slug": "example", "question": "Player above 24.5?",
             "gameStartTime": "2026-11-01T20:00:00Z", "marketSides": [
             {"description": "Yes", "long": True, "tradable": True},
             {"description": "No", "long": False, "tradable": True}]}
        d = {"state": "MARKET_STATE_OPEN", "bestBid": {"value": "0.4", "currency": "USD"},
             "bestAsk": {"value": "0.45", "currency": "USD"}}
        text = scanner.prop_message(m, d, scanner.datetime.now(scanner.timezone.utc))
        self.assertIn("Player above 24.5?", text)
        self.assertIn("Yes: 45.00", text)
        self.assertIn("No: 60.00", text)
        d["bestAsk"] = None
        self.assertIn("Yes: no available offer", scanner.prop_message(m, d, scanner.datetime.now(scanner.timezone.utc)))
        d["state"] = "MARKET_STATE_EXPIRED"
        self.assertIsNone(scanner.prop_message(m, d, scanner.datetime.now(scanner.timezone.utc)))

    def test_bad_prices(self):
        for value in ("NaN", "Infinity", "0", "1", "-1", "nonsense"):
            self.assertIsNone(scanner.price({"value": value, "currency": "USD"}))

    @patch("scanner.requests.get")
    def test_missing_secret_stops_before_network(self, get):
        del os.environ["TELEGRAM_CHAT_ID"]
        with self.assertRaises(scanner.ScannerError):
            scanner.main()
        get.assert_not_called()

    def test_http_failures_hide_url_and_body(self):
        for service in ("Telegram", "Odds API"):
            for status in (400, 401, 403, 404, 429, 500):
                method = Mock(return_value=Mock(status_code=status, text="private-response"))
                with self.assertRaises(scanner.ScannerError) as error:
                    scanner.request_json(method, "https://example.com/private-token", service)
                self.assertNotIn("private", str(error.exception))

    def test_network_failures_hide_credentials(self):
        for exception in (requests.Timeout, requests.ConnectionError, requests.HTTPError):
            with self.assertRaises(scanner.ScannerError) as error:
                scanner.request_json(Mock(side_effect=exception("private-token")), "private-url", "Telegram")
            self.assertNotIn("private", str(error.exception))

    def test_invalid_json(self):
        method = Mock(return_value=Mock(status_code=200, json=Mock(side_effect=ValueError("private-body"))))
        with self.assertRaises(scanner.ScannerError) as error:
            scanner.request_json(method, "private-url", "Telegram")
        self.assertNotIn("private", str(error.exception))

    @patch("scanner.requests.post")
    @patch("scanner.requests.get")
    def test_empty_scan_and_unconfirmed_delivery(self, get, post):
        get.return_value = Mock(status_code=200, json=Mock(return_value={"markets": []}))
        post.return_value = Mock(status_code=200, json=Mock(return_value={"ok": False}))
        with self.assertRaises(scanner.ScannerError):
            scanner.main()


if __name__ == "__main__":
    unittest.main()
