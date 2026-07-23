import argparse
import json
import sys
import time
import os
import re
import random
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

import ollama
import gspread
import googlemaps
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
from pydantic import BaseModel
from typing import Optional

try:
    from playwright_stealth import stealth_sync, StealthConfig
except ImportError:
    stealth_sync = None
    StealthConfig = None

from config import (
    CREDENTIALS_FILE, TARGET_URLS,
    MIN_PRICE, MAX_PRICE, DESTINATION_ADDRESS,
    SCROLL_COUNT, SCROLL_DELAY_MS, LOCATIONS,
    MIN_ROOMS, MAX_ROOMS, ROOMS_PRE_FILTER_REGEX,
    NEGATIVE_KEYWORDS, ROOMMATE_KEYWORDS, EXCLUDED_LOCATIONS,
    GEMINI_MAX_CONSECUTIVE_ERRORS, GEMINI_MODEL, LOGIN_MAX_ATTEMPTS,
    MAX_CONCURRENT_GROUPS, SHEET_HEADERS,
    GMAPS_MONTHLY_CAP, GMAPS_ON_CAP,
    MAX_POST_AGE_DAYS, GMAPS_TARGET_CITIES, GMAPS_VALIDATE_ADDRESSES,
    GMAPS_DISTANCE_ONLY_CONFIDENT_ADDRESS, MAX_WALKING_DISTANCE_KM,
    INCLUDE_PRICE_UNKNOWN, STEALTH_ENABLED, PRUNE_DEAD_LINKS_ENABLED
)
from prompts import get_apartment_prompt_improved
import storage

class ApartmentData(BaseModel):
    """JSON schema forced onto Gemini's response (response_schema) — eliminates parsing failures on the Gemini path."""
    rooms: Optional[float] = None
    price: Optional[int] = None
    arnona: Optional[str] = None
    vaad: Optional[str] = None
    shelter: Optional[bool] = None
    parking: Optional[str] = None
    entry_date: Optional[str] = None
    floor: Optional[str] = None
    elevator: Optional[bool] = None
    is_agent: Optional[bool] = None
    address: Optional[str] = None

def map_bool(val):
    if val is True: return "כן"
    if val is False: return "לא"
    return ""

# --- Load environment variables ---
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GMAPS_API_KEY = os.getenv("GMAPS_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")

if not GEMINI_API_KEY or not GMAPS_API_KEY or not SHEET_ID:
    print("ERROR: Missing GEMINI_API_KEY, GMAPS_API_KEY, or SHEET_ID in .env file")
    sys.exit(1)

# --- API Clients ---
client = genai.Client(api_key=GEMINI_API_KEY)
gmaps_client = googlemaps.Client(key=GMAPS_API_KEY)

GEMINI_EXHAUSTED = False
GEMINI_ERROR_COUNT = 0

# On Linux, Chrome's sandbox can fail to initialize in some VM/container
# environments, hanging navigation indefinitely rather than erroring cleanly
# (verified 2026-07-18: a bare `google-chrome --no-sandbox --disable-gpu` CLI
# call loaded facebook.com in 11s, while Playwright's launch — which strips
# --no-sandbox for anti-detection reasons, fine on Windows — hung on
# page.goto() past a 30s timeout on every single group). Add both flags back
# on Linux only; Windows keeps the original anti-detection args untouched.
_CHROME_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--autoplay-policy=user-gesture-required"]
if sys.platform.startswith("linux"):
    _CHROME_LAUNCH_ARGS += ["--no-sandbox", "--disable-gpu"]

# Strips invisible BIDI characters that Facebook injects and that break regexes
BIDI_RE = re.compile(r'[‎‏‪-‮⁦-⁩]')

# googlemaps sends GMAPS_API_KEY as a URL query param (unlike Gemini, which uses
# a header) — on a network-level failure (not a clean API error response), the
# underlying requests/urllib3 exception's str() embeds the full request URL,
# key included. Strip it before any exception text reaches a print/log.
_API_KEY_QUERY_RE = re.compile(r'([?&]key=)[^&\s]+')

def _redact_api_key(text: str) -> str:
    return _API_KEY_QUERY_RE.sub(r'\1REDACTED', text)

# article.inner_text() also includes the comments section below the post — cut at
# the first marker so a price/detail from another user's comment (not the poster's)
# doesn't contaminate data extraction.
_COMMENT_SECTION_RE = re.compile(
    r'View more comments|View \d+ repl|Write a (?:public )?comment|Submit your first comment|Most relevant'
)

def _strip_comment_section(text: str) -> str:
    match = _COMMENT_SECTION_RE.search(text)
    return text[:match.start()].strip() if match else text

# Price "second chance" — only a number found within ~25 chars of a price
# word/marker, not any 4-5 digit number in the text (so it doesn't grab a phone
# number, someone else's comment, etc.). The \D (non-digit) gap blocks crossing
# over another number sitting between the candidate and the marker — so "was
# 6500, now 7200 ש"ח" doesn't attribute the ש"ח to 6500 even though it's within
# the 25-char window.
_PRICE_MARKER = r'(?:₪|ש["״]?ח|שכ["״]?ד|שכר\s*דירה|מחיר|לחודש)'
_PRICE_CONTEXT_RE = re.compile(
    rf'{_PRICE_MARKER}\D{{0,25}}(?<![0-9])([0-9]{{4,5}})(?![0-9])'
    rf'|(?<![0-9])([0-9]{{4,5}})(?![0-9])\D{{0,25}}{_PRICE_MARKER}'
)

# ─── Concurrency primitives (groups scan in parallel tabs) ────────────────────────
_print_lock = threading.Lock()
_sheet_lock = threading.Lock()  # guards seen_urls reads/writes AND sheet writes together
_gemini_lock = threading.Lock()
_checkpoint_lock = threading.Lock()
_resume_event = threading.Event()
_resume_event.set()  # set = running; cleared = paused for a checkpoint on some tab
_headless_checkpoint_hit = False  # set once any group hits a checkpoint in --headless mode; other groups then skip fast

class HeadlessCheckpointAbort(Exception):
    """Raised when a checkpoint/CAPTCHA is detected in headless mode — can't be solved manually, so this group is stopped."""

def _safe_print(msg: str):
    with _print_lock:
        print(msg)

_stealth_warned = False

def _apply_stealth(page):
    """
    Masks automation fingerprints (navigator.webdriver, headless UA artifacts,
    missing chrome.runtime) via tf-playwright-stealth. Must run on a page
    BEFORE its first navigation — the patches are add_init_script based.
    Failure is never fatal: the bot ran for months without stealth.
    """
    global _stealth_warned
    if not STEALTH_ENABLED:
        return
    if stealth_sync is None:
        if not _stealth_warned:
            _stealth_warned = True
            _safe_print("WARNING: tf-playwright-stealth not installed — running without stealth patches. Run: pip install -r requirements.txt")
        return
    try:
        # navigator_user_agent/navigator_languages OFF: the package fakes them with a
        # hardcoded Chrome 95 UA and en-US — an ancient UA is a louder bot signal than
        # what it hides, and it would stomp the real-profile fingerprint the context
        # was deliberately given (verified 2026-07-18 against tf-playwright-stealth 1.2.0).
        stealth_sync(page, StealthConfig(navigator_user_agent=False, navigator_languages=False))
    except Exception as e:
        if not _stealth_warned:
            _stealth_warned = True
            _safe_print(f"WARNING: stealth patch failed, continuing without it: {e}")

# ─── Helper Functions ─────────────────────────────────────────────────────────────

def _is_visible(locator) -> bool:
    try:
        return locator.first.is_visible()
    except Exception:
        return False

# Converts the relative date Facebook shows ("6h", "1d", "3w", plus the longer
# forms Facebook sometimes renders: "3 hrs", "1 day", "2 wks") to an absolute date (DD/MM)
_RELATIVE_DATE_RE = re.compile(
    r'^(\d+)\s*(s|sec|secs|second|seconds|'
    r'm|min|mins|minute|minutes|'
    r'h|hr|hrs|hour|hours|'
    r'd|day|days|'
    r'w|wk|wks|week|weeks)$',
    re.IGNORECASE
)
_RELATIVE_DATE_UNIT_ALIASES = {
    's': 's', 'sec': 's', 'secs': 's', 'second': 's', 'seconds': 's',
    'm': 'm', 'min': 'm', 'mins': 'm', 'minute': 'm', 'minutes': 'm',
    'h': 'h', 'hr': 'h', 'hrs': 'h', 'hour': 'h', 'hours': 'h',
    'd': 'd', 'day': 'd', 'days': 'd',
    'w': 'w', 'wk': 'w', 'wks': 'w', 'week': 'w', 'weeks': 'w',
}
_RELATIVE_DATE_UNITS = {
    's': lambda v: timedelta(seconds=v),
    'm': lambda v: timedelta(minutes=v),
    'h': lambda v: timedelta(hours=v),
    'd': lambda v: timedelta(days=v),
    'w': lambda v: timedelta(weeks=v),
}
_YESTERDAY_RE = re.compile(r'^yesterday$', re.IGNORECASE)

# Detects absolute English-language dates that Facebook sometimes shows instead of relative text (e.g. "July 9 at 5:50 PM")
_MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10, 'october': 10,
    'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}
_ABSOLUTE_DATE_RE = re.compile(r'\b([A-Za-z]+)\s+(\d{1,2})\b')

def _parse_absolute_fb_date(text: str) -> str | None:
    match = _ABSOLUTE_DATE_RE.search(text)
    if not match:
        return None
    month = _MONTH_NAMES.get(match.group(1).lower())
    if not month:
        return None
    try:
        now = datetime.now()
        dt = datetime(now.year, month, int(match.group(2)))
        if dt > now:
            dt = dt.replace(year=now.year - 1)
        return dt.strftime("%d/%m")
    except ValueError:
        return None

_date_warning_lock = threading.Lock()
_unparsed_date_logged = False

def relative_to_date(rel: str) -> str:
    """
    Converts the relative/absolute date Facebook shows to a DD/MM date. The bot
    assumes an English-language Facebook UI (not Hebrew) — see README. An
    unrecognized format passes through unchanged, with a one-time-per-run
    warning log so a future locale change doesn't degrade silently.
    """
    global _unparsed_date_logged
    text = (rel or "").strip()
    if not text:
        return rel

    if _YESTERDAY_RE.match(text):
        return (datetime.now() - timedelta(days=1)).strftime("%d/%m")

    match = _RELATIVE_DATE_RE.match(text)
    if match:
        value = int(match.group(1))
        unit = _RELATIVE_DATE_UNIT_ALIASES[match.group(2).lower()]
        return (datetime.now() - _RELATIVE_DATE_UNITS[unit](value)).strftime("%d/%m")

    absolute = _parse_absolute_fb_date(text)
    if absolute:
        return absolute

    with _date_warning_lock:
        already_logged = _unparsed_date_logged
        _unparsed_date_logged = True
    if not already_logged:
        _safe_print(f"WARNING: unrecognized Facebook post-date format: {rel!r} — passing through unchanged. "
                    f"If Facebook's UI locale changed, date parsing may need an update.")
    return rel

