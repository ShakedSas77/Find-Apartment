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
    MAX_CONCURRENT_GROUPS, RELEVANT_SINCE_DATE, SHEET_HEADERS,
    GMAPS_MONTHLY_CAP, GMAPS_ON_CAP
)
from prompts import get_apartment_prompt_improved
import storage

class ApartmentData(BaseModel):
    """סכימת JSON נכפית לתשובת Gemini (response_schema) — מבטלת כשלי parsing על נתיב Gemini."""
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

# מחיקת תווים שקופים (BIDI) שפייסבוק שותל והורסים ביטויים רגולריים
BIDI_RE = re.compile(r'[‎‏‪-‮⁦-⁩]')

# מחיר ל"הזדמנות שנייה" — רק מספר שנמצא בטווח של עד ~25 תווים ממילת/סימן מחיר,
# לא כל מספר 4-5 ספרות בטקסט (כדי לא לתפוס מספר טלפון, תגובה של מישהו אחר וכו').
# \D (לא-ספרה) בפער חוסם מעבר מעל מספר אחר שיושב בין המועמד לסימן — כך "היה 6500
# עכשיו 7200 ש"ח" לא מייחס את ה-ש"ח ל-6500 למרות שהוא בטווח 25 התווים.
_PRICE_MARKER = r'(?:₪|ש["״]?ח|שכ["״]?ד|שכר\s*דירה|מחיר|לחודש)'
_PRICE_CONTEXT_RE = re.compile(
    rf'{_PRICE_MARKER}\D{{0,25}}(?<![0-9])([0-9]{{4,5}})(?![0-9])'
    rf'|(?<![0-9])([0-9]{{4,5}})(?![0-9])\D{{0,25}}{_PRICE_MARKER}'
)

# ─── Concurrency primitives (groups scan in parallel tabs) ────────────────────────
_print_lock = threading.Lock()
_sheet_lock = threading.Lock()  # guards seen_urls reads/writes AND sheet.append_row together
_gemini_lock = threading.Lock()
_checkpoint_lock = threading.Lock()
_resume_event = threading.Event()
_resume_event.set()  # set = running; cleared = paused for a checkpoint on some tab
_headless_checkpoint_hit = False  # set once any group hits a checkpoint in --headless mode; other groups then skip fast

class HeadlessCheckpointAbort(Exception):
    """נזרק כשמתגלה checkpoint/CAPTCHA במצב headless — אי אפשר לפתור ידנית, עוצרים את הקבוצה הזו."""

def _safe_print(msg: str):
    with _print_lock:
        print(msg)

# ─── Helper Functions ─────────────────────────────────────────────────────────────

def _is_visible(locator) -> bool:
    try:
        return locator.first.is_visible()
    except Exception:
        return False

# ממיר תאריך יחסי שפייסבוק מציג ("6h", "1d", "3w", וגם הצורות הארוכות יותר
# שפייסבוק לפעמים מרנדר: "3 hrs", "1 day", "2 wks") לתאריך אבסולוטי (DD/MM)
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

# מזהה תאריכים אבסולוטיים באנגלית שפייסבוק לפעמים מציג במקום טקסט יחסי (למשל "July 9 at 5:50 PM")
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
    ממיר תאריך יחסי/אבסולוטי שפייסבוק מציג לתאריך DD/MM. הבוט מניח ממשק פייסבוק
    באנגלית (לא עברית) — ראה README. תבנית לא מזוהה עוברת כמו שהיא (ללא שינוי),
    עם לוג אזהרה חד-פעמי לריצה כדי שמעבר locale עתידי לא ידרדר בשקט.
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
    ארנונה/ועד בית משולמים סטנדרטית אחת לחודשיים בישראל — אם הפוסט נקב בסכום חודשי,
    מכפילים לערך הדו-חודשי. מחזיר מספר שלם (לא מחרוזת) בלבד, בלי סימני מטבע/יחידה.
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
    מחזיר קומה כמספר שלם. 'קרקע' = 0. מילות סדר בעברית (ראשונה/שנייה/...) ותבנית
    'X מתוך Y' נתמכות. מספר מפורש (אם קיים) גובר על 'קרקע' — "1 מעל קומת קרקע" = 1, לא 0.
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
    """מסיר URLs (חסרי ערך לחילוץ נתונים) וצפיפות רווחים/שורות מיותרת — חוסך טוקנים."""
    clean = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    clean = re.sub(r'\n{2,}', '\n', clean)
    clean = re.sub(r'[ \t]{2,}', ' ', clean)
    return clean.strip()

