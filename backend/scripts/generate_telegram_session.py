"""
One-time interactive helper to generate a Telethon session string.

Telegram's login flow needs a real phone number + the code (and possibly 2FA
password) sent to your Telegram app - that can't be automated non-interactively,
so this script is meant to be run once, by hand, outside the normal scheduler.

Usage (from the backend/ directory, with the same venv/deps as the app):
    python scripts/generate_telegram_session.py

Or via docker, with a live TTY:
    docker compose run --rm backend python scripts/generate_telegram_session.py

You'll be prompted for:
  1. Your api_id and api_hash (from https://my.telegram.org -> API development tools)
  2. Your phone number (international format, e.g. +34...)
  3. The login code Telegram sends you
  4. Your 2FA password, if you have one enabled

The script prints a session string at the end - put it in your .env as
TELEGRAM_SESSION_STRING, along with TELEGRAM_API_ID and TELEGRAM_API_HASH.
Treat this string like a password: anyone with it can act as your account.
"""

from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main() -> None:
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()

    print("\nAdd these to your .env:\n")
    print(f"TELEGRAM_API_ID={api_id}")
    print(f"TELEGRAM_API_HASH={api_hash}")
    print(f"TELEGRAM_SESSION_STRING={session_string}")


if __name__ == "__main__":
    main()
