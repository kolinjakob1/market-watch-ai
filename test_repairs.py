import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch, Mock
from test_scanner import NOW, event, market
import scanner
import sports_engine as sports
import crypto_scanner as crypto

class Repairs(unittest.TestCase):
    def test_live_and_upcoming_discovered_finished_window_excluded(self):
        rows=[]
        for hours in [-2, 2, -12, 60]:
            m=market();m.update(id=str(hours), gameStartTime=(NOW+timedelta(hours=hours)).isoformat(),
                               marketType='moneyline',active=True,status='MARKET_STATUS_OPEN')
            rows.append(m)
        with patch.object(sports,'request_json',return_value={'markets':rows}):
            self.assertEqual({m['id'] for m in sports.discover(NOW)},{'-2','2'})
    def test_live_needs_fresh_book_and_exchange_samples(self):
        e=event();e['commence_time']=(NOW-timedelta(hours=1)).isoformat()
        bbo={'state':'MARKET_STATE_OPEN','bestBid':{'value':'.49','currency':'USD'},
             'bestAsk':{'value':'.50','currency':'USD'},'lastPriceSample':{'ts':NOW.isoformat()}}
        self.assertTrue(sports.evaluate(market(),bbo,e,NOW))
        bbo['lastPriceSample']['ts']=(NOW-timedelta(seconds=31)).isoformat()
        self.assertEqual(sports.evaluate(market(),bbo,e,NOW),[])
        for book in e['bookmakers']:book['last_update']=(NOW-timedelta(seconds=61)).isoformat()
        self.assertIsNone(sports.consensus(e,NOW))
    def test_telegram_preserves_full_long_report(self):
        message='candidate\n'*1000
        with patch.object(scanner,'request_json',return_value={'ok':True}) as req:
            scanner.send_telegram('123:abc','123',message)
            chunks=[c.kwargs['json']['text'] for c in req.call_args_list]
            self.assertEqual(''.join(chunks),message)
            self.assertTrue(all(len(c)<=3900 for c in chunks))
    def test_crypto_entry_exit_fees_and_cooldown(self):
        state={'cash':500.,'positions':{},'closed':[],'cooldown':{}}
        q={'BTC/USD':{'t':NOW.isoformat(),'bp':100.,'ap':100.1}}
        candidate={'stop':98.,'target':104.,'rsi':60.,'volume_multiple':2.,'bar':NOW.isoformat()}
        with patch.object(crypto,'SCAN_TIME',NOW),patch.object(crypto,'SYMBOLS',['BTC/USD']),patch.object(crypto,'setup',return_value=candidate):
            notes=crypto.process(state,{'BTC/USD':[]},q)
            self.assertEqual(len(state['positions']),1)
            self.assertIn('PAPER BUY',notes[0])
            self.assertGreaterEqual(state['cash'],375)
            q['BTC/USD']['bp']=105.;q['BTC/USD']['ap']=105.1
            crypto.process(state,{'BTC/USD':[]},q)
            self.assertFalse(state['positions'])
            self.assertGreater(state['cash'],500)
            self.assertEqual(len(state['closed']),1)
            self.assertIn('BTC/USD',state['cooldown'])
    def test_crypto_nonfinite_and_stale_quote_blocked(self):
        with patch.object(crypto,'SCAN_TIME',NOW):
            for ask in [float('nan'),float('inf')]:
                self.assertIsNone(crypto.quote_prices({'t':NOW.isoformat(),'bp':100,'ap':ask}))
            self.assertIsNone(crypto.quote_prices({'t':(NOW-timedelta(hours=1)).isoformat(),'bp':100,'ap':100.1}))
