"""
=== Alert-email listing source ===

A second listing source next to the Facebook groups: the sites' own
saved-search alert emails (Yad2, ...), read from a Gmail label over IMAP.
Reading your own mailbox needs nobody's permission — no bot protection to
get around, and no risk to the home IP the Facebook scraper depends on.
Pattern from bgu-housing-bot's yad2_mail.py.

The mailbox is opened READ-ONLY (`select(..., readonly=True)` + BODY.PEEK),
so this can never delete, move or even mark an email as read. Credentials
come from .env via env.get_alerts_imap_credentials() and are never printed.

Step 1 (this module today): fetch recent alert emails and extract their text,
dumped to ALERTS_DUMP_DIR via `apartment_bot.py --mail-dump`, so the per-listing
split can be written against real emails instead of a guessed template.
"""
import email
import html
import imaplib
import re
import threading
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote

import env
from config import ALERTS_IMAP_HOST, ALERTS_LOOKBACK_DAYS, ALERTS_DUMP_DIR
from core import storage
from core.evaluate import _content_prefilter, flush_pending_listings, process_candidate_listing
from core.util import _safe_print

# Stands in for a Facebook group URL in the DB and --stats, so this source's yield shows as one row.
YAD2_ALERTS_SOURCE = "https://www.yad2.co.il/my-alerts"
_LABEL = "Yad2 alerts"

