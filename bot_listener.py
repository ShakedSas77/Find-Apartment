"""
=== Telegram vote listener ===

Long-polls Telegram getUpdates for taps on the vote buttons attached by
telegram_notifier.send_listing_alert(), records each vote, updates the
message's button labels with the running tally, and syncs that same tally
into the sheet's "הצבעות" column (found by matching the listing's URL in
column A) — so a vote taken on the phone shows up in the sheet too, no
manual copying. A separate long-lived process — not part of the scrape.

    python bot_listener.py
"""

import os

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

import config
import storage

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

_POLL_TIMEOUT_SECONDS = 30
_VOTES_COLUMN = config.SHEET_HEADERS.index("הצבעות") + 1


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


def _open_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(config.CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID).sheet1


def _markup(up: int, down: int, group_id: str, post_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": f"⭐ מעניין ({up})", "callback_data": f"up:{group_id}:{post_id}"},
            {"text": f"🗑 לא רלוונטי ({down})", "callback_data": f"down:{group_id}:{post_id}"},
        ]]
    }


def _sync_sheet_vote(sheet, url: str, up: int, down: int):
    """No-op if the URL isn't in the sheet (e.g. a rejected/dry-run test post,
    or a row a later dedupe/prune pass removed) — nothing to sync in that case."""
    try:
        cell = sheet.find(url, in_column=1)
    except Exception:
        return
    try:
        sheet.update_cell(cell.row, _VOTES_COLUMN, f"⭐{up} 🗑{down}")
    except Exception as e:
        print(f"WARNING: could not sync vote tally to sheet for {url}: {e}")


def _handle_callback_query(cq: dict, sheet):
    parts = cq.get("data", "").split(":")
    if len(parts) != 3:
        return
    vote, group_id, post_id = parts
    url = f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"

    voter = cq.get("from", {})
    voter_id = str(voter.get("id", ""))
    voter_name = voter.get("first_name") or voter.get("username") or voter_id
    storage.record_vote(url, voter_id, voter_name, vote)

    votes = storage.get_votes(url)
    up = sum(1 for v in votes if v["vote"] == "up")
    down = sum(1 for v in votes if v["vote"] == "down")

    message = cq.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if chat_id and message_id:
        _call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": _markup(up, down, group_id, post_id),
        })
    _sync_sheet_vote(sheet, url, up, down)
    _call("answerCallbackQuery", {"callback_query_id": cq.get("id")})


def run():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return
    storage.init_db()
    sheet = _open_sheet()
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
                _handle_callback_query(cq, sheet)
        storage.set_telegram_offset(offset)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped.")
