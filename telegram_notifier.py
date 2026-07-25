"""
=== Telegram push + voting ===

Optional (config.TELEGRAM_ENABLED). Sends a new match to a shared Telegram
chat with an inline "star / trash" keyboard; bot_listener.py handles the
button taps and records votes. Every call here is wrapped so a Telegram
outage or misconfiguration can never break a scrape — worst case is a
printed warning and a match that just doesn't get an alert (it's still in
the sheet).
"""

import os

import requests
from dotenv import load_dotenv

import storage

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_warned_not_configured = False


def _configured() -> bool:
    global _warned_not_configured
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        return True
    if not _warned_not_configured:
        print("WARNING: TELEGRAM_ENABLED is True but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing from .env")
        _warned_not_configured = True
    return False


def _call(method: str, payload: dict) -> dict | None:
    url = _API_BASE.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"WARNING: Telegram {method} failed: {data.get('description')}")
            return None
        return data.get("result")
    except Exception as e:
        print(f"WARNING: Telegram {method} request failed: {e}")
        return None


def _group_post_ids(post_url: str) -> tuple[str, str] | None:
    """Pulls (group_id, post_id) out of the canonical
    https://www.facebook.com/groups/<gid>/posts/<pid>/ shape."""
    parts = post_url.rstrip("/").split("/")
    if len(parts) < 4 or parts[-2] != "posts":
        return None
    return parts[-3], parts[-1]


def _format_listing_message(fields: dict, score: int) -> str:
    price = fields.get("price_val")
    price_line = f"{int(price):,} ₪" if price else "מחיר לא ידוע"
    lines = [
        f"דירה חדשה — ציון התאמה {score}/100",
        f"{fields.get('rooms_val', '')} חדרים | {price_line}",
        f"מרחק הליכה: {fields.get('distance_text') or '?'} ק\"מ",
        f"קומה: {fields.get('floor', '')}",
        f"כתובת: {fields.get('address', '')}",
    ]
    if fields.get("is_agent"):
        lines.append("תיווך")
    return "\n".join(lines)


def send_listing_alert(post_url: str, fields: dict, score: int) -> str | None:
    """Sends a new match to the shared Telegram chat with vote buttons.
    Returns the sent message_id, or None on any failure/misconfiguration."""
    if not _configured():
        return None
    ids = _group_post_ids(post_url)
    if not ids:
        print(f"WARNING: could not extract group/post id from {post_url} — skipping Telegram alert")
        return None
    group_id, post_id = ids

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{_format_listing_message(fields, score)}\n\n{post_url}",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "⭐ מעניין", "callback_data": f"up:{group_id}:{post_id}"},
                {"text": "🗑 לא רלוונטי", "callback_data": f"down:{group_id}:{post_id}"},
            ]]
        },
    }
    result = _call("sendMessage", payload)
    if not result:
        return None
    message_id = str(result.get("message_id"))
    storage.set_telegram_message(post_url, TELEGRAM_CHAT_ID, message_id)
    return message_id


def send_failure_alert(summary: str):
    """Used by doctor.py --alert to DM a health-check failure summary."""
    if not _configured():
        return
    _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ apartment-bot doctor check failed:\n\n{summary}"})
