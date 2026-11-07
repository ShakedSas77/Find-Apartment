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
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path

import env
from config import ALERTS_IMAP_HOST, ALERTS_LOOKBACK_DAYS, ALERTS_DUMP_DIR

_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK_END_RE = re.compile(r"<br\s*/?>|</(?:p|div|tr|td|li|h\d)>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


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
