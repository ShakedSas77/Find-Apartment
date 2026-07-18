import argparse
import json
import sys
import time
import os
import re
import random
import threading
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
    INCLUDE_PRICE_UNKNOWN
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

# Strips invisible BIDI characters that Facebook injects and that break regexes
BIDI_RE = re.compile(r'[‎‏‪-‮⁦-⁩]')

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

def _normalize_entry_date(text: str) -> str:
    """
    Normalizes the entry date, strips foreign-language text, and converts
    "immediate"-type phrases to the standard "מיידי".
    """
    if not text:
        return ""
    text = _strip_foreign_letters(text)
    if _IMMEDIATE_RE.search(text):
        return "מיידי"
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

def _evaluate_post_data(data: dict, text: str) -> tuple[str, dict]:
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

def _build_row(post_url: str, fb_post_date: str, fields: dict) -> list:
    dist_text, dist_meters, address_confidence, address_warning, distance_source = get_walking_distance(fields["address"])
    fields["distance_text"] = dist_text
    fields["distance_meters"] = dist_meters
    fields["address_confidence"] = address_confidence
    fields["address_warning"] = address_warning
    fields["distance_source"] = distance_source

    return [
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
_STREET_HINT_RE = re.compile(r'רחוב|רח׳|שדרות|שד׳|דרך|סמטת|סמטה|כיכר|משעול')
_LANDMARK_HINT_RE = re.compile(r'ליד|בסמוך|קרוב ל|צמוד ל|באזור|בשכונת|שכונת')

def _normalize_address_key(address: str) -> str:
    if not address or address == "לא צוין":
        return ""
    norm = _CITY_TOKENS_RE.sub('', address)
    norm = _ADDRESS_PUNCT_RE.sub(' ', norm)
    return re.sub(r'\s+', ' ', norm).strip()

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

def _listing_key(row: list) -> tuple:
    url = row[0] if len(row) > 0 else ""
    address_key = _normalize_address_key(row[13] if len(row) > 13 else "")

    if not address_key or len(address_key) < 4:
        return ("url", url)

    try:
        rooms = f"{float(row[2]):.1f}"
    except (ValueError, IndexError):
        rooms = row[2] if len(row) > 2 else ""
    try:
        price = str(int(float(row[1])))
    except (ValueError, IndexError):
        price = row[1] if len(row) > 1 else ""
    return ("listing", address_key, rooms, price)

def dedupe_and_sort_sheet(sheet) -> tuple[int, int]:
    """
    Removes duplicates:
    1. Identical URL - always a duplicate.
    2. Same apartment by meaningful address + rooms + price - keeps the most recent post.
    Missing/weak addresses aren't merged by price+rooms alone, so real distinct
    apartments don't get accidentally deleted.
    """
    data = sheet.get_all_values()
    if len(data) <= 1:
        return 0, len(data) - 1 if data else 0
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

    best_by_key = {}
    for row in best_by_url.values():
        key = _listing_key(row)
        existing = best_by_key.get(key)
        if existing is None or _post_date_sort_key(row[12] if len(row) > 12 else "") > _post_date_sort_key(existing[12] if len(existing) > 12 else ""):
            best_by_key[key] = row

    deduped_rows = list(best_by_key.values())
    deduped_rows.sort(key=lambda r: _post_date_sort_key(r[12] if len(r) > 12 else ""), reverse=True)

    removed = len(rows) - len(deduped_rows)
    last_col = chr(ord('A') + len(SHEET_HEADERS) - 1)
    _with_retries(lambda: sheet.batch_clear([f"A2:{last_col}{len(rows) + 1}"]))
    if deduped_rows:
        _with_retries(lambda: sheet.update(range_name=f"A2:{last_col}{len(deduped_rows) + 1}", values=deduped_rows))
    return removed, len(deduped_rows)

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
    against ApartmentData. Validation failure on the first attempt -> a single
    retry; failure on the second too -> None (verdict parse_failed for the caller).
    """
    prompt = get_apartment_prompt_improved(_clean_post_for_llm(text))
    for attempt in range(2):
        raw = _get_llm_raw_result(prompt)
        if raw is None:
            return None
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
        _safe_print(f"\n    [Google Geocoding API Error]: {e}")
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
        _safe_print(f"\n    [Google Maps API Error]: {e}")
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

def _scan_group_page(page, target_url: str, group_label: str, sheet, seen_urls, headless: bool) -> dict:
    """
    The actual scan logic for a single group, run on a page that already
    exists. Shared between parallel mode (_scan_group, which creates a
    dedicated browser/context per thread) and sequential mode (run_scraper
    when MAX_CONCURRENT_GROUPS == 1, which runs every group in sequence on the
    same page inside the original persistent context, to preserve the
    profile's real fingerprint).

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

        _safe_print(f"[{group_label}] Scrolling ({SCROLL_COUNT} times)...")
        for _ in range(SCROLL_COUNT):
            # Jitter the scroll distance too, not just the delay — a perfectly fixed pace and size reads as more bot-like
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
            'div[role="feed"] > div > div'
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
            page.screenshot(path=f"debug_fb_{group_label.replace(' ', '_').replace('/', '-')}.png")
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
                storage.record_post(post_url, target_url, text, storage.VERDICT_PARSE_FAILED)
                return

            verdict, fields = _evaluate_post_data(data, text)
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
                    _safe_print(f"    SUCCESS: [{group_label}] Apartment queued: {fields['rooms_val']} rooms | {price_display} | {new_row[3]} | Address: {fields['address']}")
                else:
                    _safe_print(f"    [{group_label}] Skipped before queueing: duplicate URL.")

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_post, post) for post in valid_posts]
            for future in as_completed(futures):
                future.result()

        if pending_rows:
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
    except GmapsQuotaHalted:
        _safe_print(f"    [{group_label}] Stopping: Google Maps monthly cap reached (GMAPS_ON_CAP='halt').")
    except HeadlessCheckpointAbort:
        stats["checkpoint_hit"] = True
    return stats

