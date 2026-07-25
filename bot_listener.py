"""
=== Telegram vote listener ===

Long-polls Telegram getUpdates for taps on the vote buttons attached by
telegram_notifier.send_listing_alert(), records each vote, and updates the
message's button labels with the running tally. A separate long-lived
process — not part of the scrape, no browser, no LLM/Maps calls.

    python bot_listener.py
"""

import os

import requests
from dotenv import load_dotenv

import storage

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

_POLL_TIMEOUT_SECONDS = 30


def _call(method: str, payload: dict) -> dict | list | None:
    url = _API_BASE.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=_POLL_TIMEOUT_SECONDS + 10)
        data = resp.json()
        if not data.get("ok"):
            print(f"WARNING: Telegram {method} failed: {data.get('description')}")
            return None
        return data.get("result")
    except Exception as e:
        print(f"WARNING: Telegram {method} request failed: {e}")
        return None


def _tally_markup(url: str, group_id: str, post_id: str) -> dict:
    votes = storage.get_votes(url)
    up = sum(1 for v in votes if v["vote"] == "up")
    down = sum(1 for v in votes if v["vote"] == "down")
    return {
        "inline_keyboard": [[
            {"text": f"⭐ מעניין ({up})", "callback_data": f"up:{group_id}:{post_id}"},
            {"text": f"🗑 לא רלוונטי ({down})", "callback_data": f"down:{group_id}:{post_id}"},
        ]]
    }


def _handle_callback_query(cq: dict):
    parts = cq.get("data", "").split(":")
    if len(parts) != 3:
        return
    vote, group_id, post_id = parts
    url = f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"

    voter = cq.get("from", {})
    voter_id = str(voter.get("id", ""))
    voter_name = voter.get("first_name") or voter.get("username") or voter_id
    storage.record_vote(url, voter_id, voter_name, vote)

    message = cq.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if chat_id and message_id:
        _call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": _tally_markup(url, group_id, post_id),
        })
    _call("answerCallbackQuery", {"callback_query_id": cq.get("id")})


def run():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return
    storage.init_db()
    offset = storage.get_telegram_offset()
    print(f"Listening for Telegram votes (offset={offset})... Ctrl+C to stop.")
    while True:
        updates = _call("getUpdates", {"offset": offset, "timeout": _POLL_TIMEOUT_SECONDS})
        if not updates:
            continue
        for update in updates:
            offset = update["update_id"] + 1
            cq = update.get("callback_query")
            if cq:
                _handle_callback_query(cq)
        storage.set_telegram_offset(offset)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped.")
