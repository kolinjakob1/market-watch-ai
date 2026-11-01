"""Direct failure notification for scanner runs dispatched by the cadence worker."""
import json
import os
import re
from urllib.request import Request, urlopen


def normalized_token(raw):
    token = ''.join(raw.split()).strip('"\'')
    if token.startswith('bot'):
        token = token[3:]
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise RuntimeError('Invalid Telegram bot token')
    return token


def main():
    token = normalized_token(os.environ.get('TELEGRAM_BOT_TOKEN', ''))
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not chat_id:
        raise RuntimeError('Missing Telegram chat ID')
    workflow = os.environ.get('GITHUB_WORKFLOW', 'HustleWatch scanner')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    run_url = f'https://github.com/{repo}/actions/runs/{run_id}'
    message = f'HUSTLEWATCH SCANNER FAILED\n{workflow}\nLogs: {run_url}'
    payload = json.dumps({'chat_id': chat_id, 'text': message}).encode()
    request = Request(f'https://api.telegram.org/bot{token}/sendMessage',
                      data=payload, headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise RuntimeError('Telegram rejected scanner failure alert')
    print('Telegram API accepted scanner failure alert (phone receipt unverified)')


if __name__ == '__main__':
    main()
