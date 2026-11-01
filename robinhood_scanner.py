"""Run local paper simulator and report to Telegram. No real orders."""
import os
import sys
from datetime import datetime, timezone


def main():
    from paper_engine import run
    from scanner import send_telegram, telegram_token, required_env
    message = run(datetime.now(timezone.utc))
    if os.environ.get('DRY_RUN') == '1':
        print(message)
    else:
        send_telegram(telegram_token(required_env('TELEGRAM_BOT_TOKEN')),
                      required_env('TELEGRAM_CHAT_ID'), message)
    print('Paper evaluation completed')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Scanner failed: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)

