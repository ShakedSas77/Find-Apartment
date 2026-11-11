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
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

import config
from core import scoring
from core import storage
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


def _whatsapp_url(fields: dict) -> str | None:
    # The bot stays read-only on Facebook and never messages anyone; this link hands the first
    # message to the user, one tap from the alert.
    phone = fields.get("contact_phone")
    if not phone:
        return None
    address = (fields.get("address") or "").strip()
    about = f"על הדירה ב{address}" if address else "על הדירה"
    text = f"היי, ראיתי את המודעה {about}. היא עדיין רלוונטית?"
    return f"https://wa.me/972{phone[1:]}?" + urlencode({"text": text})


def _link_row(post_url: str, maps_url: str | None = None, whatsapp_url: str | None = None) -> list:
    row = [{"text": "🔗 פתח פוסט", "url": post_url}]
    if maps_url:
        row.append({"text": "🗺 פתח במפות", "url": maps_url})
    if whatsapp_url:
        row.append({"text": "💬 וואטסאפ", "url": whatsapp_url})
    return row


def build_link_keyboard(post_url: str, maps_url: str | None = None, whatsapp_url: str | None = None) -> dict:
    """reply_markup for every new listing message: "open post", plus "open in Maps" when the street is
    known and "WhatsApp" when the post had a mobile number. The ⭐/🗑 vote buttons and the 0-10 rating
    rows were dropped from new messages."""
    return {"inline_keyboard": [_link_row(post_url, maps_url, whatsapp_url)]}


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
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _format_listing_message(fields, score),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        # The button opens the listing's own URL, so a non-Facebook source (Yad2) links to its page too.
        "reply_markup": build_link_keyboard(post_url, _maps_url(fields), _whatsapp_url(fields)),
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


def send_run_started(info: dict) -> str | None:
    """
    Sent once at the start of a live run_scraper() call — never on a dry run. info: see
    _format_run_started. Returns the message_id, which update_run_progress() edits as groups finish.
    """
    if not _configured():
        return None
    result = _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": _format_run_started(info), "parse_mode": "HTML"})
    return str(result["message_id"]) if result else None


def update_run_progress(message_id: str, info: dict, progress_text: str):
    """Rewrites the run-started message with progress_text (format_run_progress()) under it."""
    if not _configured():
        return
    text = _format_run_started(info) + "\n\n" + progress_text
    _call("editMessageText", {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"})


def format_run_progress(done: int, total: int, added: int, left_s: float | None) -> str:
    """The progress line under the run-started message. left_s: estimated seconds left, None = not known yet."""
    filled = round(_RUN_PROGRESS_BAR_WIDTH * done / total) if total else _RUN_PROGRESS_BAR_WIDTH
    bar = "▓" * filled + "░" * (_RUN_PROGRESS_BAR_WIDTH - filled)
    line = f"{bar} {done}/{total} קבוצות · {added} נוספו"
    if done >= total:
        return f"✔️ {line} · הסיכום בהודעה הבאה"
    if left_s is None:
        return f"⏳ {line} · מעריך זמן..."
    if left_s < 60:
        return f"⏳ {line} · כמעט סיום"
    return f"⏳ {line} · עוד ~{_ago(timedelta(seconds=left_s))}"


def send_run_finished(stats: dict):
    """Sent once at the end of a live run_scraper() call. stats: see _format_run_finished."""
    if not _configured():
        return
    _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": _format_run_finished(stats),
                          "parse_mode": "HTML", "disable_web_page_preview": True})


# How a rejected post is named in the run-finished breakdown.
_REJECTION_LABELS = {
    storage.VERDICT_REJECTED_PRICE: "מחיר",
    storage.VERDICT_REJECTED_ROOMS: "חדרים",
    storage.VERDICT_REJECTED_DISTANCE: "מרחק",
    storage.VERDICT_REJECTED_LOCATION: "מיקום",
    storage.VERDICT_REJECTED_ENTRY_DATE: "תאריך כניסה",
    storage.VERDICT_PRICE_UNKNOWN: "בלי מחיר",
    storage.VERDICT_DUPLICATE_LISTING: "פרסום חוזר",
    storage.VERDICT_PARSE_FAILED: "לא פוענח",
}
# A run-finished message lists at most this many new listings; Telegram caps a message at 4096 characters.
_RUN_FINISHED_MAX_LISTINGS = 10
_RUN_PROGRESS_BAR_WIDTH = 12


