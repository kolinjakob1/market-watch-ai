import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import news_scanner as n

NOW = datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)


def article(identity=1, minutes=20):
    return {'id': identity, 'created_at': (NOW-timedelta(minutes=minutes)).isoformat(),
            'updated_at': (NOW-timedelta(minutes=minutes)).isoformat(),
            'headline': 'Example earnings headline', 'url': 'https://example.com/news',
            'symbols': ['AAPL'], 'source': 'test'}


class NewsTests(unittest.TestCase):
    def test_duplicate_stale_future_and_updated_stories(self):
        old = article(2, 120)
        future = article(3, -5)
        a = article()
        selected = n.select([a, a, old, future], {'seen': {}}, NOW)
        self.assertEqual(len(selected), 1)
        seen = {selected[0][0]: NOW.isoformat()}
        self.assertEqual(n.select([a], {'seen': seen}, NOW), [])
        a['updated_at'] = (NOW-timedelta(minutes=10)).isoformat()
        self.assertEqual(len(n.select([a], {'seen': seen}, NOW)), 1)

    def test_incomplete_pagination_and_malformed_feed_fail(self):
        with patch.dict(n.os.environ, {'ALPACA_API_KEY': 'x', 'ALPACA_SECRET_KEY': 'y'}):
            with patch.object(n, 'request_json', return_value={'news': [], 'next_page_token': 'loop'}):
                with self.assertRaises(n.ScannerError):
                    n.fetch(NOW)
        with self.assertRaises(n.ScannerError):
            n.select([{'id': 1}], {'seen': {}}, NOW)

    def test_failed_delivery_keeps_only_accepted_ids_and_retry_sends_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            with patch.object(n, 'fetch', return_value=[article(1), article(2)]), patch.dict(n.os.environ, {'TELEGRAM_BOT_TOKEN': '123:abc', 'TELEGRAM_CHAT_ID': '1'}):
                with patch.object(n, 'send_telegram', side_effect=[None, n.ScannerError('failed')]):
                    with self.assertRaises(n.ScannerError):
                        n.run(NOW, path)
                self.assertEqual(len(n.load(path)['seen']), 1)
                with patch.object(n, 'send_telegram') as send:
                    n.run(NOW, path)
                    self.assertEqual(send.call_count, 1)
                self.assertEqual(len(n.load(path)['seen']), 2)

    def test_dry_run_never_sends_or_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            with patch.object(n, 'fetch', return_value=[article()]), patch.object(n, 'send_telegram') as send:
                n.run(NOW, path, True)
                send.assert_not_called()
            self.assertFalse(path.exists())

    def test_batch_cap_retains_backlog_and_corrupt_state_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            with patch.object(n, 'fetch', return_value=[article(i) for i in range(12)]), patch.dict(n.os.environ, {'TELEGRAM_BOT_TOKEN': '123:abc', 'TELEGRAM_CHAT_ID': '1'}), patch.object(n, 'send_telegram'):
                n.run(NOW, path)
                self.assertEqual(n.load(path)['last_scan']['pending'], 2)
                n.run(NOW, path)
                self.assertEqual(len(n.load(path)['seen']), 12)
            path.write_text('{"version": 1, "seen": {"x": "bad"}}')
            with self.assertRaises(n.ScannerError):
                n.load(path)
