"""
=== Telegram push + voting ===

Optional (config.TELEGRAM_ENABLED). Sends a new match to a shared Telegram
chat with an inline "star / trash" keyboard; bot_listener.py handles the
button taps and records votes. Every call here is wrapped so a Telegram
outage or misconfiguration can never break a scrape — worst case is a
printed warning and a match that just doesn't get an alert (it's still in
the sheet).
"""

import html
import os
import re
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

import config
import scoring
import storage
from core.normalize import _classify_address_confidence, map_bool

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_warned_not_configured = False

# Telegram puts the bot token in the URL path (not a header), so on a network-level failure the
# requests/urllib3 exception's str() embeds ".../bot<TOKEN>/sendMessage". Strip it before any
# exception text reaches a print, log, or the doctor.py --alert DM.
_BOT_TOKEN_IN_URL_RE = re.compile(r'(/bot)[^/\s]+')


def _redact_bot_token(text: str) -> str:
    return _BOT_TOKEN_IN_URL_RE.sub(lambda match: match.group(1) + 'REDACTED', text)


def _configured() -> bool:
    global _warned_not_configured
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        return True
    if not _warned_not_configured:
        print("WARNING: TELEGRAM_ENABLED is True but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing from .env")
        _warned_not_configured = True
    return False


def _call(method: str, payload: dict, _retried: bool = False) -> dict | None:
    url = _API_BASE.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            # 429 from a burst of sends (e.g. --backfill-ratings) carries the
            # server's own cooldown in retry_after — honor it and retry once
            # rather than dropping the message, since a single flood-control
            # hit is otherwise indistinguishable from a real failure.
            retry_after = data.get("parameters", {}).get("retry_after")
            if not _retried and data.get("error_code") == 429 and retry_after:
                time.sleep(retry_after + 1)
                return _call(method, payload, _retried=True)
            print(f"WARNING: Telegram {method} failed: {data.get('description')}")
            return None
        return data.get("result")
    except Exception as e:
        print(f"WARNING: Telegram {method} request failed: {_redact_bot_token(str(e))}")
        return None


def _group_post_ids(post_url: str) -> tuple[str, str] | None:
    """Pulls (group_id, post_id) out of the canonical
    https://www.facebook.com/groups/<gid>/posts/<pid>/ shape."""
    parts = post_url.rstrip("/").split("/")
    if len(parts) < 4 or parts[-2] != "posts":
        return None
    return parts[-3], parts[-1]


def _escape(value) -> str:
    """Telegram HTML parse mode: address/warning/entry-date text comes from scraped Facebook text via
    an LLM, so it is untrusted — an unescaped `<` or `&` makes Telegram reject the whole message."""
    return html.escape(str(value), quote=False)


def _post_url(group_id: str, post_id: str) -> str:
    return f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"


def _score_tier(score: int) -> str:
    if score >= config.TELEGRAM_SCORE_GOOD:
        return "🟢"
    if score >= config.TELEGRAM_SCORE_OK:
        return "🟡"
    return "🔴"


def _as_number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_floor(floor) -> str:
    if floor is None or floor == "":
        return ""
    return "קרקע" if _as_number(floor) == 0 else _escape(floor)


def _format_listing_message(fields: dict, score: int) -> str:
    """HTML message body; a line (or field) with no data is left out rather than shown as a placeholder."""
    headline = [f"{_score_tier(score)} <b>ציון {score}/100</b>"]
    rooms = _as_number(fields.get("rooms_val"))
    if rooms:
        headline.append(f"{rooms:g} חדרים")
    price = fields.get("price_val")
    if price:
        headline.append(f"<b>{int(price):,} ₪</b>")
    lines = [" · ".join(headline)]

    total_cost = scoring.compute_total_monthly_cost(fields)
    if total_cost:
        lines.append(f"💰 עלות חודשית כוללת: <b>{total_cost:,} ₪</b>")
    if fields.get("address"):
        lines.append(f"📍 {_escape(fields['address'])}")
    distance_km = _as_number(fields.get("distance_text"))
    if distance_km is not None:
        lines.append(f"🚶 {distance_km:g} ק\"מ הליכה")

    details = [
        ("קומה", _format_floor(fields.get("floor"))),
        ("מעלית", map_bool(fields.get("elevator"))),
        ("חניה", _escape(fields["parking"]) if fields.get("parking") else ""),
        ("ממ\"ד", map_bool(fields.get("shelter"))),
    ]
    detail_line = " | ".join(f"{label}: {value}" for label, value in details if value)
    if detail_line:
        lines.append(f"🏢 {detail_line}")
    if fields.get("entry_date"):
        lines.append(f"📅 כניסה: {_escape(fields['entry_date'])}")
    fees = [f"{label}: {_escape(fields[key])}" for label, key in (("ארנונה", "arnona"), ("ועד בית", "vaad")) if fields.get(key)]
    if fees:
        lines.append(f"🧾 {' | '.join(fees)}")
    if fields.get("address_warning"):
        lines.append(f"⚠️ {_escape(fields['address_warning'])}")
    if fields.get("is_agent"):
        lines.append("תיווך")
    return "\n".join(lines)