def _scan_group(target_url: str, group_label: str, sheet, seen_urls, storage_state_path: str, headless: bool) -> dict:
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
            channel="chrome",
            headless=headless,
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=["--disable-blink-features=AutomationControlled", "--autoplay-policy=user-gesture-required"]
        )
        context = browser.new_context(storage_state=storage_state_path, viewport={"width": 1366, "height": 1600})
        page = context.new_page()
        try:
            return _scan_group_page(page, target_url, group_label, sheet, seen_urls, headless)
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

def run_scraper(headless: bool = False):
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
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel="chrome",
            headless=headless,
            viewport={"width": 1366, "height": 1600},
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=["--disable-blink-features=AutomationControlled", "--autoplay-policy=user-gesture-required"]
        )
        page = context.pages[0] if context.pages else context.new_page()

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

        if sequential_mode:
            # Sequential mode: no storage_state at all — scans every group in
            # sequence on the same page inside the original context, to
            # preserve the profile's real fingerprint
            print("Sequential mode (MAX_CONCURRENT_GROUPS=1): scanning all groups one at a time in the original "
                  "browser profile — safest against checkpoints, slowest. No session-state file is written.")
            for i, url in enumerate(shuffled_urls, 1):
                if i > 1:
                    time.sleep(random.uniform(5, 15))
                stats = _scan_group_page(page, url, f"Group {i}/{total}", sheet, seen_urls, headless)
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
            # Exports cookies/session to a file so independent threads can use
            # them — one context/browser can't be shared between threads (Playwright's sync API isn't thread-safe)
            context.storage_state(path=storage_state_path)
            context.close()

    if not sequential_mode:
        print(f"Continuing to scan groups ({MAX_CONCURRENT_GROUPS} in parallel — this raises checkpoint/ban risk "
              f"vs. sequential mode; set MAX_CONCURRENT_GROUPS=1 in config.py if you start seeing checkpoints)...")
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GROUPS) as executor:
            futures = {
                executor.submit(_scan_group, url, f"Group {idx}/{total}", sheet, seen_urls, storage_state_path, headless): idx
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

    print("Deduplicating cross-posted listings and sorting by post date...")
    removed, kept = dedupe_and_sort_sheet(sheet)
    print(f"Removed {removed} duplicate repost(s). Sheet now has {kept} listings, sorted by post date (newest first).")

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
        if url in seen_urls:
            continue

        data = analyze_post_with_llm(text)
        if not data:
            storage.record_post(url, group_url, text, storage.VERDICT_PARSE_FAILED)
            print(f"    Still failing to parse: {url}")
            continue

        verdict, fields = _evaluate_post_data(data, text)
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
        removed, kept = dedupe_and_sort_sheet(sheet)
        print(f"Removed {removed} duplicate repost(s). Sheet now has {kept} listings, sorted by post date (newest first).")
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
    Local replay, no browser: backs up the sheet to a new tab, clears the data
    rows, then re-runs every post stored in SQLite (raw_text) through the
    current code — including a fresh LLM call per post. Useful for testing a
    prompt/filter/normalization change without re-scraping Facebook (no
    checkpoint risk, no waiting for scrolling).
    Note: the relative date stored at original scan time ("3 days ago") doesn't
    update with time — replay doesn't re-apply the MAX_POST_AGE_DAYS filter, only filters/LLM/normalization.
    """
    sheet, _ = setup_google_sheet()

    backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        sheet.duplicate(new_sheet_name=backup_name)
        print(f"Backed up current sheet to tab '{backup_name}'.")
    except Exception as e:
        print(f"WARNING: could not create backup tab ({e}) — continuing anyway.")

    row_count = len(sheet.get_all_values())
    if row_count > 1:
        sheet.batch_clear([f"A2:Z{row_count}"])
    seen_urls = set()

    posts = storage.get_all_posts()
    print(f"\nReplaying {len(posts)} stored post(s) through the current code (no browser)...")

    added = 0
    for post in posts:
        url = post["url"]
        group_url = post["group_url"]
        post_date = post.get("post_date") or ""
        text = post.get("raw_text") or ""
        if not text or url in seen_urls:
            continue
        text = _strip_comment_section(BIDI_RE.sub('', text))

        prefiltered = _replay_text_prefilters(text)
        if prefiltered is not None:
            verdict, analysis = prefiltered
            storage.record_post(url, group_url, text, verdict, analysis=analysis)
            continue

        data = analyze_post_with_llm(text)
        if not data:
            storage.record_post(url, group_url, text, storage.VERDICT_PARSE_FAILED)
            continue

        verdict, fields = _evaluate_post_data(data, text)
        if verdict != storage.VERDICT_ADDED:
            storage.record_post(url, group_url, text, verdict, data, analysis=_analysis_from_fields(fields, post_date, verdict))
            continue

        new_row = _build_row(url, post_date, fields)
        dist_meters = fields.get("distance_meters")
        if fields.get("distance_source") == "google_maps" and dist_meters is not None and (dist_meters / 1000.0) > MAX_WALKING_DISTANCE_KM:
            storage.record_post(url, group_url, text, storage.VERDICT_REJECTED_DISTANCE, data, analysis=_analysis_from_fields(fields, post_date, storage.VERDICT_REJECTED_DISTANCE))
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
        removed, kept = dedupe_and_sort_sheet(sheet)
        print(f"Removed {removed} duplicate repost(s). Sheet now has {kept} listings, sorted by post date (newest first).")
    print(f"\nDone. {added} apartment(s) added from replay (out of {len(posts)} stored posts).")

def print_stats():
    counts, month, gmaps_calls = storage.get_stats()
    print("\n=== Local DB stats ===")
    for verdict in storage.ALL_VERDICTS:
        print(f"  {verdict}: {counts.get(verdict, 0)}")
    print(f"\nGoogle Maps calls this month ({month}): {gmaps_calls}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook Apartment Scraper Bot - Realtime")
    parser.add_argument("--headless", action="store_true", help="Run without UI")
    parser.add_argument("--reparse-rejected", action="store_true",
                         help="Re-run LLM + filters on stored rejected/failed posts, no browser")
    parser.add_argument("--replay", action="store_true",
                         help="Backup + clear the sheet, then rebuild it from ALL stored posts via the current code, no browser")
    parser.add_argument("--stats", action="store_true",
                         help="Print verdict counts and Maps usage from the local DB, then exit")
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

    print("\n=======================================================")
    print("  Apartment Search Bot - Real-time updates")
    print("=======================================================")
    print(f"  Groups:      {len(TARGET_URLS)}")
    print(f"  Areas (info): {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")

    run_scraper(headless=args.headless)
