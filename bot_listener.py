"""
=== Telegram vote + rating listener ===

Long-polls Telegram getUpdates for taps on the buttons attached by
telegram_notifier.build_listing_keyboard(): a ⭐/🗑 vote row and two rows of
0-10 rating buttons.
  - Votes act on the sheet based on the running tally: VOTES_TO_REMOVE
    down-votes deletes the listing's row; each up-vote bumps the sheet's
    fit-score cell by SCORE_VOTE_BOOST (clamped to 100); VOTES_TO_HIGHLIGHT
    up-votes sends a highlight message to the chat.
  - Ratings (storage.record_rating()) have no sheet side effect at all —
    pure data collection, the training set for eventually regressing
    compute_fit_score()'s weights against real human judgment
    (storage.get_ratings_with_listing_data()).
Text messages in the configured chat are chat commands (/scan, /status, /stats...),
handled by integrations/telegram_commands.py.
A separate long-lived process — not part of the scrape.

    python bot_listener.py
"""

import os

import requests
from dotenv import load_dotenv

import config
from core import storage
from integrations import telegram_commands, telegram_notifier
from integrations.sheets import open_spreadsheet

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

_POLL_TIMEOUT_SECONDS = 30
_SCORE_COL = config.SHEET_HEADERS.index("ציון התאמה") + 1
_COL_PRICE, _COL_ROOMS, _COL_DIST_KM, _COL_ADDRESS = 1, 2, 3, 13  # 0-based row indexes
_COL_ENTRY_DATE, _COL_FLOOR, _COL_ELEVATOR = 4, 5, 6
_COL_TOTAL_COST = config.SHEET_HEADERS.index("עלות חודשית כוללת")


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
        print(f"WARNING: Telegram {method} request failed: {telegram_notifier._redact_bot_token(str(e))}")
        return None


def _open_sheet():
    return open_spreadsheet().sheet1


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


def _handle_rating(cq: dict, parts: list[str]):
    """rate:{score}:{group_id}:{post_id} — pure data collection, no sheet
    side effect, unlike the up/down vote (see _handle_callback_query)."""
    _, score_str, group_id, post_id = parts
    try:
        score = int(score_str)
    except ValueError:
        return
    url = f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"

    voter = cq.get("from", {})
    voter_id = str(voter.get("id", ""))
    voter_name = voter.get("first_name") or voter.get("username") or voter_id
    storage.record_rating(url, voter_id, voter_name, score)

    _call("answerCallbackQuery", {
        "callback_query_id": cq.get("id"),
        "text": f"ניקוד {score}/10 נשמר, תודה!",
    })


def _handle_command_button(cq: dict):
    # Same rule as a typed command: only the configured chat is obeyed.
    if str(cq.get("message", {}).get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
        return
    # getUpdates replays taps made while the listener was down, and a tap carries no time of its own.
    # Telegram refuses to answer an old tap ("query is too old"), so a refused answer means a stale
    # tap, and an old "scan" button must not start a scan hours later.
    if _call("answerCallbackQuery", {"callback_query_id": cq.get("id")}) is None:
        return
    telegram_commands.handle_button(cq.get("data", ""), _send_to_chat)


def _handle_callback_query(cq: dict, sheet):
    if cq.get("data", "").split(":")[0] in ("cmd", "sg"):
        _handle_command_button(cq)
        return
    parts = cq.get("data", "").split(":")
    if parts and parts[0] == "rate" and len(parts) == 4:
        _handle_rating(cq, parts)
        return
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
                score=row[_SCORE_COL - 1] if len(row) >= _SCORE_COL else "",
                total_cost=row[_COL_TOTAL_COST] if len(row) > _COL_TOTAL_COST else "",
            )

    if chat_id and message_id:
        _call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": telegram_notifier.build_listing_keyboard(up, down, group_id, post_id),
        })
    _call("answerCallbackQuery", {"callback_query_id": cq.get("id")})


def _send_to_chat(text: str, reply_markup: dict | None = None):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _call("sendMessage", payload)


def run():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return
    storage.init_db()
    # A local Excel file is rewritten by every scan; a copy held open here would save stale rows back
    # over a scan's, so it's reopened per tap instead.
    sheet = _open_sheet() if config.OUTPUT_BACKEND == "sheets" else None
    offset = storage.get_telegram_offset()
    # Fills the "/" menu in the Telegram chat with the commands below.
    _call("setMyCommands", {"commands": [{"command": name, "description": text}
                                         for name, text in telegram_commands.COMMANDS.items()]})
    print(f"Listening for Telegram votes and commands (offset={offset})... Ctrl+C to stop.")
    while True:
        updates = _call("getUpdates", {"offset": offset, "timeout": _POLL_TIMEOUT_SECONDS})
        if not updates:
            continue
        for update in updates:
            offset = update["update_id"] + 1
            cq = update.get("callback_query")
            if cq:
                _handle_callback_query(cq, sheet or _open_sheet())
            message = update.get("message")
            if message:
                telegram_commands.handle_message(message, TELEGRAM_CHAT_ID, _send_to_chat)
        storage.set_telegram_offset(offset)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped.")
