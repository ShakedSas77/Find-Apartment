"""
=== Telegram vote listener ===

Long-polls Telegram getUpdates for taps on the vote buttons attached by
telegram_notifier.send_listing_alert(), records each vote, and acts on the
sheet based on the running tally:
  - VOTES_TO_REMOVE down-votes -> the listing's row is deleted from the sheet.
  - each up-vote -> the sheet's fit-score cell is bumped by SCORE_VOTE_BOOST
    (clamped to 100).
  - VOTES_TO_HIGHLIGHT up-votes -> a highlight message is sent to the chat.
A separate long-lived process — not part of the scrape.

    python bot_listener.py
"""

import os

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

import config
import storage
import telegram_notifier

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

_POLL_TIMEOUT_SECONDS = 30
_SCORE_COL = config.SHEET_HEADERS.index("ציון התאמה") + 1
_COL_PRICE, _COL_ROOMS, _COL_DIST_KM, _COL_ADDRESS = 1, 2, 3, 13  # 0-based row indexes
_COL_ENTRY_DATE, _COL_FLOOR, _COL_ELEVATOR = 4, 5, 6


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


def _find_row(sheet, url: str) -> tuple[int, list] | None:
    """Row number (1-based) + cell values for the listing, or None if it's not
    (or no longer) in the sheet — e.g. already removed by a prior down-vote,
    or was a dry-run/test post that never got a real row."""
    try:
        cell = sheet.find(url, in_column=1)
    except Exception:
        return None
    if cell is None:
        return None
    return cell.row, sheet.row_values(cell.row)


def _remove_listing(sheet, row_num: int):
    sheet.delete_rows(row_num)


def _boost_score(sheet, row_num: int, row: list):
    try:
        current = int(row[_SCORE_COL - 1]) if len(row) >= _SCORE_COL and row[_SCORE_COL - 1] else 0
    except ValueError:
        current = 0
    new_score = min(100, current + config.SCORE_VOTE_BOOST)
    sheet.update_cell(row_num, _SCORE_COL, new_score)


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
    found = _find_row(sheet, url)

    if down >= config.VOTES_TO_REMOVE and found:
        row_num, _ = found
        _remove_listing(sheet, row_num)
        if chat_id and message_id:
            _call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                              "reply_markup": {"inline_keyboard": []}})
            _call("sendMessage", {"chat_id": chat_id,
                                   "text": f"🗑 הוסר מהגיליון ({down} הצבעות שליליות)\n{url}"})
        _call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
        return

    if vote == "up" and found:
        row_num, row = found
        _boost_score(sheet, row_num, row)
        if up == config.VOTES_TO_HIGHLIGHT:
            telegram_notifier.send_highlight_alert(
                url,
                price=row[_COL_PRICE] if len(row) > _COL_PRICE else "",
                rooms=row[_COL_ROOMS] if len(row) > _COL_ROOMS else "",
                distance_km=row[_COL_DIST_KM] if len(row) > _COL_DIST_KM else "",
                address=row[_COL_ADDRESS] if len(row) > _COL_ADDRESS else "",
                entry_date=row[_COL_ENTRY_DATE] if len(row) > _COL_ENTRY_DATE else "",
                floor=row[_COL_FLOOR] if len(row) > _COL_FLOOR else "",
                elevator=row[_COL_ELEVATOR] if len(row) > _COL_ELEVATOR else "",
            )

    if chat_id and message_id:
        _call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": _markup(up, down, group_id, post_id),
        })
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
