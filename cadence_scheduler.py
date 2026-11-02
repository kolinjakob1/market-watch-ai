"""Wall-clock dispatcher for paper/research scanners; no market-data or trade keys.

A running worker dispatches ordinary scanner workflows, then starts a successor.
GitHub cron is a recovery trigger only, not the scan clock.
"""
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib import request, error

WORKFLOW = 'cadence.yml'
CADENCES = {'crypto.yml': (300, 180), 'scanner.yml': (600, 120),
            'robinhood.yml': (300, 240), 'news.yml': (600, 360)}


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds')


def next_slot(epoch, period, offset):
    return ((int(epoch) - offset) // period + 1) * period + offset


def stock_slot(epoch):
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return (dt.weekday() < 5 and 13 <= dt.hour <= 21) or (
        dt.weekday() >= 5 and dt.hour == 16 and dt.minute == 14)


class GitHub:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GH_TOKEN']

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = request.Request('https://api.github.com/repos/' + self.repo + path,
            data=data, method=method, headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'Content-Type': 'application/json'})
        for attempt in range(3):
            try:
                with request.urlopen(req, timeout=20) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                # Do not expose request headers or secret-bearing exception text.
                raise RuntimeError(f'GitHub {method} {path} HTTP {exc.code}') from None
            except (error.URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f'GitHub {method} {path} connection failed') from None

    def dispatch(self, workflow):
        self.call('POST', '/actions/workflows/' + workflow + '/dispatches', {'ref':'main'})


    def handoff(self, cursor):
        self.call('POST', '/actions/workflows/' + WORKFLOW + '/dispatches',
                  {'ref': 'main', 'inputs': {'cursor': json.dumps(cursor)}})


def run(github, duration, bootstrap=False, cursor=None, now=time.time, sleep=time.sleep):
    start = now()
    deadline = start + duration
    due = {name: next_slot(start, *cadence) for name, cadence in CADENCES.items()}
    if cursor:
        # Accept the original three-workflow cursor during a rolling upgrade.
        required = set(CADENCES) - {'news.yml'}
        if not required <= set(cursor) <= set(CADENCES) or not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in cursor.values()):
            raise ValueError('Invalid resume cursor')
        due.update(cursor)
    retry_at = {}
    failures = {}
    if bootstrap:
        # Immediate initial scans; later sessions preserve fixed wall-clock slots.
        for name in ('crypto.yml', 'scanner.yml', 'news.yml'):
            try:
                github.dispatch(name)
                print(f'DISPATCH bootstrap {name} accepted={utc(now())}', flush=True)
            except RuntimeError:
                failures[name] = start
                retry_at[name] = start + 15
                print(f'::warning::Initial dispatch failed for {name}; retrying', flush=True)
    while now() < deadline:
        current = now()
        for name, (period, offset) in CADENCES.items():
            if current < due[name] and name not in failures:
                continue
            if current < retry_at.get(name, 0):
                continue
            scheduled = failures.get(name, due[name])
            if name == 'robinhood.yml' and not stock_slot(current):
                due[name] = next_slot(current, period, offset)
                failures.pop(name, None)
                continue
            try:
                github.dispatch(name)
                print(f'DISPATCH {name} due={utc(scheduled)} accepted={utc(now())} lag={now()-scheduled:.1f}s', flush=True)
                failures.pop(name, None)
                retry_at.pop(name, None)
                # Skip expired slots after a pause; never burst-dispatch catch-up scans.
                due[name] = next_slot(now(), period, offset)
            except RuntimeError:
                failures[name] = scheduled
                retry_at[name] = now() + 15
                print(f'::warning::Dispatch failed for {name}; retry in 15s', flush=True)
                if now() - scheduled >= period:
                    raise RuntimeError(f'{name} dispatch unavailable for a full scan interval')
        sleep(min(1, max(0, deadline-now())))
    # A successful POST is required before this worker exits. Concurrency serializes
    # the queued successor after this run, so two clocks cannot dispatch together.
    github.handoff(due)
    print(f'HANDOFF successor accepted={utc(now())}', flush=True)


if __name__ == '__main__':
    try:
        run(GitHub(), int(os.environ.get('SESSION_SECONDS', '3300')),
            bootstrap=os.environ.get('GITHUB_EVENT_NAME') == 'push',
            cursor=json.loads(os.environ.get('RESUME_CURSOR') or '{}'))
    except Exception as exc:
        print(f'::error::Scheduler stopped: {type(exc).__name__}: {exc}', flush=True)
        raise SystemExit(1)