_NOT_AGENT_RE = re.compile(r'ללא\s+תיווך|לא\s+מתיווך|בלי\s+תיווך|לא\s+תיווך')
_AGENT_SIGNAL_RE = re.compile(r'תיווך|נדל["״]?ן')

def _detect_agent(text: str, llm_is_agent):
    """
    מוסיף אות דטרמיניסטי מעל שיפוט המודל: מילה 'תיווך' או שם קבוצת נדל"ן בטקסט = ודאי
    תיווך. ביטויי שלילה מפורשים ("ללא תיווך" וכו') לא נספרים כאות חיובי — משאירים למודל.
    """
    if _NOT_AGENT_RE.search(text):
        return llm_is_agent
    if _AGENT_SIGNAL_RE.search(text):
        return True
    return llm_is_agent

# 'שותפ'/'שותף' לבד פוסל שותפים, אבל בעל דירה שכותב "מתאים לזוג או ל-2 שותפים" מתאר
# גמישות דיירים — לא שכירת חדר. אם 'זוג' מופיע בסמוך (עד ~30 תווים), לא פוסלים.
# lookbehind (?<!מי) מונע התאמה על 'מיזוג' (מיזוג אוויר, שכיח מאוד במודעות);
# lookahead (?!י) מונע 'זוגי'/'זוגית' (מיטה זוגית). [פף] מכסה גם צורת היחיד "שותף".
_ROOMMATE_COUPLE_EXCEPTION_RE = re.compile(
    r'(?<!מי)זוג(?!י).{0,30}שות[פף]|שות[פף].{0,30}(?<!מי)זוג(?!י)',
    re.DOTALL
)

_PARKING_NONE_RE = re.compile(r'אין\s*חניה|בלי\s*חניה|ללא\s*חניה|^אין$')
_PARKING_PRIVATE_RE = re.compile(r'פרטי|טאבו|מקור|צמוד|תת\s*קרקעי|חניון')
_PARKING_STREET_RE = re.compile(r'רחוב|ציבור|חופשית')

def _classify_parking(raw: str) -> str:
    """מסווג את שדה החניה החופשי מהמודל לאחת משלוש קטגוריות קבועות, או ריק אם לא ברור/לא צוין."""
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

_LATIN_LETTERS_RE = re.compile(r'[A-Za-zÀ-ɏ]+')

