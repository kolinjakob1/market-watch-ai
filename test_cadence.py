import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock
import cadence_scheduler as clock


def epoch(text):
    return datetime.fromisoformat(text.replace('Z','+00:00')).timestamp()


class FakeTime:
    def __init__(self, start): self.value=start
    def now(self): return self.value
    def sleep(self, seconds): self.value += seconds


class FakeGitHub:
    def __init__(self, timer, fail=None):
        self.timer=timer;self.events=[];self.cursor=None;self.fail=fail;self.failed=False
    def dispatch(self, name):
        if name==self.fail and not self.failed:
            self.failed=True
            raise RuntimeError('temporary outage')
        self.events.append((name,self.timer.now()))
    def handoff(self, cursor): self.cursor=dict(cursor)


class CadenceTests(unittest.TestCase):
    def test_offsets_rollover_and_no_weekend_stock_orders(self):
        t=epoch('2026-11-01T17:59:59Z')
        self.assertEqual(clock.utc(clock.next_slot(t,300,180)),'2026-11-01T18:03:00+00:00')
        self.assertEqual(clock.utc(clock.next_slot(t,600,120)),'2026-11-01T18:02:00+00:00')
        self.assertFalse(clock.stock_slot(t))
        self.assertTrue(clock.stock_slot(epoch('2026-11-02T14:04:00Z')))
        self.assertTrue(clock.stock_slot(epoch('2026-11-01T16:14:00Z')))
    def test_real_ten_and_five_minute_intervals_without_drift(self):
        timer=FakeTime(epoch('2026-11-01T17:41:50Z'));gh=FakeGitHub(timer)
        clock.run(gh,1300,now=timer.now,sleep=timer.sleep)
        for workflow,interval in [('scanner.yml',600),('crypto.yml',300)]:
            starts=[t for name,t in gh.events if name==workflow]
            self.assertGreaterEqual(len(starts),3)
            self.assertTrue(all(b-a==interval for a,b in zip(starts,starts[1:])))
        self.assertFalse(any(n=='robinhood.yml' for n,t in gh.events))
        self.assertIsNotNone(gh.cursor)
    def test_handoff_catches_slot_crossed_during_worker_startup(self):
        timer=FakeTime(epoch('2026-11-01T17:42:50Z'));first=FakeGitHub(timer)
        clock.run(first,5,now=timer.now,sleep=timer.sleep)
        timer.sleep(20)  # new worker begins after the 17:43 crypto slot
        second=FakeGitHub(timer)
        clock.run(second,10,cursor=first.cursor,now=timer.now,sleep=timer.sleep)
        self.assertEqual([n for n,t in second.events],['crypto.yml'])
        self.assertGreaterEqual(second.events[0][1],epoch('2026-11-01T17:43:00Z'))
    def test_transient_dispatch_failure_retries_without_losing_slot(self):
        timer=FakeTime(epoch('2026-11-01T17:41:50Z'));gh=FakeGitHub(timer,fail='scanner.yml')
        clock.run(gh,100,now=timer.now,sleep=timer.sleep)
        sports=[t for n,t in gh.events if n=='scanner.yml']
        self.assertEqual(sports,[epoch('2026-11-01T17:42:15Z')])
        self.assertEqual(len([n for n,t in gh.events if n=='crypto.yml']),1)
    def test_self_dispatch_payload_uses_supported_workflow_inputs(self):
        with patch.dict('os.environ',{'GITHUB_REPOSITORY':'test/repo','GH_TOKEN':'not-real'}):
            gh=clock.GitHub()
        gh.call=Mock()
        cursor={name:1234 for name in clock.CADENCES}
        gh.handoff(cursor)
        import json
        payload=gh.call.call_args.args[2]
        self.assertEqual(json.loads(payload['inputs']['cursor']),cursor)
        self.assertEqual(payload['ref'],'main')
    def test_handoff_rejection_is_not_reported_as_success(self):
        timer=FakeTime(epoch('2026-11-01T17:41:50Z'));gh=FakeGitHub(timer)
        gh.handoff=Mock(side_effect=RuntimeError('permission denied'))
        with self.assertRaises(RuntimeError):clock.run(gh,1,now=timer.now,sleep=timer.sleep)

if __name__=='__main__':unittest.main()