_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK_END_RE = re.compile(r"<br\s*/?>|</(?:p|div|tr|td|li|h\d)>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# The daily digest ("התראות יד2 - ריכוז מודעות יומי", layout as of 2026-11-08): a block per listing
# — price line, street, "neighborhood | type | N חדרים | M מ״ר", then a "לפרטים נוספים" link —
# between the "found N new listings" line and "לכל המודעות". The item links sit in the HTML only,
# behind a click-tracking redirect that's decoded here, never followed.
_YAD2_SENDER = "yad2.co.il"
_DIGEST_START = "מודעות חדשות שמתאימות לחיפוש שלך"
_DIGEST_END = "לכל המודעות"
_LISTING_END = "לפרטים נוספים"
_YAD2_ITEM_RE = re.compile(r"yad2\.co\.il/realestate/item/([a-z0-9]+)", re.I)
_DIRECTION_MARKS_RE = re.compile("[‎‏‪-‮⁦-⁩]")
_PRICE_LINE_RE = re.compile(r"^([\d,]+)\s*₪$")
_ROOMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*חדרים")
_SIZE_RE = re.compile(r"(\d+)\s*מ[״\"]ר")
# The listings never name their city; the saved search's title does ("דירות להשכרה בגבעתיים"), and a
# bare street geocodes wrong where both target cities have it (כ"ג 11: 2.9 km bare, 1.3 km in Givatayim).
_SEARCH_CITY_RE = re.compile(r"להשכרה ב(\S.*)$", re.M)


def scan_alert_emails(sheet, seen_urls: set, live: bool) -> dict:
    """
    Runs every listing in the last ALERTS_LOOKBACK_DAYS of Yad2 digest emails through the same
    pre-filters, evaluation, sheet write and Telegram alert as a Facebook post. No LLM call: the
    email already states price, rooms and street. Returns stats shaped like a group scan's.
    """
    stats = {"added": 0, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}
    pending_rows, pending_seen_urls, pending_records, pending_telegram = [], set(), [], []
    stats_lock = threading.Lock()
    for msg in fetch_recent_messages(ALERTS_LOOKBACK_DAYS):
        if _YAD2_SENDER not in _header(msg, "From"):
            continue
        listings = parse_yad2_digest(msg)
        if not listings:
            _safe_print(f"WARNING: [{_LABEL}] no listings read from '{_header(msg, 'Subject')}' "
                        f"({_header(msg, 'Date')}) — the email layout may have changed.")
            continue
        post_date = parsedate_to_datetime(_header(msg, "Date")).astimezone().strftime("%d/%m")
        for listing in listings:
            stats["posts_seen"] += 1
            url = listing["url"]
            if url in seen_urls or storage.should_skip(url):
                continue
            text = _listing_text(listing)
            rejection = _content_prefilter(text, url)
            if rejection:
                verdict, analysis, message = rejection
                _safe_print(f"    [{_LABEL}] Pre-filtered: {message}")
                storage.record_post(url, YAD2_ALERTS_SOURCE, text, verdict, analysis=analysis)
                stats["prefiltered"] += 1
                continue
            address = ", ".join(part for part in (listing["street"], listing["city"]) if part)
            parsed = {"rooms": listing["rooms"], "price": listing["price"], "address": address}
            process_candidate_listing(url, text, post_date, YAD2_ALERTS_SOURCE, _LABEL, live, seen_urls,
                                      pending_rows, pending_seen_urls, pending_records, stats, stats_lock,
                                      pending_telegram, parsed=parsed)
    flush_pending_listings(sheet, seen_urls, pending_rows, pending_seen_urls, pending_records,
                           pending_telegram, stats, live, _LABEL)
    return stats


def parse_yad2_digest(msg: Message) -> list[dict]:
    """
    The listings in one Yad2 daily digest: url, price, rooms, size, street, neighborhood, kind, city.
    [] when the email doesn't read as a digest, or its listings and item links don't pair up
    one to one — better to skip an email than attach a price to the wrong apartment.
    """
    text = _DIRECTION_MARKS_RE.sub("", message_text(msg))
    start, end = text.find(_DIGEST_START), text.rfind(_DIGEST_END)
    if start < 0 or end < start:
        return []
    listings = [_parse_listing_block(block) for block in text[start:end].split(_LISTING_END)[:-1]]
    item_ids = list(dict.fromkeys(_YAD2_ITEM_RE.findall(unquote(_html_markup(msg)))))
    if not listings or len(item_ids) != len(listings) or not all(listings):
        return []
    city = _SEARCH_CITY_RE.search(text[start:end])
    return [dict(listing, url=f"https://www.yad2.co.il/realestate/item/{item_id}",
                 city=city.group(1).strip() if city else "")
            for listing, item_id in zip(listings, item_ids)]


def dump_alert_emails(days: int = ALERTS_LOOKBACK_DAYS, out_dir: str = ALERTS_DUMP_DIR) -> int:
    """Writes each recent alert email's text to out_dir as one .txt file; returns how many were written."""
    messages = fetch_recent_messages(days)
    target = Path(out_dir)
    target.mkdir(exist_ok=True)
    for i, msg in enumerate(messages, 1):
        header = f"From: {_header(msg, 'From')}\nDate: {_header(msg, 'Date')}\nSubject: {_header(msg, 'Subject')}\n\n"
        (target / f"alert_{i:03}.txt").write_text(header + message_text(msg), encoding="utf-8")
    return len(messages)


def fetch_recent_messages(days: int) -> list[Message]:
    """Alert emails from the last `days` days in the configured folder, oldest first. Read-only."""
    user, password, folder = env.get_alerts_imap_credentials()
    if not user or not password:
        raise RuntimeError("ALERTS_IMAP_USER / ALERTS_IMAP_PASSWORD missing from .env")
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(ALERTS_IMAP_HOST)
    try:
        imap.login(user, password)
        status, _ = imap.select(_quote_folder(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot open mail folder {folder!r} — check ALERTS_IMAP_FOLDER matches the Gmail label")
        _, data = imap.search(None, "SINCE", since)
        messages = []
        for msg_id in data[0].split():
            _, parts = imap.fetch(msg_id, "(BODY.PEEK[])")
            raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
            if raw:
                messages.append(email.message_from_bytes(raw))
        return messages
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def message_text(msg: Message) -> str:
    """The email's readable text: the text/plain part when there is one, else its HTML converted to text."""
    plain, html_parts = [], []
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if content_type == "text/plain" else html_parts).append(text)
    if plain:
        return "\n".join(plain).strip()
    return "\n".join(_html_to_text(h) for h in html_parts).strip()


def _parse_listing_block(block: str) -> dict | None:
    lines = [line.strip() for line in block.split("\n") if line.strip()]
    detail_index = next((i for i, line in enumerate(lines) if "|" in line and _ROOMS_RE.search(line)), None)
    if not detail_index:
        return None
    prices = [m.group(1) for line in lines[:detail_index] if (m := _PRICE_LINE_RE.match(line))]
    if not prices:
        return None
    details = [part.strip() for part in lines[detail_index].split("|")]
    size = _SIZE_RE.search(lines[detail_index])
    return {
        "price": int(prices[-1].replace(",", "")),
        "rooms": float(_ROOMS_RE.search(lines[detail_index]).group(1)),
        "size": int(size.group(1)) if size else None,
        "street": lines[detail_index - 1],
        "neighborhood": details[0],
        "kind": details[1] if len(details) > 1 else "דירה",
    }


def _listing_text(listing: dict) -> str:
    # Stored as the post's raw text and run through the same text pre-filters as a Facebook post,
    # so it's written the way a post would say it.
    size = f", {listing['size']} מ\"ר" if listing["size"] else ""
    return (f"להשכרה {listing['kind']} {listing['rooms']:g} חדרים{size}\n"
            f"כתובת: {listing['street']}, שכונת {listing['neighborhood']}, {listing['city']}\n"
            f"מחיר: {listing['price']:,} ₪ לחודש\n"
            f"(מתוך התראת יד2)")


def _html_markup(msg: Message) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload is not None:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def _html_to_text(markup: str) -> str:
    # Crude on purpose: block tags become newlines (the seams a per-listing split needs), the rest is
    # dropped. html.unescape matters: Hebrew in mail templates often arrives as numeric entities.
    text = _STYLE_RE.sub(" ", markup)
    text = _BLOCK_END_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub(" ", text))
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _header(msg: Message, name: str) -> str:
    return str(make_header(decode_header(msg.get(name, ""))))


def _quote_folder(folder: str) -> str:
    # Gmail labels can contain spaces and slashes, which IMAP needs quoted.
    return f'"{folder}"' if not folder.startswith('"') else folder