def _strip_latin_address(address: str) -> str:
    """
    גיבוי דטרמיניסטי לכלל 'עברית בלבד' בפרומפט — qwen2.5 עדיין דולף לפעמים תעתיק
    לועזי (למשל "רamat Gan", "Białik") למרות ההנחיה. מוחק כל רצף אותיות לטיניות.
    """
    if not address:
        return address
    cleaned = _LATIN_LETTERS_RE.sub('', address)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' \t-–—,/')
    return cleaned

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
    מפעיל את בדיקות הסף (חדרים/מחיר, כולל ה'הזדמנות שנייה' ע"י regex) וכל נרמול
    השדות. shared בין _scan_group ל---reparse-rejected כדי שהלוגיקה תישאר זהה.
    מחזירה (verdict, fields) — fields מכיל את מה שצריך כדי לבנות שורת גיליון
    כש-verdict הוא storage.VERDICT_ADDED.
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

    # --- תיקון שגיאות והזדמנות שנייה למחיר ---
    # ה"הזדמנות שנייה" (חיפוש מספר בטקסט) רצה רק כשהמודל לא החזיר מחיר כלל —
    # אם המודל כן החזיר ערך אמיתי (גם אם מחוץ לתקציב, למשל 7200), לא דורסים אותו
    # במספר אחר סתם כי הוא נמצא ליד סימן מחיר בטקסט (יכול להיות "מחיר קודם", ועד
    # שנתי וכו') — זה בדיוק מה שיצר false positives בעבר.
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

    # --- הזדמנות שנייה לחדרים: רק כשהמודל לא החזיר מספר חדרים תקין כלל ---
    # (לא דורסים ערך מספרי אמיתי שהמודל כן החזיר, גם אם הוא מחוץ לטווח היעד)
    if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS) and rooms_missing_or_invalid:
        clean_text_rooms = BIDI_RE.sub('', text)
        room_matches = [float(r) for r in re.findall(r'([1-9](?:\.5)?)\s*חד', clean_text_rooms)]
        valid_rooms = [r for r in room_matches if MIN_ROOMS <= r <= MAX_ROOMS]
        if valid_rooms:
            rooms_val = valid_rooms[0]

    if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS):
        return storage.VERDICT_REJECTED_ROOMS, {"rooms_val": rooms_val, "price_val": price_val}
    if not (MIN_PRICE <= price_val <= MAX_PRICE):
        return storage.VERDICT_REJECTED_PRICE, {"rooms_val": rooms_val, "price_val": price_val}

    arnona = _normalize_bimonthly_fee(data.get("arnona") or "")
    vaad = _normalize_bimonthly_fee(data.get("vaad") or "")
    _warn_if_fee_implausible("Vaad bayit", vaad, 2400)
    _warn_if_fee_implausible("Arnona", arnona, 3000)

    address = _strip_latin_address(data.get("address") or "")
    address = _reject_hallucinated_address(address, text)
    floor = _parse_floor(data.get("floor") or "")
    is_agent = _detect_agent(text, data.get("is_agent"))
    parking = _classify_parking(data.get("parking") or "")

    return storage.VERDICT_ADDED, {
        "rooms_val": rooms_val, "price_val": price_val, "arnona": arnona, "vaad": vaad,
        "address": address, "floor": floor, "is_agent": is_agent, "parking": parking,
        "entry_date": data.get("entry_date") or "", "elevator": data.get("elevator"),
        "shelter": data.get("shelter"),
    }

def _build_row(post_url: str, fb_post_date: str, fields: dict) -> list:
    dist_text, _ = get_walking_distance(fields["address"])
    return [
        post_url,
        int(fields["price_val"]),
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
    ]

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

        # שולפים רק את עמודת ה-URL (עמודה 1) לדה-דופליקציה — לא את כל הטבלה
        seen_urls = {url for url in sheet.col_values(1)[1:] if url}

        print(f"    Found {len(seen_urls)} existing apartments in the sheet. Will skip them.")
        return sheet, seen_urls
    except Exception as e:
        print(f"    ERROR: reading Google Sheet: {e}")
        print("    Aborting: cannot dedupe or write results without the sheet.")
        sys.exit(1)

# ─── Cross-post dedup + sort ────────────────────────────────────────────────
# פוסטים לפעמים מתפרסמים מחדש או בכמה קבוצות תחת URL שונה — לא נתפס ע"י seen_urls.
# מזהים כפילות לפי (רחוב מנורמל, חדרים, מחיר) ושומרים את הפרסום העדכני ביותר.
_CITY_TOKENS_RE = re.compile(r'רמת[\s-]?גן|גבעתיים|תל[\s-]?אביב|ר["״]?ג\b|\bרג\b')
_ADDRESS_PUNCT_RE = re.compile(r'[",./\-–—_]')
_POST_DATE_DDMM_RE = re.compile(r'^(\d{1,2})/(\d{1,2})$')

def _normalize_address_key(address: str) -> str:
    if not address or address == "לא צוין":
        return ""
    norm = _CITY_TOKENS_RE.sub('', address)
    norm = _ADDRESS_PUNCT_RE.sub(' ', norm)
    return re.sub(r'\s+', ' ', norm).strip()

def _post_date_sort_key(date_str: str) -> tuple:
    match = _POST_DATE_DDMM_RE.match((date_str or "").strip())
    if not match:
        return (-1, -1)
    return (int(match.group(2)), int(match.group(1)))  # (month, day)

_RELEVANT_SINCE_KEY = _post_date_sort_key(RELEVANT_SINCE_DATE)

