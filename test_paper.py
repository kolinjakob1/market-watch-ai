import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import paper_engine as paper

OPEN = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
NOW = OPEN + 21 * paper.BAR
CLOSE = OPEN + timedelta(hours=6.5)


def bars(direction):
    return [
        {'t': (OPEN + i * paper.BAR).isoformat(), 'c': 100 + direction * i / 10,
         'v': 200 if i >= 20 else 100, 'vw': 100 + direction * i / 10}
        for i in range(22)
    ]


def snapshot(bid, ask, stamp=NOW):
    return {'latestQuote': {'bp': bid, 'ap': ask, 'bs': 10, 'as': 10, 't': stamp.isoformat()}}


class StockOptionTests(unittest.TestCase):
    def test_only_completed_regular_session_bars_confirm_direction(self):
        for direction, side in [(1, 'call'), (-1, 'put')]:
            rows = bars(direction)
            self.assertEqual(paper.signal(rows, NOW, OPEN)[0][0], side)
            rows[20]['v'] = 150
            self.assertIsNone(paper.signal(rows, NOW, OPEN)[0])
            rows[20]['v'] = 200
            self.assertIsNone(paper.signal(rows[1:], NOW, OPEN)[0])

    def test_no_entry_on_premarket_quote(self):
        snap = snapshot(10, 10.01, OPEN - timedelta(seconds=5))
        self.assertIsNone(paper.quotes(snap, OPEN, session_open=OPEN))

    def test_portfolio_and_daily_limits(self):
        state = {'cash': 500, 'positions': [], 'closed': [], 'last_entry': {}, 'realized': 0}
        symbols = ['AAA', 'BBB', 'CCC', 'DDD']
        quotes = {s: snapshot(10, 10.01) for s in symbols}
        entries = [{'symbol': s, 'underlying': s, 'multiplier': 1} for s in symbols]
        paper.tick(state, quotes, entries, NOW)
        self.assertEqual(len(state['positions']), 2)
        paper.tick(state, {'AAA': snapshot(11, 11.01)}, [], NOW)
        paper.tick(state, quotes, entries, NOW)
        self.assertEqual(set(state['last_entry']), {'AAA', 'BBB', 'CCC'})
        paper.tick(state, {'BBB': snapshot(11, 11.01)}, [], NOW)
        paper.tick(state, quotes, entries, NOW)
        self.assertEqual(len(state['positions']), 1)

        state['closed'].append({'closed_at': NOW.isoformat(), 'pnl': -30})
        state['last_entry'].clear()
        paper.tick(state, quotes, entries, NOW)
        self.assertEqual(len(state['positions']), 1)

    def test_bearish_setup_buys_verified_put_across_pages(self):
        contract = 'AAPL261120P00098000'
        seen = []

        def api(method, url, service, **kwargs):
            params = kwargs['params']
            if url.endswith('/clock'):
                return {'is_open': True, 'next_close': CLOSE.isoformat()}
            if url.endswith('/stocks/snapshots'):
                return {'AAPL': snapshot(97.98, 98.02)}
            if url.endswith('/stocks/bars'):
                return {'bars': {'AAPL': bars(-1)}}
            if url.endswith('/options/snapshots/AAPL'):
                self.assertEqual(params['type'], 'put')
                seen.append(params.get('page_token'))
                if not params.get('page_token'):
                    return {'snapshots': {}, 'next_page_token': 'next'}
                return {'snapshots': {contract: snapshot(.46, .48)}}
            if url.endswith('/contracts/' + contract):
                return {'symbol': contract, 'underlying_symbol': 'AAPL', 'status': 'active',
                        'tradable': True, 'size': '100'}
            self.fail(f'Unexpected API call {url}')

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'paper_state.json'
            with patch.object(paper, 'Path', return_value=path), patch.object(paper, 'request_json', side_effect=api), \
                 patch.object(paper, 'required_env', return_value='test'), patch.dict('os.environ', {'WATCHLIST': 'AAPL'}):
                self.assertIn('PAPER BUY ' + contract, paper.run(NOW, persist=False))
                self.assertFalse(path.exists())
                self.assertIn('PAPER BUY ' + contract, paper.run(NOW))
                self.assertEqual(seen, [None, 'next', None, 'next'])
                ledger = json.loads(path.read_text())
                self.assertEqual(ledger['positions'][0]['strategy'], paper.STRATEGY)
                self.assertEqual(ledger['positions'][0]['quantity'], 1)

    def test_opra_denial_keeps_bullish_stock_fallback(self):
        def api(method, url, service, **kwargs):
            if url.endswith('/clock'):
                return {'is_open': True, 'next_close': CLOSE.isoformat()}
            if url.endswith('/stocks/snapshots'):
                return {'AAPL': snapshot(101.98, 102.02)}
            if url.endswith('/stocks/bars'):
                return {'bars': {'AAPL': bars(1)}}
            raise paper.ScannerError('Alpaca options returned HTTP 403')

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paper, 'Path', return_value=Path(tmp) / 'ledger.json'), \
                 patch.object(paper, 'request_json', side_effect=api), \
                 patch.object(paper, 'required_env', return_value='test'), patch.dict('os.environ', {'WATCHLIST': 'AAPL'}):
                message = paper.run(NOW, persist=False)
                self.assertIn('Options blocked:', message)
                self.assertIn('PAPER BUY AAPL', message)
