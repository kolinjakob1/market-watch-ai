import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import crypto_scanner as c


class PaperExplorationTests(unittest.TestCase):
    def test_small_exploratory_position_is_paper_only_and_risk_capped(self):
        now = datetime(2026, 11, 1, 21, 30, tzinfo=timezone.utc)
        bars = []
        for i in range(80):
            t = now - timedelta(minutes=(80-i)*5+1)
            close = 100 + i * 0.02
            bars.append({'t': c.iso(t), 'c': str(close), 'h': str(close+0.1),
                         'l': str(close-0.1), 'v': '100'})
        bars_by_symbol = {s: bars for s in c.SYMBOLS}
        quotes = {s: {'t': c.iso(now), 'bp': '101.58', 'ap': '101.60'} for s in c.SYMBOLS}
        state = {'cash': 500., 'positions': {}, 'closed': [], 'cooldown': {}}
        with patch.object(c, 'SCAN_TIME', now), patch.object(c, 'setup', return_value=None), patch.object(c, 'ema', side_effect=[101.5, 101.0]), patch.object(c, 'rsi', return_value=55), patch.object(c, 'atr', return_value=.2):
            events = c.process(state, bars_by_symbol, quotes)
        self.assertEqual(len(events), 1)
        self.assertIn('EXPLORATORY PAPER BUY', events[0])
        self.assertEqual(len(state['positions']), 1)
        p = next(iter(state['positions'].values()))
        self.assertLessEqual(p['spent'], 20.001)
        self.assertEqual(p['strategy'], 'exploration_paper')
        self.assertEqual(state['cash'] + p['spent'], 500.0)


if __name__ == '__main__':
    unittest.main()