def _listing_key(row: list) -> tuple:
    address_key = _normalize_address_key(row[13] if len(row) > 13 else "")
    try:
        rooms = f"{float(row[2]):.1f}"
    except (ValueError, IndexError):
        rooms = row[2] if len(row) > 2 else ""
    try:
        price = str(int(float(row[1])))
    except (ValueError, IndexError):
        price = row[1] if len(row) > 1 else ""
    return (address_key, rooms, price)

def dedupe_and_sort_sheet(sheet) -> tuple[int, int]:
    """
    מסירה כפילויות (אותה דירה, URL שונה) ושומרת רק את הפרסום העדכני ביותר לפי
    'תאריך פרסום', ואז ממיינת את כל השורות מהחדש לישן. מריצים בסוף כל ריצה,
    ואפשר גם ידנית מול טבלה קיימת. מחזירה (מספר שהוסרו, מספר שנשמרו).
    """
    data = sheet.get_all_values()
    if len(data) <= 1:
        return 0, len(data) - 1 if data else 0
    rows = data[1:]

    best_by_key = {}
    for row in rows:
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

def _get_llm_raw_result(prompt: str) -> dict | None:
    """
    מריץ ניסיון פרסור LLM יחיד (Gemini אם לא exhausted, אחרת Ollama). מחזירה dict
    גולמי, לפני ולידציה מול הסכמה — הולידציה מתבצעת ברמת analyze_post_with_llm
    כדי לחול על שני הנתיבים באופן זהה.
    """
    global GEMINI_EXHAUSTED, GEMINI_ERROR_COUNT
    if not GEMINI_EXHAUSTED:
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
                _safe_print("    המודל הוגדר לא נכון או הוצא משימוש — עדכן GEMINI_MODEL ב-config.py")
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
            format=ApartmentData.model_json_schema(),  # מכריח decoding תואם-סכמה — לא צריך תיקוני JSON ידניים יותר
            options={'temperature': 0, 'num_ctx': 4096},
            keep_alive='10m',
        )
        return json.loads(ollama_response['message']['content'])
    except Exception as ollama_err:
        _safe_print(f"\n    ERROR: local Ollama analysis failed: {ollama_err}")
        return None