def _normalize_bimonthly_fee(raw: str):
    """
    Arnona (municipal tax)/vaad bayit (building fee) are standardly billed once
    every two months in Israel — if the post stated a monthly amount, double it
    to the bi-monthly value. Returns an integer only (not a string), with no
    currency/unit marks.
    """
    if not raw:
        return ""
    digits = re.sub(r'[^\d]', '', raw)
    if not digits:
        return 0 if "כלול" in raw else ""
    value = int(digits)
    if re.search(r'לחודש(?!יים)', raw):
        value *= 2
    return value

_FLOOR_ORDINALS = {
    'קרקע': 0, 'ראשונה': 1, 'שניה': 2, 'שנייה': 2, 'שלישית': 3, 'רביעית': 4,
    'חמישית': 5, 'שישית': 6, 'שביעית': 7, 'שמינית': 8, 'תשיעית': 9, 'עשירית': 10,
}
_FLOOR_DIGIT_RE = re.compile(r'\d+')

def _parse_floor(raw: str):
    """
    Returns floor as an integer. 'קרקע' (ground) = 0. Hebrew ordinal words
    (first/second/...) and the 'X מתוך Y' (X of Y) pattern are supported. An
    explicit digit (if present) wins over 'קרקע' — "1 above ground floor" = 1, not 0.
    """
    if not raw:
        return ""
    raw = raw.strip()
    match = _FLOOR_DIGIT_RE.search(raw)
    if match:
        return int(match.group(0))
    for word, num in _FLOOR_ORDINALS.items():
        if word in raw:
            return num
    return ""

def _clean_post_for_llm(raw_text: str) -> str:
    """Strips URLs (useless for data extraction) and excess whitespace/blank lines — saves tokens."""
    clean = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    clean = re.sub(r'\n{2,}', '\n', clean)
    clean = re.sub(r'[ \t]{2,}', ' ', clean)
    return clean.strip()

_NOT_AGENT_RE = re.compile(r'ללא\s+תיווך|לא\s+מתיווך|בלי\s+תיווך|לא\s+תיווך')
_AGENT_SIGNAL_RE = re.compile(r'תיווך|נדל["״]?ן')

def _detect_agent(text: str, llm_is_agent):
    """
    Adds a deterministic signal on top of the model's judgment: the word 'תיווך'
    (agency) or a real-estate agency name in the text = definitely an agent.
    Explicit negation phrases ("ללא תיווך"/no agent, etc.) don't count as a
    positive signal — left to the model.
    """
    if _NOT_AGENT_RE.search(text):
        return llm_is_agent
    if _AGENT_SIGNAL_RE.search(text):
        return True
    return llm_is_agent

# 'שותפ'/'שותף' (roommate) alone disqualifies roommate posts, but a landlord who
# writes "suitable for a couple or 2 roommates" is describing tenant-type
# flexibility — not renting out a single room. If 'זוג' (couple) appears nearby
# (within ~30 chars), don't disqualify.
# The lookbehind (?<!מי) prevents matching 'מיזוג' (air conditioning, very common
# in listings); the lookahead (?!י) prevents 'זוגי'/'זוגית' (double bed). [פף]
# also covers the singular form "שותף".
_ROOMMATE_COUPLE_EXCEPTION_RE = re.compile(
    r'(?<!מי)זוג(?!י).{0,30}שות[פף]|שות[פף].{0,30}(?<!מי)זוג(?!י)',
    re.DOTALL
)

_PARKING_NONE_RE = re.compile(r'אין\s*חניה|בלי\s*חניה|ללא\s*חניה|^אין$')
_PARKING_PRIVATE_RE = re.compile(r'פרטי|טאבו|מקור|צמוד|תת\s*קרקעי|חניון')
_PARKING_STREET_RE = re.compile(r'רחוב|ציבור|חופשית')

def _classify_parking(raw: str) -> str:
    """Classifies the model's free-text parking field into one of three fixed categories, or blank if unclear/unstated."""
    if not raw:
        return ""
    raw = raw.strip()
    if _PARKING_NONE_RE.search(raw):
        return "לא"
    if _PARKING_PRIVATE_RE.search(raw):
        return "פרטית"
    if _PARKING_STREET_RE.search(raw):
        return "ברחוב"
    return ""

_FOREIGN_LETTERS_RE = re.compile(r'[^\u0590-\u05FF\d\s.,\-\/\\\'"()\[\]]+')

def _strip_foreign_letters(text: str) -> str:
    """
    Deterministic backstop for the prompt's 'Hebrew only' rule. Strips any
    letter that isn't Hebrew, a digit, or punctuation, to prevent foreign-
    language hallucinations from the model.
    """
    if not text:
        return text
    cleaned = _FOREIGN_LETTERS_RE.sub('', text)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' \t-–—,/')
    return cleaned

_IMMEDIATE_RE = re.compile(r'מיידי|מיד|עכשיו|כניסה\s*מיידית|היום')
_DATE_DDMM_RE = re.compile(r'(\d{1,2})[./](\d{1,2})(?:[./]\d{2,4})?')

def _normalize_entry_date(text: str) -> str:
    """
    Normalizes the entry date, strips foreign-language text, converts
    "immediate"-type phrases to the standard "מיידי", and zero-pads a
    numeric day.month(.year) date (e.g. "1.8" / "1.8.26") to "DD/MM",
    dropping the year. Non-numeric dates (e.g. "ספטמבר") pass through as-is.
    """
    if not text:
        return ""
    text = _strip_foreign_letters(text)
    if _IMMEDIATE_RE.search(text):
        return "מיידי"
    m = _DATE_DDMM_RE.fullmatch(text.strip().rstrip('./'))
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{day:02d}/{month:02d}"
    return text

def _reject_hallucinated_address(address: str, source_text: str) -> str:
    """
    qwen2.5 repeatedly invents "נמל התעופה" (airport) as an address for posts that
    never mention an airport at all — even with an explicit anti-invention prompt
    rule. Deterministic denylist: reject it unless the source text actually says so.
    """
    if address and "נמל התעופה" in address and "נמל התעופה" not in source_text and "שדה תעופה" not in source_text:
        return ""
    return address

def _warn_if_fee_implausible(label: str, value, max_bimonthly: int):
    if not value:
        return
    if value > max_bimonthly:
        _safe_print(f"\n    WARNING: {label} looks unusually high ({value}) - verify manually.")

def _evaluate_post_data(data: dict, text: str, group_url: str = "") -> tuple[str, dict]:
    """
    Runs the threshold checks (rooms/price, including the regex "second chance")
    and all field normalization. Shared between _scan_group and --reparse-rejected
    so the logic stays identical. Returns (verdict, fields) — fields holds what's
    needed to build a sheet row when verdict is storage.VERDICT_ADDED.
    """
    rooms = data.get("rooms")
    price = data.get("price")

    try:
        rooms_val = float(rooms) if rooms is not None else 0.0
        rooms_missing_or_invalid = rooms is None
    except (TypeError, ValueError):
        rooms_val = 0.0
        rooms_missing_or_invalid = True

    try:
        price_val = float(price) if price is not None else 0.0
        price_missing_or_invalid = price is None
    except (TypeError, ValueError):
        price_val = 0.0
        price_missing_or_invalid = True

    # --- Error correction and "second chance" for price ---
    # The "second chance" (searching the text for a number) only runs when the
    # model returned no price at all — if the model did return a real value
    # (even if out of budget, e.g. 7200), it's not overridden by some other
    # number just because it's near a price marker in the text (could be "old
    # price", annual committee fee, etc.) — that's exactly what created false
    # positives before.
    if not (MIN_PRICE <= price_val <= MAX_PRICE):
        clean_text = BIDI_RE.sub('', text)
        clean_text = re.sub(r'(?<=[0-9])[.,\s](?=[0-9]{3}(?![0-9]))', '', clean_text)
        possible_prices = [
            int(m.group(1) or m.group(2))
            for m in _PRICE_CONTEXT_RE.finditer(clean_text)
        ]
        if possible_prices:
            valid_prices = [p for p in possible_prices if MIN_PRICE <= p <= MAX_PRICE]
            if valid_prices and price_missing_or_invalid:
                price_val = float(valid_prices[0])
            elif price_val < 3000 or price_val > 30000:
                price_val = float(possible_prices[0])

    # --- Second chance for rooms: only when the model returned no valid room count at all ---
    # (doesn't override a real numeric value the model did return, even if outside the target range)
    if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS) and rooms_missing_or_invalid:
        clean_text_rooms = BIDI_RE.sub('', text)
        room_matches = [float(r) for r in re.findall(r'([1-9](?:\.5)?)\s*חד', clean_text_rooms)]
        valid_rooms = [r for r in room_matches if MIN_ROOMS <= r <= MAX_ROOMS]
        if valid_rooms:
            rooms_val = valid_rooms[0]

    if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS):
        return storage.VERDICT_REJECTED_ROOMS, {"rooms_val": rooms_val, "price_val": price_val}

    # Price 0/missing after both paths (LLM + regex "second chance") = "no price
    # stated in the post" ("contact for details") — a relevant lead, not a
    # rejection like a real price that's simply out of range.
    price_unknown = price_val == 0 and price_missing_or_invalid
    if not price_unknown and not (MIN_PRICE <= price_val <= MAX_PRICE):
        return storage.VERDICT_REJECTED_PRICE, {"rooms_val": rooms_val, "price_val": price_val}
    if price_unknown and not INCLUDE_PRICE_UNKNOWN:
        return storage.VERDICT_PRICE_UNKNOWN, {"rooms_val": rooms_val, "price_val": price_val}

    arnona = _normalize_bimonthly_fee(data.get("arnona") or "")
    vaad = _normalize_bimonthly_fee(data.get("vaad") or "")
    _warn_if_fee_implausible("Vaad bayit", vaad, 2400)
    _warn_if_fee_implausible("Arnona", arnona, 3000)

    address = _strip_foreign_letters(data.get("address") or "")
    address = _reject_hallucinated_address(address, text)
    address = _format_address_display(address, group_url)
    floor = _parse_floor(data.get("floor") or "")
    is_agent = _detect_agent(text, data.get("is_agent"))
    parking = _classify_parking(data.get("parking") or "")
    entry_date = _normalize_entry_date(data.get("entry_date") or "")

    fields = {
        "rooms_val": rooms_val, "price_val": price_val, "arnona": arnona, "vaad": vaad,
        "address": address, "floor": floor, "is_agent": is_agent, "parking": parking,
        "entry_date": entry_date, "elevator": data.get("elevator"),
        "shelter": data.get("shelter"),
    }
    return storage.VERDICT_ADDED, fields

# Rows are written with value_input_option="USER_ENTERED" (needed so numeric
# columns like price/rooms are stored as real numbers, not text) — that also
# lets Sheets interpret a cell starting with =/+/-/@ as a formula. Row content
# ultimately comes from scraped Facebook text via an LLM (untrusted input), so
# this is an explicit safeguard, not incidental: prefix any such string with a
# literal-text apostrophe before it's sent. (In practice every string field
# here is already narrowed to a fixed set or Hebrew-only text that happens to
# exclude these characters — this guard is deliberate defense-in-depth so that
# stays true even if that normalization changes later.)
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")

