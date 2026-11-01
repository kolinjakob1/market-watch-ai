import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
import scanner
import sports_engine as sports
import paper_engine as paper

NOW = datetime(2026,11,2,16,tzinfo=timezone.utc)

def snap(bid=10,ask=10.01,age=0):
    return {'latestQuote': {'bp':bid,'ap':ask,'bs':10,'as':10,'t':(NOW-timedelta(seconds=age)).isoformat()}}

def event(prices=(1.6,2.5)):
    return {'home_team':'Boston Celtics','away_team':'Miami Heat','commence_time':(NOW+timedelta(hours=2)).isoformat(),
            'bookmakers':[{'key':str(i),'last_update':NOW.isoformat(),'markets':[{'key':'h2h','outcomes':[
                {'name':'Boston Celtics','price':prices[0]},{'name':'Miami Heat','price':prices[1]}]}]} for i in range(3)]}

def market():
    return {'id':'1','slug':'test','feeCoefficient':.0695,'gameStartTime':(NOW+timedelta(hours=2)).isoformat(),
            'marketSides':[{'long':True,'tradable':True,'team':{'name':'Boston Celtics'}},
                           {'long':False,'tradable':True,'team':{'name':'Miami Heat'}}]}

class SportsTests(unittest.TestCase):
    def test_exact_identity_and_date(self):
        self.assertIsNotNone(sports.match(market(),[event()]))
        e=event();e['home_team']='Boston';self.assertIsNone(sports.match(market(),[e]))
        e=event();e['commence_time']=(NOW+timedelta(days=1)).isoformat();self.assertIsNone(sports.match(market(),[e]))
    def test_minimum_fresh_books_and_draw(self):
        e=event();self.assertIsNotNone(sports.consensus(e,NOW))
        e['bookmakers'].pop();self.assertIsNone(sports.consensus(e,NOW))
        e=event()
        for b in e['bookmakers']: b['last_update']=(NOW-timedelta(hours=1)).isoformat()
        self.assertIsNone(sports.consensus(e,NOW))
        e=event()
        for b in e['bookmakers']: b['markets'][0]['outcomes'].append({'name':'Draw','price':3})
        self.assertIsNone(sports.consensus(e,NOW))
    def test_fee_adjusted_positive_edge_only(self):
        bbo={'state':'MARKET_STATE_OPEN','bestBid':{'value':'.49','currency':'USD'},'bestAsk':{'value':'.50','currency':'USD'}}
        picks=sports.evaluate(market(),bbo,event(),NOW)
        self.assertEqual(picks[0]['team'],'Boston Celtics');self.assertGreater(picks[0]['edge'],.03)
        self.assertEqual(sports.evaluate(market(),bbo,event((2,2)),NOW),[])
        target=sports.evaluate(market(),bbo,event((2,2)),NOW,min_edge=-1)[0]
        self.assertLess(target['limit'],target['cost'])
        self.assertGreaterEqual(target['fair']-target['limit']-.0695*target['limit']*(1-target['limit'])-.01,.03)
    def test_secret_safe_errors(self):
        method=Mock(return_value=Mock(status_code=401))
        with self.assertRaises(scanner.ScannerError) as err: scanner.request_json(method,'private-token','Feed')
        self.assertNotIn('private',str(err.exception))
    @patch('sports_engine.request_json')
    def test_discovery_uses_moneyline_filter(self,request):
        request.return_value={'markets':[]};self.assertEqual(sports.discover(NOW),[])
        self.assertEqual(request.call_args.kwargs['params']['marketTypes'],'moneyline')

class PaperTests(unittest.TestCase):
    def state(self):
        return {'version':1,'cash':500,'realized':0,'positions':[],'closed':[],'last_entry':{}}
    def entry(self): return {'symbol':'AAPL','underlying':'AAPL','multiplier':1,'feed':'iex'}
    def test_buy_dedup_exit_and_cash_account_settlement(self):
        s=self.state();e=self.entry()
        self.assertEqual(len(paper.tick(s,{'AAPL':snap()},[e],NOW)),1)
        self.assertGreaterEqual(s['cash'],400);self.assertLess(s['cash'],401)
        paper.tick(s,{'AAPL':snap()},[e],NOW);self.assertEqual(len(s['positions']),1)
        paper.tick(s,{'AAPL':snap(11,11.01)},[e],NOW)
        self.assertEqual(len(s['closed']),1);self.assertEqual(len(s['positions']),0)
        self.assertGreater(s['realized'],0);self.assertLess(s['cash'],401)
        paper.tick(s,{},[],NOW+timedelta(days=1));self.assertGreater(s['cash'],500)
    def test_stale_crossed_nonfinite_and_zero_size_rejected(self):
        for q in [snap(age=3600),snap(11,10),snap(float('nan'),10)]:
            s=self.state();paper.tick(s,{'AAPL':q},[self.entry()],NOW);self.assertEqual(s['positions'],[])
        q=snap();q['latestQuote']['as']=0
        self.assertIsNone(paper.quotes(q,NOW))
    def test_option_contract_multiplier_and_affordability(self):
        e={'symbol':'AAPL261120C00200000','underlying':'AAPL','multiplier':100,'feed':'opra'}
        s=self.state()
        paper.tick(s,{e['symbol']:snap(.8,.81)},[e],NOW)
        self.assertEqual(s['positions'][0]['quantity'],1)
        self.assertLess(s['positions'][0]['cost'],100)
        s=self.state()
        paper.tick(s,{e['symbol']:snap(1.1,1.11)},[e],NOW)
        self.assertEqual(s['positions'],[])

    def test_stop_and_close(self):
        for closing, bid in [(False,9),(True,10)]:
            s=self.state();paper.tick(s,{'AAPL':snap()},[self.entry()],NOW)
            paper.tick(s,{'AAPL':snap(bid,bid+.01)},[],NOW,closing)
            self.assertEqual(len(s['closed']),1)
    def test_corrupt_state_never_silently_resets(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';p.write_text('{invalid')
            with self.assertRaises(json.JSONDecodeError):paper.load(p)
    @patch('paper_engine.request_json',return_value={'is_open':False})
    @patch.dict('os.environ',{'ALPACA_API_KEY':'fake','ALPACA_SECRET_KEY':'fake'})
    def test_closed_market_no_orders_or_quote_fills(self,request):
        with tempfile.TemporaryDirectory() as d, patch('paper_engine.Path',return_value=Path(d)/'state.json'):
            self.assertIn('Market closed',paper.run(NOW));self.assertEqual(request.call_count,1)

if __name__=='__main__':unittest.main()