def analyze_post_with_llm(text: str) -> dict | None:
    """
    שני ניסיונות מקסימום: כל ניסיון מריץ LLM ואז מוודא את הפלט מול ApartmentData.
    כשל ולידציה בניסיון הראשון -> ניסיון חוזר יחיד; כשל גם בשני -> None (verdict
    parse_failed אצל הקורא).
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
    """מריץ fn עד attempts פעמים על כשל, עם המתנה גוברת בין ניסיונות. חוזרת/זורקת בניסיון האחרון."""
    last_err = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
    raise last_err

_CITY_ONLY_ADDRESSES = {"רמת גן", "רמת-גן", "גבעתיים", "תל אביב", 'ר"ג', "ר״ג"}

class GmapsQuotaHalted(Exception):
    """נזרק כש-GMAPS_ON_CAP == 'halt' והמכסה החודשית הגיעה לתקרה — עוצר את כל הריצה."""

_gmaps_cap_lock = threading.Lock()
_gmaps_cap_notice_printed = False

def get_walking_distance(address: str):
    """מחזיר (מרחק בק"מ כמחרוזת מספרית בלבד, למשל '1.4', או '' אם לא ניתן לחשב; מרחק במטרים)."""
    if not address or len(address) < 3:
        return "", 999999
    if address.strip() in _CITY_ONLY_ADDRESSES:
        # אין רחוב, רק שם עיר — לא שווה לבזבז קריאת API על זה
        return "", 999999

    global _gmaps_cap_notice_printed
    if storage.get_gmaps_usage() >= GMAPS_MONTHLY_CAP:
        with _gmaps_cap_lock:
            already_notified = _gmaps_cap_notice_printed
            _gmaps_cap_notice_printed = True
        if not already_notified:
            _safe_print(f"\n    WARNING: Google Maps monthly cap reached ({GMAPS_MONTHLY_CAP} calls). "
                        f"GMAPS_ON_CAP='{GMAPS_ON_CAP}' in config.py.")
        if GMAPS_ON_CAP == "halt":
            raise GmapsQuotaHalted()
        return "מכסה חודשית הסתיימה", 999999

    # תוספת רשת ביטחון ל-Google Maps: הבטחת אזור החיפוש
    if not any(city in address for city in ["רמת גן", "גבעתיים", "תל אביב", "רמת-גן", "ר\"ג"]):
        address = f"{address}, רמת גן, גבעתיים, ישראל"

    try:
        storage.increment_gmaps_usage()  # נספר לפני הקריאה — origins×destinations הוא תמיד 1×1 כאן
        result = _with_retries(lambda: gmaps_client.distance_matrix(
            origins=address,
            destinations=DESTINATION_ADDRESS,
            mode="walking",
        ))
        element = result["rows"][0]["elements"][0]
        if element["status"] == "OK":
            dist_meters = element["distance"]["value"]
            return f"{dist_meters / 1000:.1f}", dist_meters
        return "", float('inf')
    except Exception as e:
        _safe_print(f"\n    [Google Maps API Error]: {e}")
        return "", float('inf')

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
    אם פייסבוק מבקש התחברות/2FA/CAPTCHA — עוצר את כל הקבוצות, לא רק את זו הנוכחית.
    במצב headless אין חלון גלוי לפתור בו את זה ידנית, אז שומרים צילום מסך, מסמנים
    לכל שאר הקבוצות לוותר במקום כל אחת לפגוש את אותו קיר בעצמה, וזורקים חריגה
    שמאותתת ל-_scan_group לעצור את הקבוצה הזו בניקיון.
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
    לוגיקת הסריקה בפועל של קבוצה אחת, מריצה על page שכבר קיים. משותפת בין מצב
    מקבילי (_scan_group, שיוצר browser/context ייעודי לכל thread) לבין מצב סדרתי
    (run_scraper כש-MAX_CONCURRENT_GROUPS == 1, שמריץ את כל הקבוצות ברצף על אותו
    page בתוך ה-persistent context המקורי, לשימור טביעת האצבע האמיתית של הפרופיל).

    מחזירה dict לסיכום הריצה: added, checkpoint_hit (True כשהקבוצה דולגה/הופסקה
    בגלל checkpoint שאי אפשר לפתור במצב headless), posts_seen, prefiltered, llm_parsed.
    """
    stats = {"added": 0, "checkpoint_hit": False, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}
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
            # ג'יטר גם על מרחק הגלילה, לא רק על ההמתנה — קצב וגודל גלילה קבועים לחלוטין נראה יותר בוטי
            page.mouse.wheel(0, random.randint(3000, 5000))
            jittered_delay = max(500, SCROLL_DELAY_MS + random.randint(-400, 400))
            page.wait_for_timeout(jittered_delay)
        _safe_print(f"[{group_label}] Done scrolling.")

        # לחץ על "קרא עוד" כדי לחשוף את כל הטקסט של הפוסטים הארוכים
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

            post_url, fb_post_date = extract_post_info(article)
            if post_url == "Link not extracted":
                continue  # מדלגים על תגובות או אלמנטים שאינם פוסט אמיתי

            if storage.should_skip(post_url):
                _safe_print(f"    [{group_label}] Pre-filtered: Already processed (cached verdict in local DB).")
                continue

            with _sheet_lock:
                already_seen = post_url in seen_urls
            if already_seen:
                _safe_print(f"    [{group_label}] Pre-filtered: Post already exists in Google Sheets (Duplicate).")
                continue

            post_date_key = _post_date_sort_key(fb_post_date)
            if post_date_key != (-1, -1) and post_date_key < _RELEVANT_SINCE_KEY:
                _safe_print(f"    [{group_label}] Pre-filtered: Post date {fb_post_date} is before cutoff ({RELEVANT_SINCE_DATE}).")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
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
            if re.search(r'ל\s*מ\s*כ\s*י\s*ר\s*ה', text):
                # Look for prices >= 1,000,000 in any format (commas, dots, spaces) or the word "מיליון"
                sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
                if sale_price_match:
                    _safe_print(f"    [{group_label}] Pre-filtered: Apartment for sale (found 'למכירה' + {sale_price_match.group(0).strip()}).")
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

            _safe_print(f"[{group_label}] Analyzing post (URL: {post_url})...")
            time.sleep(2)

            data = analyze_post_with_llm(text)
            stats["llm_parsed"] += 1
            if not data:
                _safe_print(f"    [{group_label}] Skipped: LLM failed to parse or returned no data.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PARSE_FAILED)
                continue

            verdict, fields = _evaluate_post_data(data, text)
            if verdict == storage.VERDICT_REJECTED_ROOMS:
                _safe_print(f"    [{group_label}] Skipped: Room count is not suitable ({fields['rooms_val']}).")
                storage.record_post(post_url, target_url, text, verdict, data)
                continue
            if verdict == storage.VERDICT_REJECTED_PRICE:
                _safe_print(f"    [{group_label}] Skipped: Price is not suitable ({int(fields['price_val']):,} ₪).")
                storage.record_post(post_url, target_url, text, verdict, data)
                continue

            new_row = _build_row(post_url, fb_post_date, fields)

            with _sheet_lock:
                try:
                    _with_retries(lambda: sheet.append_row(new_row))
                    seen_urls.add(post_url)
                    stats["added"] += 1
                    storage.record_post(post_url, target_url, text, storage.VERDICT_ADDED, data)
                    _safe_print(f"    SUCCESS: [{group_label}] Apartment added: {fields['rooms_val']} rooms | {int(fields['price_val']):,} ₪ | {new_row[3]} | Address: {fields['address']}")
                except Exception as e:
                    _safe_print(f"    ERROR: [{group_label}] writing to sheet: {e}")
    except GmapsQuotaHalted:
        _safe_print(f"    [{group_label}] Stopping: Google Maps monthly cap reached (GMAPS_ON_CAP='halt').")
    except HeadlessCheckpointAbort:
        stats["checkpoint_hit"] = True
    return stats

def _scan_group(target_url: str, group_label: str, sheet, seen_urls, storage_state_path: str, headless: bool) -> dict:
    """
    מצב מקבילי: כל thread מריץ instance משלו של Playwright (ה-sync API אינו
    thread-safe כשחולקים browser/context אחד בין threads) — ההתחברות משותפת דרך
    storage_state שיוצא פעם אחת מהפרופיל הראשי, לא דרך שיתוף אובייקט ה-context.
    הלוגיקה בפועל של סריקת קבוצה משותפת עם המצב הסדרתי דרך _scan_group_page.
    """
    # פיזור זמן פתיחה אקראי בין הטאבים — פחות נראה כמו בוט מאשר פתיחה סימולטנית של כולם
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
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(storage_state=storage_state_path, no_viewport=True)
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
            no_viewport=True,
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"]
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
            # מצב סדרתי: אין storage_state בכלל — סורקים את כל הקבוצות ברצף על אותו
            # page בתוך ה-context המקורי, לשימור טביעת האצבע האמיתית של הפרופיל
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
            # מייצאים cookies/session לקובץ כדי ש-threads עצמאיים יוכלו להשתמש בהם —
            # לא ניתן לשתף context/browser אחד בין threads (Playwright sync API אינו thread-safe)
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
    previously verdict-ed rejected_price / rejected_rooms / parse_failed — the
    prompt-iteration workflow. Opens no browser; still writes real matches to the
    sheet via the normal (non-Playwright) Sheets/Maps API clients.
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
            storage.record_post(url, group_url, text, verdict, data)
            print(f"    Still {verdict}: {url}")
            continue

        new_row = _build_row(url, "", fields)
        try:
            _with_retries(lambda: sheet.append_row(new_row))
            seen_urls.add(url)
            storage.record_post(url, group_url, text, storage.VERDICT_ADDED, data)
            added += 1
            print(f"    SUCCESS: Apartment added: {fields['rooms_val']} rooms | {int(fields['price_val']):,} ₪ | Address: {fields['address']}")
        except Exception as e:
            print(f"    ERROR: writing to sheet: {e}")

    print(f"\nDone. {added} new apartment(s) added from reparse.")

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

    print("\n=======================================================")
    print("  Apartment Search Bot - Real-time updates")
    print("=======================================================")
    print(f"  Groups:      {len(TARGET_URLS)}")
    print(f"  Areas (info): {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")

    run_scraper(headless=args.headless)