def _sheet_safe_cell(value):
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + value
    return value

def _build_row(post_url: str, fb_post_date: str, fields: dict) -> list:
    dist_text, dist_meters, address_confidence, address_warning, distance_source = get_walking_distance(fields["address"])
    fields["distance_text"] = dist_text
    fields["distance_meters"] = dist_meters
    fields["address_confidence"] = address_confidence
    fields["address_warning"] = address_warning
    fields["distance_source"] = distance_source

    row = [
        post_url,
        int(fields["price_val"]) if fields["price_val"] else "",
        fields["rooms_val"],
        dist_text,
        fields["entry_date"],
        fields["floor"],
        map_bool(fields["elevator"]),
        fields["parking"],
        fields["arnona"],
        fields["vaad"],
        map_bool(fields["shelter"]),
        map_bool(fields["is_agent"]),
        fb_post_date,
        fields["address"],
        datetime.now().strftime("%Y-%m-%d %H:%M"),
    ]
    return [_sheet_safe_cell(v) for v in row]


def _analysis_from_fields(fields: dict, post_date: str, reject_reason: str = "", model_used: str = "gemini_or_ollama") -> dict:
    return {
        "price_val": fields.get("price_val"),
        "rooms_val": fields.get("rooms_val"),
        "address": fields.get("address"),
        "address_confidence": fields.get("address_confidence"),
        "distance_text": fields.get("distance_text"),
        "distance_meters": fields.get("distance_meters"),
        "post_date": post_date,
        "reject_reason": reject_reason,
        "model_used": model_used,
    }

def setup_google_sheet():
    """
    Connects to Google Sheets, checks existing data,
    and creates headers if the sheet is empty.
    Returns the sheet object and a set of already seen URLs.
    """
    print("\nConnecting to Google Sheets and reading existing data...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SHEET_ID).sheet1

    try:
        # Check headers via row 1 only — avoids pulling the whole sheet into memory
        headers = sheet.row_values(1)

        if not headers:
            print("Missing column headers in Google Sheet. Adding them to row 1...")
            sheet.insert_row(SHEET_HEADERS, 1)
        elif len(headers) != len(SHEET_HEADERS) or headers[0] != "לינק למודעה":
            print("Outdated column headers in Google Sheet. Updating row 1...")
            if headers and headers[0] == "לינק למודעה":
                sheet.delete_rows(1)
            sheet.insert_row(SHEET_HEADERS, 1)

        # Pull only the URL column (column 1) for dedup — not the whole table
        seen_urls = {url for url in sheet.col_values(1)[1:] if url}

        print(f"    Found {len(seen_urls)} existing apartments in the sheet. Will skip them.")
        return sheet, seen_urls
    except Exception as e:
        print(f"    ERROR: reading Google Sheet: {e}")
        print("    Aborting: cannot dedupe or write results without the sheet.")
        sys.exit(1)


def _append_rows_batch(sheet, rows: list[list]):
    if not rows:
        return
    _with_retries(lambda: sheet.append_rows(rows, value_input_option="USER_ENTERED"))


# ─── Cross-post dedup + sort ────────────────────────────────────────────────
# Posts sometimes get reposted or posted to multiple groups under a different
# URL — not caught by seen_urls. Duplicates are identified by (normalized
# street, rooms, price) and the most recent post is kept.
_CITY_TOKENS_RE = re.compile(r'רמת[\s-]?גן|גבעתיים|תל[\s-]?אביב|ר["״]?ג\b|\bרג\b')
_ADDRESS_PUNCT_RE = re.compile(r'[",./\-–—_]')
_POST_DATE_DDMM_RE = re.compile(r'^(\d{1,2})/(\d{1,2})$')
_HEBREW_RE = re.compile(r'[\u0590-\u05FF]')
_STREET_HINT_RE = re.compile(r'רחוב|רח[\'’׳]|שדרות|שד[\'’׳]|דרך|סמטת|סמטה|כיכר|משעול')
_LANDMARK_HINT_RE = re.compile(r'ליד|בסמוך|קרוב ל|צמוד ל|באזור|בשכונת|שכונת')
# Street-type word/abbreviation carries no distinguishing signal for dedupe —
# "רחוב הרצל" and "רח' הרצל" are the same street. LLM output uses the ASCII
# apostrophe (U+0027), not the Hebrew geresh (׳), so both must be recognized.
_STREET_TYPE_RE = _STREET_HINT_RE

def _normalize_address_key(address: str) -> str:
    if not address or address == "לא צוין":
        return ""
    norm = _CITY_TOKENS_RE.sub('', address)
    norm = _STREET_TYPE_RE.sub('', norm)
    norm = _ADDRESS_PUNCT_RE.sub(' ', norm)
    return re.sub(r'\s+', ' ', norm).strip()

# ─── Address display formatting ─────────────────────────────────────────────
# Reshapes the LLM's raw (verbatim-from-post) address into the sheet's
# preferred display convention: "<street> <number>, <city>". Only fires when
# an explicit city is present in the text — same "never invent" discipline as
# _reject_hallucinated_address: a neighborhood or street mentioned without a
# city isn't guessed at, it's left as-is.
_CITY_DISPLAY_RES = [
    (re.compile(r'(?:ב|ל)?רמת[\s-]?גן|(?:ב|ל)?ר["״]?ג\b|(?:ב|ל)?\bרג\b'), "רמת גן"),
    (re.compile(r'(?:ב|ל)?גבעתיים'), "גבעתיים"),
    (re.compile(r'(?:ב|ל)?תל[\s-]?אביב'), "תל אביב"),
]
_NEIGHBORHOOD_DISPLAY_RE = re.compile(r'^(?:ב)?שכונ(?:ת|ה)\s+(.+)$')
_CORNER_DISPLAY_RE = re.compile(r'^(.+?)\s+(?:על\s+)?פינת\s+(.+)$')
_TRAILING_NUMBER_RE = re.compile(r'^(.+?)\s+(\d+[א-ת]?)$')

def _extract_city_and_core(address: str) -> tuple[str, str] | None:
    """Finds an explicit city token in the address text. Returns (remaining core, canonical city), or None."""
    for city_re, canonical in _CITY_DISPLAY_RES:
        m = city_re.search(address)
        if m:
            core = address[:m.start()] + address[m.end():]
            # Edge-trim only (not a global punctuation substitution like
            # _ADDRESS_PUNCT_RE): a Hebrew acronym street name can carry an
            # internal גרשיים ("הרא"ה", "ל"ה"), and an already-formatted
            # corner keeps its internal " - " separator. Stripping those
            # unconditionally would corrupt real content and break
            # idempotency (re-running this on its own prior output must be
            # a no-op).
            core = re.sub(r'\s+', ' ', core).strip(' ,./-–—_\t')
            return core, canonical
    return None

def _format_core_with_city(core: str, city: str) -> str:
    """Shapes address text (city already removed) plus a resolved city into the display format. Empty string if unusable."""
    if not core or not _HEBREW_RE.search(core):
        return ""

    m = _NEIGHBORHOOD_DISPLAY_RE.match(core)
    if m:
        neighborhood = m.group(1).strip()
        return f"{neighborhood}, {city}" if neighborhood else ""

    m = _CORNER_DISPLAY_RE.match(core)
    if m:
        street_a = _STREET_HINT_RE.sub('', m.group(1)).strip()
        street_b = _STREET_HINT_RE.sub('', m.group(2)).strip()
        return f"{street_a} - {street_b}, {city}" if street_a and street_b else ""

    street_core = _STREET_HINT_RE.sub('', core, count=1).strip()
    if not street_core:
        return ""

    m = _TRAILING_NUMBER_RE.match(street_core)
    if m:
        street, number = m.group(1).strip(), m.group(2).strip()
        return f"{street} {number}, {city}" if street else ""

    return f"{street_core}, {city}"

def _extract_candidate_cities(group_name: str) -> list[str]:
    """Distinct canonical cities mentioned in a FB group's real display name (e.g. 'דירות רמת גן/גבעתיים')."""
    cities = []
    for city_re, canonical in _CITY_DISPLAY_RES:
        if city_re.search(group_name) and canonical not in cities:
            cities.append(canonical)
    return cities

def _gmaps_result_matches_city(result: dict, city: str) -> bool:
    formatted = result.get("formatted_address", "")
    result_city = _gmaps_result_city(result)
    return city in formatted or city == result_city or city in result_city

def _resolve_ambiguous_city(street_core: str, candidate_cities: list[str]) -> str | None:
    """
    Resolves which of a group's candidate cities a street actually belongs to.
    1 candidate -> trusted directly, no Maps call. >=2 -> geocode each to see
    which ones the street exists in; if more than one does, the walking-
    distance-closer one to DESTINATION_ADDRESS wins. Never guesses: returns
    None (caller falls back to the address unchanged) when nothing resolves
    confidently or Maps is unavailable/over quota.
    """
    if not candidate_cities:
        return None
    if len(candidate_cities) == 1:
        return candidate_cities[0]

    if not GMAPS_VALIDATE_ADDRESSES:
        return None

    hits = []  # (city, formatted_address)
    for city in candidate_cities:
        over_cap, _ = _handle_gmaps_cap_if_needed()
        if over_cap:
            break
        query = f"{street_core}, {city}, ישראל"
        try:
            storage.increment_gmaps_usage()
            results = _with_retries(lambda: gmaps_client.geocode(query, language="he", region="il"))
        except Exception as e:
            _safe_print(f"\n    [Google Geocoding API Error]: {_redact_api_key(str(e))}")
            continue
        if not results:
            continue
        best = results[0]
        if _gmaps_has_street_precision(best) and _gmaps_result_matches_city(best, city):
            hits.append((city, best.get("formatted_address", "")))

    if not hits:
        return None
    if len(hits) == 1:
        return hits[0][0]

    best_city, best_meters = None, float("inf")
    for city, formatted in hits:
        over_cap, _ = _handle_gmaps_cap_if_needed()
        if over_cap:
            break
        try:
            storage.increment_gmaps_usage()
            result = _with_retries(lambda: gmaps_client.distance_matrix(
                origins=formatted, destinations=DESTINATION_ADDRESS, mode="walking", language="he", region="il",
            ))
            element = result["rows"][0]["elements"][0]
            if element.get("status") == "OK" and element["distance"]["value"] < best_meters:
                best_meters = element["distance"]["value"]
                best_city = city
        except Exception as e:
            _safe_print(f"\n    [Google Distance Matrix API Error]: {_redact_api_key(str(e))}")
            continue

    return best_city or hits[0][0]

def _format_address_display(address: str, group_url: str | None = None) -> str:
    """
    "רחוב המעלות 12 בגבעתיים" -> "המעלות 12, גבעתיים"
    "רחוב הירדן רמת גן" -> "הירדן, רמת גן" (no number)
    "שכונת מרום נווה ברמת גן" -> "מרום נווה, רמת גן" (neighborhood, not a street)
    "רחוב אלימלך על פינת הרצל ברמת גן" -> "אלימלך - הרצל, רמת גן" (corner)
    When no city is stated at all, falls back to resolving one from the
    scanning group's own candidate cities (see _resolve_ambiguous_city) for a
    plain street address — corner/neighborhood phrasing without a city is out
    of scope (too ambiguous to geocode as a single street query). Falls back
    to the address unchanged whenever nothing resolves confidently — never
    guesses a city.
    """
    if not address:
        return address

    found = _extract_city_and_core(address)
    if found:
        core, city = found
        return _format_core_with_city(core, city) or address

    if group_url and not _NEIGHBORHOOD_DISPLAY_RE.match(address.strip()) and not _CORNER_DISPLAY_RE.search(address):
        candidates = storage.get_group_city_hint(group_url)
        if candidates:
            street_core = _STREET_HINT_RE.sub('', address, count=1).strip()
            m = _TRAILING_NUMBER_RE.match(street_core)
            street_only = m.group(1).strip() if m else street_core
            if street_only and _HEBREW_RE.search(street_only):
                city = _resolve_ambiguous_city(street_only, candidates)
                if city:
                    return _format_core_with_city(street_core, city) or address

    return address

def _infer_post_date(date_str: str, now: datetime | None = None) -> datetime | None:
    """
    Converts displayed DD/MM into a full datetime near 'now'.
    Keeps sheet formatting as DD/MM, but makes filtering/sorting year-safe.
    """
    now = now or datetime.now()
    match = _POST_DATE_DDMM_RE.match((date_str or "").strip())
    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))

    try:
        candidate = datetime(now.year, month, day)
    except ValueError:
        return None

    if candidate > now + timedelta(days=1):
        candidate = candidate.replace(year=now.year - 1)

    return candidate