def _maps_url(fields: dict) -> str | None:
    """Google Maps search link for the listing's address, or None unless at least the street is known.
    Telegram URL buttons only accept http(s)/tg links, so this is the https maps link; on a phone it
    hands off to the Google Maps app. Uses the address's stored confidence when the pipeline computed
    one (it can be downgraded by geocoding), else classifies the text the way the pipeline does — strict
    on purpose: a missing button beats a button to the wrong place."""
    address = (fields.get("address") or "").strip()
    if not address:
        return None
    confidence = fields.get("address_confidence") or _classify_address_confidence(address)[0]
    if confidence != "high":
        return None
    query = address if "ישראל" in address else f"{address}, ישראל"
    return "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": query})


def _link_row(post_url: str, maps_url: str | None = None) -> list:
    row = [{"text": "🔗 פתח פוסט", "url": post_url}]
    if maps_url:
        row.append({"text": "🗺 פתח במפות", "url": maps_url})
    return row


def build_link_keyboard(group_id: str, post_id: str, maps_url: str | None = None) -> dict:
    """reply_markup for every new listing message: "open post", plus "open in Maps" when the street is
    known. The ⭐/🗑 vote buttons and the 0-10 rating rows were dropped from new messages."""
    return {"inline_keyboard": [_link_row(_post_url(group_id, post_id), maps_url)]}


def build_listing_keyboard(up: int, down: int, group_id: str, post_id: str) -> dict:
    """
    Vote keyboard (⭐/🗑 row + "open post" link row) for messages sent *before* the vote buttons
    were removed: bot_listener.py rebuilds it on a tap on one of those old messages so the tally
    and the link row stay put. New messages use build_link_keyboard().
    """
    vote_row = [
        {"text": f"⭐ מעניין ({up})", "callback_data": f"up:{group_id}:{post_id}"},
        {"text": f"🗑 לא רלוונטי ({down})", "callback_data": f"down:{group_id}:{post_id}"},
    ]
    return {"inline_keyboard": [vote_row, _link_row(_post_url(group_id, post_id))]}


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
        "text": _format_listing_message(fields, score),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": build_link_keyboard(group_id, post_id, _maps_url(fields)),
    }
    result = _call("sendMessage", payload)
    if not result:
        return None
    message_id = str(result.get("message_id"))
    storage.set_telegram_message(post_url, TELEGRAM_CHAT_ID, message_id)
    return message_id


def send_highlight_alert(post_url: str, price: str, rooms: str, distance_km: str, address: str,
                          entry_date: str = "", floor: str = "", elevator: str = "",
                          score: str = "", total_cost: str = ""):
    """Sent by bot_listener.py once a listing crosses VOTES_TO_HIGHLIGHT up-votes
    — a distinct, vote-less message calling out a listing both voters liked.
    Values are the sheet row's raw cells (strings), so each is escaped and a blank one is left out."""
    if not _configured():
        return
    summary = [part for part in (
        f"ציון {_escape(score)}/100" if score else "",
        f"{_escape(rooms)} חדרים" if rooms else "",
        f"<b>{_escape(price)} ₪</b>" if price else "",
    ) if part]
    lines = [f"🔥 <b>דירה חמה</b> — {config.VOTES_TO_HIGHLIGHT}+ הצבעות חיוביות!"]
    if summary:
        lines.append(" · ".join(summary))
    if total_cost:
        lines.append(f"💰 עלות חודשית כוללת: <b>{_escape(total_cost)} ₪</b>")
    if address:
        lines.append(f"📍 {_escape(address)}")
    if distance_km:
        lines.append(f"🚶 {_escape(distance_km)} ק\"מ הליכה")
    detail_line = " | ".join(f"{label}: {_escape(value)}" for label, value in (("קומה", floor), ("מעלית", elevator)) if value)
    if detail_line:
        lines.append(f"🏢 {detail_line}")
    if entry_date:
        lines.append(f"📅 כניסה: {_escape(entry_date)}")
    _call("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [_link_row(post_url, _maps_url({"address": address}))]},
    })


def send_backfill_announcement(count: int):
    """Sent once before apartment_bot.py --backfill-ratings replays every
    sheet row, so the chat knows why a burst of old listings just showed up."""
    if not _configured():
        return
    text = (
        f"📋 גיבוי היסטורי — {count} דירות מהעבר.\n"
        f"דרגו כל אחת (0-10) — עוזר לנו לכייל את מערכת הניקוד לפי מה שבאמת חשוב לכם."
    )
    _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})


def send_failure_alert(summary: str):
    """Used by doctor.py --alert to DM a health-check failure summary."""
    if not _configured():
        return
    _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ apartment-bot doctor check failed:\n\n{summary}"})


def send_run_started(total_groups: int):
    """Sent once at the start of a --live run_scraper() call — never on a dry run."""
    if not _configured():
        return
    _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": f"▶️ הרצה התחילה — {total_groups} קבוצות לסריקה"})


def send_run_finished(stats: dict):
    """Sent once at the end of a --live run_scraper() call, with the same
    numbers printed to the console summary line."""
    if not _configured():
        return
    text = (
        f"✅ הרצה הסתיימה\n"
        f"{stats['groups_scanned']}/{stats['total']} קבוצות נסרקו\n"
        f"{stats['posts_seen']} פוסטים נבדקו | {stats['prefiltered']} סוננו מראש | {stats['llm_parsed']} נשלחו ל-LLM\n"
        f"{stats['added']} דירות נוספו\n"
        f"{stats['gmaps_calls']} קריאות Google Maps החודש"
    )
    if stats.get("checkpoint_skipped"):
        text += f"\n⚠️ {stats['checkpoint_skipped']} קבוצה/ות דולגו עקב checkpoint"
    _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
