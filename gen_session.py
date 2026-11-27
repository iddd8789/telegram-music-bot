"""
Generate a Pyrogram session string for the assistant account.

Run this once on your own computer:
    python gen_session.py

It will ask for:
  1. API_ID and API_HASH (from https://my.telegram.org)
  2. The phone number of the assistant Telegram account
  3. The login code Telegram sends to that account
  4. The two-factor password, if that account has one

At the end it prints the session string. Copy it and use it as the
SESSION_STRING environment variable when you run bot.py.
"""

from pyrogram import Client


def main() -> None:
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()

    print("\nNow log in with the ASSISTANT account (the one that will play music).")
    with Client("assistant_session", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        session_string = app.export_session_string()

    print("\nYour SESSION_STRING (keep it secret, anyone with it controls this account):\n")
    print(session_string)


if __name__ == "__main__":
    main()