def _is_recent_post_date(date_str: str) -> bool:
    parsed = _infer_post_date(date_str)
    if parsed is None:
        return True
    return parsed >= datetime.now() - timedelta(days=MAX_POST_AGE_DAYS)


def _post_date_sort_key(date_str: str) -> datetime:
    parsed = _infer_post_date(date_str)
    return parsed or datetime.min


def _classify_address_confidence(address: str) -> tuple[str, str]:
    cleaned = (address or "").strip()
    if not cleaned:
        return "missing", "כתובת חסרה"

    if cleaned in _CITY_ONLY_ADDRESSES:
        return "low", "כתובת ברמת עיר בלבד"

    if not _HEBREW_RE.search(cleaned):
        return "missing", "כתובת לא תקינה"

    if _STREET_HINT_RE.search(cleaned) or re.search(r'\d+', cleaned):
        return "high", ""

    if _LANDMARK_HINT_RE.search(cleaned):
        return "medium", "כתובת לפי שכונה/ציון דרך - לבדיקה"

    if len(_normalize_address_key(cleaned)) < 4:
        return "low", "כתובת קצרה/כללית מדי"

    return "medium", "כתובת ללא אינדיקציה ברורה לרחוב"

def _listing_key(row: list, db_data: dict[str, dict] | None = None) -> tuple:
    """
    Keys a row by address+rooms+price sourced from the bot's own original
    parse in the DB (db_data, keyed by URL) when available, falling back to
    the live sheet cells otherwise. Matching off the DB record means a manual
    edit to a sheet cell (e.g. fixing a price) can't stop a later crosspost
    of the same listing, added under a different URL, from still being
    recognized as a duplicate.
    """
    url = row[0] if len(row) > 0 else ""
    db_row = (db_data or {}).get(url)

    raw_address = db_row["address"] if db_row and db_row.get("address") else (row[13] if len(row) > 13 else "")
    address_key = _normalize_address_key(raw_address)

    if not address_key or len(address_key) < 4:
        return ("url", url)

    raw_rooms = db_row["rooms_val"] if db_row and db_row.get("rooms_val") is not None else (row[2] if len(row) > 2 else "")
    try:
        rooms = f"{float(raw_rooms):.1f}"
    except (ValueError, TypeError):
        rooms = raw_rooms
    raw_price = db_row["price_val"] if db_row and db_row.get("price_val") is not None else (row[1] if len(row) > 1 else "")
    try:
        price = str(int(float(raw_price)))
    except (ValueError, TypeError):
        price = raw_price
    return ("listing", address_key, rooms, price)

def dedupe_and_sort_sheet(sheet) -> tuple[int, int, int]:
    """
    Rewrites the sheet's data range in one pass:
    1. Removes duplicates: identical URL is always a duplicate; same apartment
       by meaningful address + rooms + price keeps the most recent post.
       Missing/weak addresses aren't merged by price+rooms alone, so real
       distinct apartments don't get accidentally deleted.
    2. Drops rows older than MAX_POST_AGE_DAYS (via _is_recent_post_date) — the
       sheet is just a "recent/relevant" view; the local DB (should_skip) is
       what permanently remembers a URL, so removing an old row here can never
       cause it to be rescanned or re-added. Rows with an unparseable/missing
       post date are kept (same "don't delete what we're not sure about" rule).
    Returns (duplicates_removed, stale_removed, kept).
    """
    data = sheet.get_all_values()
    if len(data) <= 1:
        return 0, 0, len(data) - 1 if data else 0
    rows = data[1:]

    best_by_url = {}
    for row in rows:
        url = row[0] if row else ""
        existing = best_by_url.get(url)
        if not url:
            key = f"empty-url-{len(best_by_url)}"
            best_by_url[key] = row
        elif existing is None or _post_date_sort_key(row[12] if len(row) > 12 else "") > _post_date_sort_key(existing[12] if len(existing) > 12 else ""):
            best_by_url[url] = row

    db_data = storage.get_added_listing_data()
    best_by_key = {}
    for row in best_by_url.values():
        key = _listing_key(row, db_data)
        existing = best_by_key.get(key)
        if existing is None or _post_date_sort_key(row[12] if len(row) > 12 else "") > _post_date_sort_key(existing[12] if len(existing) > 12 else ""):
            best_by_key[key] = row

    deduped_rows = list(best_by_key.values())
    duplicates_removed = len(rows) - len(deduped_rows)

    fresh_rows = [r for r in deduped_rows if _is_recent_post_date(r[12] if len(r) > 12 else "")]
    stale_removed = len(deduped_rows) - len(fresh_rows)

    fresh_rows.sort(key=lambda r: _post_date_sort_key(r[12] if len(r) > 12 else ""), reverse=True)

    _rewrite_sheet_data_rows(sheet, len(rows), fresh_rows)
    return duplicates_removed, stale_removed, len(fresh_rows)

def _rewrite_sheet_data_rows(sheet, original_row_count: int, new_rows: list[list]):
    """Shared full-rewrite helper: clears the data range (header kept) and writes new_rows back."""
    last_col = chr(ord('A') + len(SHEET_HEADERS) - 1)
    _with_retries(lambda: sheet.batch_clear([f"A2:{last_col}{original_row_count + 1}"]))
    if new_rows:
        _with_retries(lambda: sheet.update(range_name=f"A2:{last_col}{len(new_rows) + 1}", values=new_rows))

_DEAD_POST_RE = re.compile(r"isn.t available right now", re.IGNORECASE)