def _format_run_started(info: dict) -> str:
    """
    info: groups (count), new_groups (never scanned before), alerts (Yad2 emails read too),
    previous (storage.get_last_successful_live_run() row, or None), now (datetime).
    """
    groups_line = f"🔎 {info['groups']} קבוצות פייסבוק"
    if info.get("new_groups"):
        groups_line += f" · {info['new_groups']} חדשות (סריקה ראשונה, ארוכה יותר)"
    lines = [f"▶️ <b>סריקה התחילה</b> · {info['now']:%H:%M}", groups_line]
    if info.get("alerts"):
        lines.append("📧 + התראות יד2 מהמייל")
    lines.append(f"🎯 {config.MIN_ROOMS:g}–{config.MAX_ROOMS:g} חדרים · ₪{config.MIN_PRICE:,}–{config.MAX_PRICE:,}"
                 f" · עד {config.MAX_WALKING_DISTANCE_KM:g} ק\"מ")
    previous = info.get("previous")
    if previous and previous.get("started_at"):
        ago = _ago(info["now"] - datetime.fromisoformat(previous["started_at"]))
        lines.append(f"⏮ סריקה קודמת: לפני {ago} · {previous.get('added') or 0} דירות נוספו")
    return "\n".join(lines)


def _format_run_finished(stats: dict) -> str:
    """
    stats: the console summary numbers (groups_scanned, total, posts_seen, prefiltered, llm_parsed,
    added, gmaps_calls, checkpoint_skipped, errors, warnings) plus duration_s, activity
    (storage.get_run_activity()) and not_member_groups (count).
    """
    activity = stats.get("activity") or {"verdicts": {}, "models": {}, "added": []}
    added = activity["added"]
    lines = [f"✅ <b>סריקה הסתיימה</b> · {_ago(timedelta(seconds=stats.get('duration_s', 0)))}"]

    if added:
        yad2 = sum(1 for listing in added if "yad2.co.il" in listing["url"])
        sources = f" ({len(added) - yad2} פייסבוק, {yad2} יד2)" if yad2 else ""
        lines.append(f"🏠 <b>{len(added)} דירות חדשות</b>{sources}")
        lines += [_run_listing_line(listing) for listing in added[:_RUN_FINISHED_MAX_LISTINGS]]
        if len(added) > _RUN_FINISHED_MAX_LISTINGS:
            lines.append(f"  ועוד {len(added) - _RUN_FINISHED_MAX_LISTINGS} — בגיליון")
    else:
        lines.append("🏠 אין דירות חדשות הפעם")

    lines.append(f"🔎 {stats['groups_scanned']}/{stats['total']} קבוצות · {stats['posts_seen']:,} פוסטים · "
                 f"{stats['prefiltered']:,} סוננו מראש · {stats['llm_parsed']} נשלחו ל-AI")
    rejections = sorted(((activity["verdicts"].get(verdict, 0), label) for verdict, label in _REJECTION_LABELS.items()),
                        reverse=True)
    if any(n for n, _ in rejections):
        lines.append("🚫 נדחו: " + " · ".join(f"{label} {n}" for n, label in rejections if n))
    if activity["models"]:
        lines.append("🤖 " + " · ".join(f"{_escape(model)} {n}" for model, n in activity["models"].items()))
    if stats.get("gmaps_calls"):
        lines.append(f"🗺 {stats['gmaps_calls']} קריאות Google Maps החודש")
    if stats.get("not_member_groups"):
        lines.append(f"👥 {stats['not_member_groups']} קבוצות שלא הצטרפת אליהן — ראה/י את לשונית Groups")
    if stats.get("checkpoint_skipped"):
        lines.append(f"⚠️ {stats['checkpoint_skipped']} קבוצות דולגו עקב checkpoint — יש להריץ עם --headful")
    if stats.get("errors") or stats.get("warnings"):
        lines.append(f"⚠️ {stats.get('errors', 0)} שגיאות, {stats.get('warnings', 0)} אזהרות — ראו את קובץ הלוג")
    return "\n".join(lines)


def _run_listing_line(listing: dict) -> str:
    parts = []
    if listing.get("rooms_val"):
        parts.append(f"{listing['rooms_val']:g} חד'")
    if listing.get("price_val"):
        parts.append(f"₪{int(listing['price_val']):,}")
    distance = listing.get("distance_text") or ""
    if distance.replace(".", "", 1).isdigit():
        parts.append(f"{distance} ק\"מ")
    address = _escape(listing.get("address") or "פתח מודעה")
    url = html.escape(listing["url"], quote=True)
    return "• " + " · ".join(parts + [f'<a href="{url}">{address}</a>'])


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} דק'"
    if minutes < 48 * 60:
        return f"{minutes // 60}:{minutes % 60:02d} שע'"
    return f"{minutes // (24 * 60)} ימים"


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
    if stats.get("errors") or stats.get("warnings"):
        text += f"\n⚠️ {stats.get('errors', 0)} שגיאות, {stats.get('warnings', 0)} אזהרות — ראו את קובץ הלוג"
    _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