def _is_post_removed(page, url: str, headless: bool, label: str) -> bool:
    """
    Navigates to a post URL and checks for Facebook's generic "This content
    isn't available right now" placeholder — shown when a post was deleted,
    or its visibility was changed to exclude this account. Confirms with one
    reload before concluding it's really gone, so a transient load hiccup
    can't wipe a real listing.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    page.wait_for_timeout(2000)
    _handle_checkpoint_if_present(page, url, label, headless)
    if not _is_visible(page.get_by_text(_DEAD_POST_RE)):
        return False

    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception:
        return True
    return _is_visible(page.get_by_text(_DEAD_POST_RE))

def _prune_dead_links(page, sheet, headless: bool, live: bool) -> int:
    """
    Beginning-of-run pass: visits every URL already in the sheet and removes
    rows whose post Facebook now shows as unavailable (deleted, or visibility
    changed) — such a link is a dead lead that would otherwise sit in the
    sheet until MAX_POST_AGE_DAYS ages it out on its own. live=False checks
    and reports what would be removed without writing, same dry-run contract
    as the rest of the pipeline. Local DB rows are untouched — same reasoning
    as stale-row pruning in dedupe_and_sort_sheet: the sheet is just the
    "current" view, should_skip() only ever reads verdict/attempts.
    """
    data = sheet.get_all_values()
    if len(data) <= 1:
        return 0
    rows = data[1:]

    dead_indices = []
    for i, row in enumerate(rows):
        url = row[0] if row else ""
        if not url:
            continue
        if _is_post_removed(page, url, headless, "Dead-link check"):
            dead_indices.append(i)
            _safe_print(f"    Dead link found (post no longer available): {url}")
        time.sleep(random.uniform(2, 5))

    if not dead_indices:
        print("Dead-link check: 0 removed listing(s) found.")
        return 0

    if not live:
        print(f"DRY RUN: would remove {len(dead_indices)} dead listing(s) from the sheet. Pass --live to commit.")
        return len(dead_indices)

    keep_rows = [row for i, row in enumerate(rows) if i not in dead_indices]
    _rewrite_sheet_data_rows(sheet, len(rows), keep_rows)
    print(f"Removed {len(dead_indices)} dead listing(s) from the sheet (post no longer available).")
    return len(dead_indices)

# FB serves the same group post under multiple URL shapes — /posts/<id> and
# /permalink/<id> are interchangeable, and www/m/web hosts all resolve to the
# same post. Without canonicalization each shape looks like a new post to the
# dedupe checks (DB primary key + sheet seen_urls) and costs a duplicate LLM call.
_CANONICAL_GROUP_POST_RE = re.compile(
    r'https://(?:www|m|web)\.facebook\.com/groups/([^/?#]+)/(?:posts|permalink)/(\d+)'
)

def _canonical_post_url(url: str) -> str:
    m = _CANONICAL_GROUP_POST_RE.match(url)
    if m:
        # Trailing slash kept — matches the shape already stored in the DB/sheet
        return f"https://www.facebook.com/groups/{m.group(1)}/posts/{m.group(2)}/"
    return url  # unknown shape: pass through unchanged, never break dedupe on odd URLs

def extract_post_info(article) -> tuple[str, str]:
    try:
        links = article.locator('a[role="link"]').all()
        for link in links:
            href = link.get_attribute("href") or ""
            if any(seg in href for seg in ("/posts/", "/permalink/", "/marketplace/item/")):
                if "comment_id" in href:
                    continue
                clean = href.split("?")[0]
                if clean.startswith("/"):
                    clean = "https://www.facebook.com" + clean
                clean = _canonical_post_url(clean)

                # Extract the post date directly from the Facebook timestamp link
                post_date = ""
                try:
                    link_text = link.inner_text().strip()
                    if link_text:
                        post_date = relative_to_date(link_text)
                except Exception:
                    pass

                return clean, post_date
    except Exception:
        pass
    return "Link not extracted", ""

_ollama_lock = threading.Lock()
_gemini_rate_lock = threading.Lock()
_last_gemini_call = 0.0

def _get_llm_raw_result(prompt: str) -> dict | None:
    """
    Runs a single LLM parsing attempt (Gemini if not exhausted, otherwise
    Ollama). Returns a raw dict, before schema validation — validation happens
    at the analyze_post_with_llm level so it applies identically to both paths.
    """
    global GEMINI_EXHAUSTED, GEMINI_ERROR_COUNT, _last_gemini_call
    if not GEMINI_EXHAUSTED:
        with _gemini_rate_lock:
            now = time.time()
            elapsed = now - _last_gemini_call
            if elapsed < 4.0:
                time.sleep(4.0 - elapsed)
            _last_gemini_call = time.time()
            
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ApartmentData,
                ),
            )
            result = response.parsed.model_dump() if response.parsed else json.loads(response.text)
            with _gemini_lock:
                GEMINI_ERROR_COUNT = 0
            return result
        except Exception as gemini_err:
            error_msg = str(gemini_err)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                _safe_print("\n    Gemini quota exhausted. Switching permanently to Ollama...")
                with _gemini_lock:
                    GEMINI_EXHAUSTED = True
            elif "404" in error_msg or "NOT_FOUND" in error_msg:
                _safe_print(f"\n    WARNING: Gemini model '{GEMINI_MODEL}' not found ({error_msg}).")
                _safe_print("    The model is misconfigured or deprecated — update GEMINI_MODEL in config.py")
                with _gemini_lock:
                    GEMINI_EXHAUSTED = True
            else:
                with _gemini_lock:
                    GEMINI_ERROR_COUNT += 1
                    current_count = GEMINI_ERROR_COUNT
                _safe_print(f"\n    WARNING: Gemini error ({error_msg}). Falling back to Ollama...")
                if current_count >= GEMINI_MAX_CONSECUTIVE_ERRORS:
                    _safe_print(f"\n    {current_count} consecutive Gemini errors. Switching permanently to Ollama...")
                    with _gemini_lock:
                        GEMINI_EXHAUSTED = True
    else:
        _safe_print("[Local Ollama] ")

    try:
        with _ollama_lock:
            ollama_response = ollama.chat(
                model='qwen2.5:7b',
                messages=[{'role': 'user', 'content': prompt}],
                format=ApartmentData.model_json_schema(),  # forces schema-compliant decoding — no manual JSON repair needed anymore
                options={'temperature': 0, 'num_ctx': 4096},
                keep_alive='10m',
            )
        return json.loads(ollama_response['message']['content'])
    except Exception as ollama_err:
        _safe_print(f"\n    ERROR: local Ollama analysis failed: {ollama_err}")
        return None

def analyze_post_with_llm(text: str) -> dict | None:
    """
    Two attempts max: each attempt runs the LLM and then validates the output
    against ApartmentData. A raw call failure (LLM/network error) or a schema
    validation failure both count as one attempt; failure on both -> None
    (verdict parse_failed for the caller).
    """
    prompt = get_apartment_prompt_improved(_clean_post_for_llm(text))
    for attempt in range(2):
        raw = _get_llm_raw_result(prompt)
        if raw is None:
            _safe_print(f"    WARNING: LLM call returned no result (attempt {attempt + 1}/2)")
            continue
        try:
            return ApartmentData.model_validate(raw).model_dump()
        except Exception as validation_err:
            _safe_print(f"    WARNING: LLM output failed schema validation (attempt {attempt + 1}/2): {validation_err}")
    return None

def _with_retries(fn, attempts: int = 3, base_delay: float = 1.0):
    """Runs fn up to `attempts` times on failure, with increasing delay between attempts. Returns/raises on the last attempt."""
    last_err = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
    if last_err:
        raise last_err
    raise RuntimeError("Function failed repeatedly without capturing an exception")

_CITY_ONLY_ADDRESSES = {"רמת גן", "רמת-גן", "גבעתיים", "תל אביב", 'ר"ג', "ר״ג"}

class GmapsQuotaHalted(Exception):
    """Raised when GMAPS_ON_CAP == 'halt' and the monthly quota has been reached — stops the entire run."""

_gmaps_cap_lock = threading.Lock()
_gmaps_cap_notice_printed = False

def _handle_gmaps_cap_if_needed() -> tuple[bool, str]:
    global _gmaps_cap_notice_printed
    if storage.get_gmaps_usage() < GMAPS_MONTHLY_CAP:
        return False, ""

    with _gmaps_cap_lock:
        already_notified = _gmaps_cap_notice_printed
        _gmaps_cap_notice_printed = True
    if not already_notified:
        _safe_print(f"\n    WARNING: Google Maps monthly cap reached ({GMAPS_MONTHLY_CAP} calls). "
                    f"GMAPS_ON_CAP='{GMAPS_ON_CAP}' in config.py.")
    if GMAPS_ON_CAP == "halt":
        raise GmapsQuotaHalted()
    return True, "מכסה חודשית הסתיימה"


def _gmaps_component_long_names(result: dict, component_type: str) -> list[str]:
    names = []
    for component in result.get("address_components", []):
        if component_type in component.get("types", []):
            names.append(component.get("long_name", ""))
    return names


def _gmaps_result_city(result: dict) -> str:
    for component_type in ("locality", "administrative_area_level_2", "administrative_area_level_1"):
        names = _gmaps_component_long_names(result, component_type)
        if names:
            return names[0]
    return ""


def _gmaps_has_street_precision(result: dict) -> bool:
    types = set(result.get("types", []))
    if "street_address" in types or "premise" in types:
        return True
    component_types = {
        t
        for component in result.get("address_components", [])
        for t in component.get("types", [])
    }
    return "route" in component_types


def _gmaps_city_allowed(result: dict) -> bool:
    formatted = result.get("formatted_address", "")
    city = _gmaps_result_city(result)
    return any(target in formatted or target in city for target in GMAPS_TARGET_CITIES)


def _validate_address_with_geocoding(address: str, confidence: str) -> tuple[str, str, str, str, str]:
    """
    Returns canonical_address, city, updated_confidence, warning, geocode_status.
    """
    if not GMAPS_VALIDATE_ADDRESSES:
        return address, "", confidence, "", "disabled"

    if confidence in {"missing", "low"}:
        return "", "", confidence, "כתובת חלשה - לא נשלחה לגיאוקודינג", "skipped_low_confidence"

    over_cap, placeholder = _handle_gmaps_cap_if_needed()
    if over_cap:
        return address, "", confidence, placeholder, "quota"

    query = address
    if not any(city in query for city in ["רמת גן", "גבעתיים", "תל אביב", "רמת-גן", "ר\"ג"]):
        query = f"{query}, רמת גן, גבעתיים, ישראל"

    try:
        storage.increment_gmaps_usage()
        results = _with_retries(lambda: gmaps_client.geocode(query, language="he", region="il"))
    except Exception as e:
        _safe_print(f"\n    [Google Geocoding API Error]: {_redact_api_key(str(e))}")
        return address, "", confidence, "שגיאת אימות כתובת", "geocode_error"

    if not results:
        return "", "", "low", "Google לא מצא את הכתובת", "not_found"

    best = results[0]
    if not _gmaps_city_allowed(best):
        return "", _gmaps_result_city(best), "low", "הכתובת לא אומתה בעיר יעד", "wrong_city"

    if not _gmaps_has_street_precision(best):
        return "", _gmaps_result_city(best), "low", "Google החזיר תוצאה כללית בלבד", "not_street_precision"

    return best.get("formatted_address", address), _gmaps_result_city(best), "high", "", "ok"


def get_walking_distance(address: str):
    """
    Returns distance_text, distance_meters, confidence, warning, source.
    """
    confidence, warning = _classify_address_confidence(address)

    if not address or len(address) < 3:
        return "", 999999, "missing", "כתובת חסרה", "skipped"

    cached = storage.get_address_cache(address)
    if cached:
        return (
            cached.get("distance_text") or "",
            cached.get("distance_meters") or 999999,
            cached.get("confidence") or confidence,
            cached.get("warning") or "",
            "cache",
        )

    if address.strip() in _CITY_ONLY_ADDRESSES:
        storage.save_address_cache(address, "", "", "low", "כתובת ברמת עיר בלבד", "", 999999, "skipped", "city_only")
        return "", 999999, "low", "כתובת ברמת עיר בלבד", "skipped"

    canonical_address, city, confidence, geocode_warning, geocode_status = _validate_address_with_geocoding(address, confidence)
    warning = geocode_warning or warning

    if GMAPS_DISTANCE_ONLY_CONFIDENT_ADDRESS and confidence not in {"high", "medium"}:
        storage.save_address_cache(address, canonical_address, city, confidence, warning, "", 999999, "skipped", geocode_status)
        return "", 999999, confidence, warning, "skipped"

    over_cap, placeholder = _handle_gmaps_cap_if_needed()
    if over_cap:
        storage.save_address_cache(address, canonical_address, city, confidence, warning, placeholder, 999999, "quota", geocode_status)
        return placeholder, 999999, confidence, warning, "quota"

    # Safety-net addition for Google Maps: anchoring the search area
    origin = canonical_address or address
    if not any(city_name in origin for city_name in ["רמת גן", "גבעתיים", "תל אביב", "רמת-גן", "ר\"ג"]):
        origin = f"{origin}, רמת גן, גבעתיים, ישראל"

    try:
        storage.increment_gmaps_usage()  # counted before the call — origins×destinations is always 1×1 here
        result = _with_retries(lambda: gmaps_client.distance_matrix(
            origins=origin,
            destinations=DESTINATION_ADDRESS,
            mode="walking",
            language="he",
            region="il",
        ))
        element = result["rows"][0]["elements"][0]
        if element["status"] == "OK":
            dist_meters = element["distance"]["value"]
            dist_text = f"{dist_meters / 1000:.1f}"
            storage.save_address_cache(address, canonical_address, city, confidence, warning, dist_text, dist_meters, "google_maps", geocode_status)
            return dist_text, dist_meters, confidence, warning, "google_maps"

        storage.save_address_cache(address, canonical_address, city, confidence, "Distance Matrix לא החזיר מסלול תקין", "", 999999, "google_maps_failed", geocode_status)
        return "", float('inf'), confidence, "Distance Matrix לא החזיר מסלול תקין", "google_maps_failed"
    except Exception as e:
        _safe_print(f"\n    [Google Maps API Error]: {_redact_api_key(str(e))}")
        storage.save_address_cache(address, canonical_address, city, confidence, "שגיאת Distance Matrix", "", 999999, "google_maps_error", geocode_status)
        return "", float('inf'), confidence, "שגיאת Distance Matrix", "google_maps_error"

def _dismiss_popups(page):
    for selector in [
        'div[aria-label="Close"]',
        'div[aria-label="סגירה"]',
        'button:has-text("Not Now")',
        'button:has-text("לא עכשיו")',
    ]:
        try:
            page.locator(selector).first.click(timeout=2000)
        except Exception:
            pass

# ─── Core Scraper ────────────────────────────────────────────────────────

def _handle_checkpoint_if_present(page, target_url: str, group_label: str, headless: bool):
    """
    If Facebook asks for login/2FA/CAPTCHA — stops every group, not just the
    current one. In headless mode there's no visible window to solve it in
    manually, so a screenshot is saved, every other group is flagged to give up
    instead of each one hitting the same wall itself, and an exception is
    raised that signals _scan_group to stop this group cleanly.
    """
    global _headless_checkpoint_hit
    while (_is_visible(page.locator('input[name="email"]')) or
           _is_visible(page.locator('input[name="pass"]')) or
           "checkpoint" in page.url or
           _is_visible(page.locator('iframe[title*="recaptcha"]')) or
           _is_visible(page.get_by_text("I'm not a robot"))):
        if headless:
            safe_label = group_label.replace(' ', '_').replace('/', '-')
            screenshot_path = f"checkpoint_{safe_label}.png"
            try:
                page.screenshot(path=screenshot_path)
            except Exception:
                pass
            with _checkpoint_lock:
                already_flagged = _headless_checkpoint_hit
                _headless_checkpoint_hit = True
            if not already_flagged:
                _safe_print(f"\n[{group_label}] Facebook is asking for a password, 2FA, or CAPTCHA security check. "
                            f"Cannot solve this in --headless mode. Screenshot saved to {screenshot_path}. "
                            f"Rerun without --headless to resolve it manually.")
            raise HeadlessCheckpointAbort()

        is_leader = False
        with _checkpoint_lock:
            if _resume_event.is_set():
                _resume_event.clear()
                is_leader = True
        if is_leader:
            try:
                page.bring_to_front()
            except Exception:
                pass
            _safe_print(f"\n[{group_label}] Facebook is asking for a password, 2FA, or CAPTCHA security check. Pausing ALL groups.")
            input("  -> Please complete it in the browser tab that was brought to front, then press ENTER here to resume all groups... ")
            _resume_event.set()
        else:
            _resume_event.wait()
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception:
            pass

def _scan_group_page(page, target_url: str, group_label: str, sheet, seen_urls, headless: bool, live: bool = False) -> dict:
    """
    The actual scan logic for a single group, run on a page that already
    exists. Shared between parallel mode (_scan_group, which creates a
    dedicated browser/context per thread) and sequential mode (run_scraper
    when MAX_CONCURRENT_GROUPS == 1, which runs every group in sequence on the
    same page inside the original persistent context, to preserve the
    profile's real fingerprint).

    live=False (default, dry-run): classifies posts normally (LLM calls,
    Maps/geocoding calls, and their quota tracking all still happen — the
    read-side of the pipeline is identical either way) but a would-be match
    is never written to the sheet and never recorded as VERDICT_ADDED, so a
    dry run can never block a real future match via should_skip's cache.
    Non-match verdicts (prefiltered/rejected/parse_failed) are still recorded
    regardless of live, since that's just avoiding repeat LLM cost on posts
    already known not to match, not a "commit" of a result.

    Returns a dict summarizing the run: added, checkpoint_hit (True when the
    group was skipped/stopped due to a checkpoint that can't be solved in
    headless mode), posts_seen, prefiltered, llm_parsed.
    """
    stats = {"added": 0, "checkpoint_hit": False, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}
    pending_rows = []
    pending_seen_urls = set()
    pending_records = []
    try:
        _safe_print(f"\n{'='*50}\nScanning {group_label}\n{target_url}\n{'='*50}")

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            _safe_print(f"    WARNING: [{group_label}] Navigation interrupted by Facebook. Checking for security checkpoints...")

        page.wait_for_timeout(3000)
        _handle_checkpoint_if_present(page, target_url, group_label, headless)
        _dismiss_popups(page)

        # Captures the group's real display name so a no-city address can later be
        # resolved to a candidate city (see _resolve_ambiguous_city) — page.title()
        # is far more stable across FB DOM changes than a CSS selector, and costs
        # nothing extra since the page is already loaded.
        try:
            group_title = page.title()
            if group_title:
                storage.save_group_city_hint(target_url, group_title, _extract_candidate_cities(group_title))
        except Exception:
            pass

        _safe_print(f"[{group_label}] Scrolling ({SCROLL_COUNT} times)...")
        for _ in range(SCROLL_COUNT):
            # Jitter the scroll distance too, not just the delay — a perfectly fixed pace and size reads as more bot-like
            # Occasional cursor drift: a human's mouse doesn't sit frozen while reading a feed
            if random.random() < 0.25:
                try:
                    page.mouse.move(random.randint(200, 1100), random.randint(200, 800), steps=random.randint(5, 15))
                except Exception:
                    pass
            page.mouse.wheel(0, random.randint(3000, 5000))
            jittered_delay = max(500, SCROLL_DELAY_MS + random.randint(-400, 400))
            page.wait_for_timeout(jittered_delay)
        _safe_print(f"[{group_label}] Done scrolling.")

        # Click "See more" to reveal the full text of long posts
        for text_pattern in ["See more", "קרא עוד", "ראה עוד"]:
            for el in page.locator(f'div[role="button"]:has-text("{text_pattern}"), span:has-text("{text_pattern}")').all():
                try:
                    if el.is_visible():
                        el.click(timeout=1000)
                        page.wait_for_timeout(300)
                except Exception:
                    pass
        page.wait_for_timeout(1500)

        # Dynamic Selectors fallback loop
        selectors = [
            'div[role="article"]',
            'div[aria-posinset]',
            'div.x1yztbdb',
            'div[data-ad-preview="message"]',
            'div[data-pagelet^="GroupFeed"] > div > div',
            'div[role="feed"] > div > div',
            # Last resort: FB post containers carry aria-labelledby (author) +
            # aria-describedby (body) even when role="article" is absent
            'div[aria-labelledby][aria-describedby]'
        ]

        raw_articles = []
        for sel in selectors:
            elements = page.locator(sel).all()
            if len(elements) > 0:
                raw_articles = elements
                break

        # Process articles
        articles_data = []
        for article in raw_articles:
            try:
                text = article.inner_text().strip()
                if len(text) > 20:
                    articles_data.append({"element": article, "text": text})
            except Exception:
                continue

        _safe_print(f"[{group_label}] Found {len(articles_data)} real posts.")
        stats["posts_seen"] = len(articles_data)

        if len(articles_data) == 0:
            _safe_print(f"    [{group_label}] No posts detected. Saving debug screenshot...")
            try:
                page.screenshot(path=f"debug_fb_{group_label.replace(' ', '_').replace('/', '-')}.png")
            except Exception as e:
                _safe_print(f"    WARNING: [{group_label}] debug screenshot failed: {e}")
            return stats

        valid_posts = []
        for item in articles_data:
            article = item["element"]
            text = item["text"]

            # Per-article "See more" — JS click bypasses visibility/off-screen issues
            try:
                article.scroll_into_view_if_needed(timeout=500)
                clicked = article.evaluate(
                    """el => {
                        const patterns = ['See more', 'קרא עוד', 'ראה עוד'];
                        for (const btn of el.querySelectorAll('div[role="button"], span, a')) {
                            if (patterns.includes(btn.textContent.trim())) { btn.click(); return true; }
                        }
                        return false;
                    }"""
                )
                if clicked:
                    page.wait_for_timeout(400)
                    text = article.inner_text().strip()
            except Exception:
                pass

            text = BIDI_RE.sub('', text)
            text = _strip_comment_section(text)

            post_url, fb_post_date = extract_post_info(article)
            if post_url == "Link not extracted":
                continue  # skip comments or elements that aren't a real post

            if storage.should_skip(post_url):
                _safe_print(f"    [{group_label}] Pre-filtered: Already processed (cached verdict in local DB).")
                continue

            with _sheet_lock:
                already_seen = post_url in seen_urls
            if already_seen:
                _safe_print(f"    [{group_label}] Pre-filtered: Post already exists in Google Sheets (Duplicate).")
                continue

            if fb_post_date and not _is_recent_post_date(fb_post_date):
                _safe_print(f"    [{group_label}] Pre-filtered: Post date {fb_post_date} is older than {MAX_POST_AGE_DAYS} days.")
                storage.record_post(
                    post_url,
                    target_url,
                    text,
                    storage.VERDICT_PREFILTERED,
                    analysis={
                        "post_date": fb_post_date,
                        "reject_reason": f"older_than_{MAX_POST_AGE_DAYS}_days",
                    },
                )
                stats["prefiltered"] += 1
                continue

            excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text]
            if excluded_found:
                _safe_print(f"    [{group_label}] Pre-filtered: Contains excluded location '{excluded_found[0]}'.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: roommate/shared-room posts, unless explicitly also couple-friendly ---
            roommate_match = re.search(ROOMMATE_KEYWORDS, text)
            if roommate_match and not _ROOMMATE_COUPLE_EXCEPTION_RE.search(text):
                _safe_print(f"    [{group_label}] Pre-filtered: Contains negative keyword '{roommate_match.group(0)}'.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: Remove other obvious non-relevant posts (Sublets, Studio, Commercial, Seekers) ---
            neg_match = re.search(NEGATIVE_KEYWORDS, text)
            if neg_match:
                _safe_print(f"    [{group_label}] Pre-filtered: Contains negative keyword '{neg_match.group(0)}'.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: Identify Sales instead of Rentals ---
            # Many agent posts omit the word "for sale" and just write "Price:
            # 3,395,000 ₪" — a 7+ digit price (or "X million") is an
            # unambiguous sale signal even without the word itself.
            # The separator requirement ([.,]\d{3}) blocks a false match on a phone number (050-1234567).
            sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
            if sale_price_match:
                _safe_print(f"    [{group_label}] Pre-filtered: Apartment for sale (price {sale_price_match.group(0).strip()}).")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: Check for room counts explicitly before heavy LLM processing ---
            if not re.search(ROOMS_PRE_FILTER_REGEX, text):
                clean_snip = text[:100].replace('\n', ' ')
                actual_rooms_match = re.search(r'([1-9](?:\.5)?)\s*חד', text)
                if actual_rooms_match:
                    found_val = actual_rooms_match.group(1)
                    _safe_print(f"    [{group_label}] Pre-filtered: Post is for {found_val} rooms (not matching target {MIN_ROOMS}-{MAX_ROOMS}).\n      URL: {post_url}\n      Text: {clean_snip}...")
                else:
                    _safe_print(f"    [{group_label}] Pre-filtered: No mention of matching room count.\n      URL: {post_url}\n      Text: {clean_snip}...")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            valid_posts.append({"url": post_url, "text": text, "post_date": fb_post_date})

        _safe_print(f"[{group_label}] Found {len(valid_posts)} valid posts for LLM parsing.")

        added = 0
        pending_records = []
        pending_rows = []
        pending_seen_urls = set()
        local_lock = threading.Lock()

        def process_post(post):
            post_url = post["url"]
            text = post["text"]
            fb_post_date = post["post_date"]

            _safe_print(f"[{group_label}] Analyzing post (URL: {post_url})...")
            
            try:
                data = analyze_post_with_llm(text)
            except (GmapsQuotaHalted, HeadlessCheckpointAbort):
                raise
                
            with local_lock:
                stats["llm_parsed"] += 1
            if not data:
                _safe_print(f"    [{group_label}] Skipped: LLM failed to parse or returned no data.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PARSE_FAILED,
                                     analysis=_analysis_from_fields({}, fb_post_date, storage.VERDICT_PARSE_FAILED))
                return

            verdict, fields = _evaluate_post_data(data, text, target_url)
            if verdict == storage.VERDICT_REJECTED_ROOMS:
                _safe_print(f"    [{group_label}] Skipped: Room count is not suitable ({fields['rooms_val']}).")
                storage.record_post(post_url, target_url, text, verdict, data, analysis=_analysis_from_fields(fields, fb_post_date, verdict))
                return
            if verdict == storage.VERDICT_REJECTED_PRICE:
                _safe_print(f"    [{group_label}] Skipped: Price is not suitable ({int(fields['price_val']):,} ₪).")
                storage.record_post(post_url, target_url, text, verdict, data, analysis=_analysis_from_fields(fields, fb_post_date, verdict))
                return
            if verdict == storage.VERDICT_PRICE_UNKNOWN:
                _safe_print(f"    [{group_label}] Skipped: no price stated in post (contact seller directly).")
                storage.record_post(post_url, target_url, text, verdict, data, analysis=_analysis_from_fields(fields, fb_post_date, verdict))
                return

            new_row = _build_row(post_url, fb_post_date, fields)

            dist_meters = fields.get("distance_meters")
            # Only reject on distance if a real distance was actually computed
            # against Google Maps — dist_meters is a placeholder (999999/inf)
            # for an uncertain address/quota/error, not a real distance, and
            # shouldn't disqualify a post as if it were far away (see
            # CLAUDE.md, verified 2026-07-18).
            if fields.get("distance_source") == "google_maps" and dist_meters is not None and (dist_meters / 1000.0) > MAX_WALKING_DISTANCE_KM:
                _safe_print(f"    [{group_label}] Skipped: Distance too far ({fields.get('distance_text')} > {MAX_WALKING_DISTANCE_KM}km).")
                storage.record_post(post_url, target_url, text, storage.VERDICT_REJECTED_DISTANCE, data, analysis=_analysis_from_fields(fields, fb_post_date, storage.VERDICT_REJECTED_DISTANCE))
                return

            with _sheet_lock:
                seen_now = post_url in seen_urls or post_url in pending_seen_urls
                if not seen_now:
                    pending_rows.append(new_row)
                    pending_seen_urls.add(post_url)
                    pending_records.append((post_url, target_url, text, data, _analysis_from_fields(fields, fb_post_date)))
                    with local_lock:
                        stats["added"] += 1
                    price_display = f"{int(fields['price_val']):,} ₪" if fields['price_val'] else "מחיר לא צוין"
                    prefix = "SUCCESS" if live else "DRY RUN"
                    verb = "queued" if live else "would queue (pass --live to commit)"
                    _safe_print(f"    {prefix}: [{group_label}] Apartment {verb}: {fields['rooms_val']} rooms | {price_display} | {new_row[3]} | Address: {fields['address']}")
                else:
                    _safe_print(f"    [{group_label}] Skipped before queueing: duplicate URL.")

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_post, post) for post in valid_posts]
            for future in as_completed(futures):
                future.result()

        if pending_rows and live:
            with _sheet_lock:
                try:
                    _append_rows_batch(sheet, pending_rows)
                    seen_urls.update(pending_seen_urls)
                    for record_url, record_group_url, record_text, record_data, record_analysis in pending_records:
                        storage.record_post(record_url, record_group_url, record_text, storage.VERDICT_ADDED, record_data, analysis=record_analysis)
                    _safe_print(f"    [{group_label}] Batch wrote {len(pending_rows)} row(s) to Google Sheets.")
                except Exception as e:
                    _safe_print(f"    ERROR: [{group_label}] batch writing to sheet: {e}")
                    stats["added"] -= len(pending_rows)
        elif pending_rows:
            _safe_print(f"    [{group_label}] DRY RUN: {len(pending_rows)} row(s) would be written to Google Sheets (skipped — pass --live to commit).")
    except GmapsQuotaHalted:
        _safe_print(f"    [{group_label}] Stopping: Google Maps monthly cap reached (GMAPS_ON_CAP='halt').")
    except HeadlessCheckpointAbort:
        stats["checkpoint_hit"] = True
    return stats

def _scan_group(target_url: str, group_label: str, sheet, seen_urls, storage_state_path: str, headless: bool,
                user_agent: str = "", browser_locale: str = "", live: bool = False) -> dict:
    """
    Parallel mode: each thread runs its own Playwright instance (the sync API
    isn't thread-safe when sharing one browser/context between threads) — the
    login is shared via a storage_state exported once from the main profile,
    not by sharing the context object. The actual per-group scan logic is
    shared with sequential mode via _scan_group_page.
    """
    # Random opening-time spread between tabs — looks less bot-like than opening them all simultaneously
    time.sleep(random.uniform(0.5, 3.0))

    if headless:
        with _checkpoint_lock:
            already_hit = _headless_checkpoint_hit
        if already_hit:
            _safe_print(f"    [{group_label}] Skipping: a security checkpoint was already hit in another group (headless mode). Rerun without --headless.")
            return {"added": 0, "checkpoint_hit": True, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=_CHROME_LAUNCH_ARGS
        )
        # Match the real profile's fingerprint: a worker with Playwright's default
        # UA/locale next to the logged-in profile's real ones = same account on two
        # different "devices" simultaneously, a stronger bot signal than either alone.
        context_kwargs = {"storage_state": storage_state_path, "viewport": {"width": 1366, "height": 1600}}
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        if browser_locale:
            context_kwargs["locale"] = browser_locale
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        _apply_stealth(page)
        try:
            return _scan_group_page(page, target_url, group_label, sheet, seen_urls, headless, live)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

def _print_gmaps_quota_status():
    usage = storage.get_gmaps_usage()
    remaining = max(GMAPS_MONTHLY_CAP - usage, 0)
    print(f"Google Maps quota this month: {usage}/{GMAPS_MONTHLY_CAP} used, {remaining} remaining.")
    if GMAPS_MONTHLY_CAP > 0 and usage >= GMAPS_MONTHLY_CAP * 0.8:
        print(f"WARNING: Google Maps usage is at {usage}/{GMAPS_MONTHLY_CAP} ({usage / GMAPS_MONTHLY_CAP:.0%}) of the monthly cap.")

def run_scraper(headless: bool = False, live: bool = False):
    if not live:
        print("DRY RUN: classifying and printing only — no sheet writes, no dedupe/prune. Pass --live to commit.")
    sheet, seen_urls = setup_google_sheet()
    _print_gmaps_quota_status()

    profile_dir = os.path.join(os.getcwd(), "chrome_profile")
    storage_state_path = os.path.join(profile_dir, "_session_state.json")

    shuffled_urls = random.sample(TARGET_URLS, len(TARGET_URLS))
    total = len(shuffled_urls)
    groups_scanned = 0
    total_added = 0
    total_posts_seen = 0
    total_prefiltered = 0
    total_llm_parsed = 0
    checkpoint_skipped = 0
    sequential_mode = MAX_CONCURRENT_GROUPS == 1

    # --- Login phase: single persistent-context tab, sequential ---
    with sync_playwright() as p:
        persistent_kwargs = {}
        if headless:
            # Headless Chromium's UA says "HeadlessChrome" in both the HTTP header and
            # JS — the most obvious headless tell, and a UA can only be set at context
            # creation. Probe a throwaway browser for the real platform-correct UA and
            # strip the "Headless" marker.
            try:
                probe = p.chromium.launch(headless=True)
                probe_page = probe.new_page()
                probe_ua = probe_page.evaluate("navigator.userAgent") or ""
                probe.close()
                if "HeadlessChrome" in probe_ua:
                    persistent_kwargs["user_agent"] = probe_ua.replace("HeadlessChrome", "Chrome")
            except Exception:
                pass
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            viewport={"width": 1366, "height": 1600},
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=_CHROME_LAUNCH_ARGS,
            **persistent_kwargs
        )
        page = context.pages[0] if context.pages else context.new_page()
        _apply_stealth(page)

        print("Opening Facebook...")
        page.goto("https://www.facebook.com", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if headless:
            if _is_visible(page.locator('input[name="email"]')):
                print("ERROR: Cannot login manually in headless mode. Please run without --headless first.")
                context.close()
                sys.exit(1)
        else:
            if _is_visible(page.locator('input[name="email"]')):
                print("\nPlease log in to Facebook manually in the browser window that just opened.\n")

                for attempt in range(LOGIN_MAX_ATTEMPTS):
                    input("  -> Press ENTER here in the terminal ONLY AFTER you have fully logged in and see your feed... ")
                    page.wait_for_timeout(2000)
                    if not _is_visible(page.locator('input[name="email"]')):
                        break
                    print("ERROR: Facebook login form is still visible. Please complete login first.")
                else:
                    print("ERROR: Login not completed after several attempts. Exiting.")
                    context.close()
                    sys.exit(1)
            else:
                print("Already logged into Facebook. Skipping manual login.")

        if PRUNE_DEAD_LINKS_ENABLED:
            _prune_dead_links(page, sheet, headless, live)

        if sequential_mode:
            # Sequential mode: no storage_state at all — scans every group in
            # sequence on the same page inside the original context, to
            # preserve the profile's real fingerprint
            print("Sequential mode (MAX_CONCURRENT_GROUPS=1): scanning all groups one at a time in the original "
                  "browser profile — safest against checkpoints, slowest. No session-state file is written.")
            for i, url in enumerate(shuffled_urls, 1):
                if i > 1:
                    time.sleep(random.uniform(5, 15))
                try:
                    stats = _scan_group_page(page, url, f"Group {i}/{total}", sheet, seen_urls, headless, live)
                except Exception as e:
                    _safe_print(f"\nERROR: [Group {i}/{total}] crashed: {e}")
                    continue
                groups_scanned += 1
                total_added += stats["added"]
                total_posts_seen += stats["posts_seen"]
                total_prefiltered += stats["prefiltered"]
                total_llm_parsed += stats["llm_parsed"]
                if stats["checkpoint_hit"]:
                    remaining = total - i
                    checkpoint_skipped += 1 + remaining
                    _safe_print(f"Stopping sequential scan: {remaining} remaining group(s) also skipped due to the checkpoint.")
                    break
            context.close()
        else:
            # Capture the logged-in profile's fingerprint so parallel workers can
            # present the same one (see _scan_group). "HeadlessChrome" is stripped
            # so a headless run's workers still claim the normal Chrome UA.
            real_user_agent = ""
            real_locale = ""
            try:
                real_user_agent = (page.evaluate("navigator.userAgent") or "").replace("HeadlessChrome", "Chrome")
                real_locale = page.evaluate("navigator.language") or ""
            except Exception:
                pass
            # Exports cookies/session to a file so independent threads can use
            # them — one context/browser can't be shared between threads (Playwright's sync API isn't thread-safe)
            context.storage_state(path=storage_state_path)
            context.close()

    if not sequential_mode:
        print(f"Continuing to scan groups ({MAX_CONCURRENT_GROUPS} in parallel — this raises checkpoint/ban risk "
              f"vs. sequential mode; set MAX_CONCURRENT_GROUPS=1 in config.py if you start seeing checkpoints)...")
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GROUPS) as executor:
            futures = {
                executor.submit(_scan_group, url, f"Group {idx}/{total}", sheet, seen_urls, storage_state_path, headless,
                                real_user_agent, real_locale, live): idx
                for idx, url in enumerate(shuffled_urls, 1)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    stats = future.result()
                    groups_scanned += 1
                    total_added += stats["added"]
                    total_posts_seen += stats["posts_seen"]
                    total_prefiltered += stats["prefiltered"]
                    total_llm_parsed += stats["llm_parsed"]
                    if stats["checkpoint_hit"]:
                        checkpoint_skipped += 1
                except Exception as e:
                    _safe_print(f"\nERROR: [Group {idx}/{total}] crashed: {e}")

    gmaps_calls_this_month = storage.get_gmaps_usage()
    print(f"\nScraping finished successfully. {total_added} apartments added.")
    print(f"Summary: {groups_scanned}/{total} groups scanned, {total_posts_seen} posts seen, "
          f"{total_prefiltered} pre-filtered, {total_llm_parsed} sent to the LLM, {total_added} matches added, "
          f"{gmaps_calls_this_month} Maps calls used this month, {checkpoint_skipped} checkpoint(s) hit.")
    if checkpoint_skipped:
        print(f"{checkpoint_skipped} group(s) skipped due to a security checkpoint — rerun headful.")

    if live:
        print("Deduplicating cross-posted listings, dropping stale rows, and sorting by post date...")
        duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(sheet)
        print(f"Removed {duplicates_removed} duplicate repost(s) and {stale_removed} stale row(s) (older than {MAX_POST_AGE_DAYS} days). "
              f"Sheet now has {kept} listings, sorted by post date (newest first).")

        pruned = storage.prune_old_posts(MAX_POST_AGE_DAYS)
        if pruned:
            print(f"Lightened {pruned} local DB row(s) older than {MAX_POST_AGE_DAYS} days (verdict kept, so they won't be rescanned).")
    else:
        print("DRY RUN: skipping sheet dedupe/sort and DB pruning (pass --live to commit).")

    return {
        "groups_scanned": groups_scanned,
        "total": total,
        "checkpoint_skipped": checkpoint_skipped,
        "total_added": total_added,
    }

def reparse_rejected_posts():
    """
    Re-runs the LLM + filters against raw text already stored in SQLite for posts
    previously verdict-ed rejected_price / rejected_rooms / rejected_distance /
    price_unknown / parse_failed — the prompt-iteration workflow. Opens no browser;
    still writes real matches to the sheet via the normal (non-Playwright) Sheets/Maps
    API clients.
    """
    sheet, seen_urls = setup_google_sheet()
    candidates = storage.get_reparse_candidates()
    print(f"\nRe-parsing {len(candidates)} rejected/failed post(s) from local DB (no browser)...")

    added = 0
    for post in candidates:
        url = post["url"]
        text = post["raw_text"]
        group_url = post["group_url"]
        if not text or url in seen_urls:
            continue

        data = analyze_post_with_llm(text)
        if not data:
            storage.record_post(url, group_url, text, storage.VERDICT_PARSE_FAILED,
                                 analysis=_analysis_from_fields({}, post.get("post_date") or "", storage.VERDICT_PARSE_FAILED))
            print(f"    Still failing to parse: {url}")
            continue

        verdict, fields = _evaluate_post_data(data, text, group_url)
        if verdict != storage.VERDICT_ADDED:
            storage.record_post(url, group_url, text, verdict, data, analysis=_analysis_from_fields(fields, post.get("post_date") or "", verdict))
            print(f"    Still {verdict}: {url}")
            continue

        post_date = post.get("post_date") or ""
        new_row = _build_row(url, post_date, fields)
        dist_meters = fields.get("distance_meters")
        if fields.get("distance_source") == "google_maps" and dist_meters is not None and (dist_meters / 1000.0) > MAX_WALKING_DISTANCE_KM:
            storage.record_post(url, group_url, text, storage.VERDICT_REJECTED_DISTANCE, data, analysis=_analysis_from_fields(fields, post_date, storage.VERDICT_REJECTED_DISTANCE))
            print(f"    Still rejected_distance: {url}")
            continue
        try:
            _with_retries(lambda: sheet.append_row(new_row, value_input_option="USER_ENTERED"))
            seen_urls.add(url)
            storage.record_post(url, group_url, text, storage.VERDICT_ADDED, data, analysis=_analysis_from_fields(fields, post_date))
            added += 1
            price_display = f"{int(fields['price_val']):,} ₪" if fields['price_val'] else "מחיר לא צוין"
            print(f"    SUCCESS: Apartment added: {fields['rooms_val']} rooms | {price_display} | Address: {fields['address']}")
        except Exception as e:
            print(f"    ERROR: writing to sheet: {e}")

    if added:
        duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(sheet)
        print(f"Removed {duplicates_removed} duplicate repost(s) and {stale_removed} stale row(s). Sheet now has {kept} listings, sorted by post date (newest first).")
    print(f"\nDone. {added} new apartment(s) added from reparse.")

def _replay_text_prefilters(text: str) -> tuple[str, dict] | None:
    """
    Content-based filter checks (not URL/date/live cache) for --replay — mirrors
    _scan_group_page (where the conditions also live for the live group_label
    prints), but here without a browser. If you change NEGATIVE_KEYWORDS/
    EXCLUDED_LOCATIONS/etc., update both places.
    Returns (verdict, reject_reason) if it should stop, otherwise None to continue to the LLM.
    """
    excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text]
    if excluded_found:
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"excluded_location:{excluded_found[0]}"}

    roommate_match = re.search(ROOMMATE_KEYWORDS, text)
    if roommate_match and not _ROOMMATE_COUPLE_EXCEPTION_RE.search(text):
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"roommate_keyword:{roommate_match.group(0)}"}

    neg_match = re.search(NEGATIVE_KEYWORDS, text)
    if neg_match:
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"negative_keyword:{neg_match.group(0)}"}

    sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
    if sale_price_match:
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"for_sale:{sale_price_match.group(0).strip()}"}

    if not re.search(ROOMS_PRE_FILTER_REGEX, text):
        return storage.VERDICT_PREFILTERED, {"reject_reason": "room_count_mismatch"}

    return None

def replay_all_posts():
    """
    Read-only, no browser, no sheet, no DB writes: re-runs every post stored
    in SQLite (raw_text) through the current filters/LLM/normalization and
    reports which posts' verdict would change vs. what's stored. The tuning
    workflow after editing a prompt/filter/threshold — see exactly what flips
    without rebuilding the sheet (which would wipe hand-edited rows) or
    mutating the DB's cached verdicts.
    Note: the relative date stored at original scan time ("3 days ago") doesn't
    update with time — replay doesn't re-apply the MAX_POST_AGE_DAYS filter, only filters/LLM/normalization.
    """
    posts = storage.get_all_posts()
    print(f"\nReplaying {len(posts)} stored post(s) through the current code (read-only — no sheet/DB writes)...")

    changed = []
    skipped_no_text = 0
    for post in posts:
        url = post["url"]
        old_verdict = post.get("verdict") or "(none)"
        group_url = post.get("group_url") or ""
        text = post.get("raw_text") or ""
        if not text:
            skipped_no_text += 1
            continue
        text = _strip_comment_section(BIDI_RE.sub('', text))

        prefiltered = _replay_text_prefilters(text)
        if prefiltered is not None:
            new_verdict, _ = prefiltered
        else:
            data = analyze_post_with_llm(text)
            if not data:
                new_verdict = storage.VERDICT_PARSE_FAILED
            else:
                new_verdict, fields = _evaluate_post_data(data, text, group_url)
                if new_verdict == storage.VERDICT_ADDED:
                    dist_meters = fields.get("distance_meters")
                    if fields.get("distance_source") == "google_maps" and dist_meters is not None and (dist_meters / 1000.0) > MAX_WALKING_DISTANCE_KM:
                        new_verdict = storage.VERDICT_REJECTED_DISTANCE

        if new_verdict != old_verdict:
            changed.append((url, old_verdict, new_verdict))
            print(f"    CHANGED: {old_verdict} -> {new_verdict}  {url}")

    print(f"\nDone. {len(changed)} verdict(s) changed out of {len(posts)} stored posts "
          f"({skipped_no_text} skipped: raw_text pruned). Nothing was written — "
          f"re-run the real scraper (--live) to actually commit any newly-matching posts.")
    return changed

def print_stats():
    counts, month, gmaps_calls = storage.get_stats()
    print("\n=== Local DB stats ===")
    for verdict in storage.ALL_VERDICTS:
        print(f"  {verdict}: {counts.get(verdict, 0)}")
    print(f"\nGoogle Maps calls this month ({month}): {gmaps_calls}")

def prune_data():
    """
    On-demand cleanup, no browser: drops sheet rows and lightens local DB rows
    older than MAX_POST_AGE_DAYS. Runs automatically at the end of every
    scheduled scan too (run_scraper()) — this flag is for running it standalone.
    """
    sheet, _ = setup_google_sheet()
    print(f"\nPruning rows/records older than {MAX_POST_AGE_DAYS} days...")
    duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(sheet)
    print(f"Removed {duplicates_removed} duplicate repost(s) and {stale_removed} stale row(s). Sheet now has {kept} listings.")
    pruned = storage.prune_old_posts(MAX_POST_AGE_DAYS)
    print(f"Lightened {pruned} local DB row(s) (verdict kept, so they won't be rescanned).")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook Apartment Scraper Bot - Realtime")
    parser.add_argument("--headless", action="store_true", help="Run without UI")
    parser.add_argument("--live", action="store_true",
                         help="Commit results: write matches to Google Sheets, record verdicts, dedupe/prune. "
                              "Default is dry-run (classify + print only, nothing written).")
    parser.add_argument("--reparse-rejected", action="store_true",
                         help="Re-run LLM + filters on stored rejected/failed posts, no browser")
    parser.add_argument("--replay", action="store_true",
                         help="Re-run ALL stored posts through the current code and report which verdicts would change, no browser, no sheet/DB writes")
    parser.add_argument("--stats", action="store_true",
                         help="Print verdict counts and Maps usage from the local DB, then exit")
    parser.add_argument("--prune", action="store_true",
                         help="Drop sheet rows and lighten local DB rows older than MAX_POST_AGE_DAYS, no browser, then exit")
    args = parser.parse_args()

    storage.init_db()

    if args.stats:
        print_stats()
        sys.exit(0)

    if args.reparse_rejected:
        reparse_rejected_posts()
        sys.exit(0)

    if args.replay:
        replay_all_posts()
        sys.exit(0)

    if args.prune:
        prune_data()
        sys.exit(0)

    print("\n=======================================================")
    print("  Apartment Search Bot - Real-time updates")
    print("=======================================================")
    print(f"  Groups:      {len(TARGET_URLS)}")
    print(f"  Areas (info): {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")

    try:
        result = run_scraper(headless=args.headless, live=args.live)
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    if result["checkpoint_skipped"] > 0:
        sys.exit(2)
    if result["groups_scanned"] == 0:
        sys.exit(1)
    sys.exit(0)
