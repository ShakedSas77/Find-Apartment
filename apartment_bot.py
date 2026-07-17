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
    CREDENTIALS_FILE, SHEET_ID, TARGET_URLS,
    MIN_PRICE, MAX_PRICE, DESTINATION_ADDRESS,
    SCROLL_COUNT, SCROLL_DELAY_MS, LOCATIONS,
    MIN_ROOMS, MAX_ROOMS, ROOMS_PRE_FILTER_REGEX,
    NEGATIVE_KEYWORDS, EXCLUDED_LOCATIONS,
    GEMINI_MAX_CONSECUTIVE_ERRORS, LOGIN_MAX_ATTEMPTS,
    MAX_CONCURRENT_GROUPS, SHEET_HEADERS
)
from prompts import get_apartment_prompt_improved

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
    return "לא צוין"

# --- Load environment variables ---
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GMAPS_API_KEY = os.getenv("GMAPS_API_KEY")

if not GEMINI_API_KEY or not GMAPS_API_KEY:
    print("❌ Error: Missing API keys in .env file")
    sys.exit(1)

# --- API Clients ---
client = genai.Client(api_key=GEMINI_API_KEY)
gmaps_client = googlemaps.Client(key=GMAPS_API_KEY)

GEMINI_EXHAUSTED = False
GEMINI_ERROR_COUNT = 0

# מחיקת תווים שקופים (BIDI) שפייסבוק שותל והורסים ביטויים רגולריים
BIDI_RE = re.compile(r'[‎‏‪-‮⁦-⁩]')

# ─── Concurrency primitives (groups scan in parallel tabs) ────────────────────────
_print_lock = threading.Lock()
_sheet_lock = threading.Lock()  # guards seen_urls reads/writes AND sheet.append_row together
_gemini_lock = threading.Lock()
_checkpoint_lock = threading.Lock()
_resume_event = threading.Event()
_resume_event.set()  # set = running; cleared = paused for a checkpoint on some tab

def _safe_print(msg: str):
    with _print_lock:
        print(msg)

# ─── Helper Functions ─────────────────────────────────────────────────────────────

def _is_visible(locator) -> bool:
    try:
        return locator.first.is_visible()
    except Exception:
        return False

# ממיר תאריך יחסי שפייסבוק מציג ("6h", "1d", "3w") לתאריך אבסולוטי (DD/MM)
_RELATIVE_DATE_RE = re.compile(r'^(\d+)\s*(s|m|h|d|w)$', re.IGNORECASE)
_RELATIVE_DATE_UNITS = {
    's': lambda v: timedelta(seconds=v),
    'm': lambda v: timedelta(minutes=v),
    'h': lambda v: timedelta(hours=v),
    'd': lambda v: timedelta(days=v),
    'w': lambda v: timedelta(weeks=v),
}

def relative_to_date(rel: str) -> str:
    match = _RELATIVE_DATE_RE.match((rel or "").strip())
    if not match:
        return rel
    value, unit = int(match.group(1)), match.group(2).lower()
    return (datetime.now() - _RELATIVE_DATE_UNITS[unit](value)).strftime("%d/%m")

def _normalize_bimonthly_fee(raw: str) -> str:
    """ארנונה/ועד בית משולמים סטנדרטית אחת לחודשיים בישראל — אם הפוסט נקב בסכום חודשי, מכפילים לערך הדו-חודשי."""
    if not raw or raw == "לא צוין":
        return raw
    digits = re.sub(r'[^\d]', '', raw)
    if not digits:
        return raw
    if re.search(r'לחודש(?!יים)', raw):
        return f"{int(digits) * 2}₪ לחודשיים"
    return raw

def _clean_post_for_llm(raw_text: str) -> str:
    """מסיר URLs (חסרי ערך לחילוץ נתונים) וצפיפות רווחים/שורות מיותרת — חוסך טוקנים."""
    clean = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    clean = re.sub(r'\n{2,}', '\n', clean)
    clean = re.sub(r'[ \t]{2,}', ' ', clean)
    return clean.strip()

def _warn_if_fee_implausible(label: str, raw: str, max_bimonthly: int):
    if not raw or raw == "לא צוין":
        return
    digits = re.sub(r'[^\d]', '', raw)
    if not digits:
        return
    if int(digits) > max_bimonthly:
        print(f"\n    ⚠ {label} looks unusually high ({raw}) — verify manually.")

def setup_google_sheet():
    """
    Connects to Google Sheets, checks existing data,
    and creates headers if the sheet is empty.
    Returns the sheet object and a set of already seen URLs.
    """
    print("\n📊 Connecting to Google Sheets and reading existing data...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SHEET_ID).sheet1

    try:
        # Check headers via row 1 only — avoids pulling the whole sheet into memory
        headers = sheet.row_values(1)

        if not headers:
            print("📝 Missing column headers in Google Sheet. Adding them to row 1...")
            sheet.insert_row(SHEET_HEADERS, 1)
        elif len(headers) != len(SHEET_HEADERS) or headers[0] != "לינק למודעה":
            print("📝 Outdated column headers in Google Sheet. Updating row 1...")
            if headers and headers[0] == "לינק למודעה":
                sheet.delete_rows(1)
            sheet.insert_row(SHEET_HEADERS, 1)

        # שולפים רק את עמודת ה-URL (עמודה 1) לדה-דופליקציה — לא את כל הטבלה
        seen_urls = {url for url in sheet.col_values(1)[1:] if url}

        print(f"    Found {len(seen_urls)} existing apartments in the sheet. Will skip them.")
        return sheet, seen_urls
    except Exception as e:
        print(f"    ❌ Error reading Google Sheet: {e}")
        print("    Aborting: cannot dedupe or write results without the sheet.")
        sys.exit(1)

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
                post_date = "לא צוין"
                try:
                    link_text = link.inner_text().strip()
                    if link_text:
                        post_date = relative_to_date(link_text)
                except Exception:
                    pass
                    
                return clean, post_date
    except Exception:
        pass
    return "Link not extracted", "לא צוין"

def analyze_post_with_llm(text: str) -> dict | None:
    global GEMINI_EXHAUSTED, GEMINI_ERROR_COUNT
    prompt = get_apartment_prompt_improved(_clean_post_for_llm(text))
    if not GEMINI_EXHAUSTED:
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash',
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
                _safe_print("\n    ⏳ Gemini quota exhausted. Switching permanently to Ollama... ")
                with _gemini_lock:
                    GEMINI_EXHAUSTED = True
            else:
                with _gemini_lock:
                    GEMINI_ERROR_COUNT += 1
                    current_count = GEMINI_ERROR_COUNT
                _safe_print(f"\n    ⚠ Gemini Error ({error_msg}). Falling back to Ollama... ")
                if current_count >= GEMINI_MAX_CONSECUTIVE_ERRORS:
                    _safe_print(f"\n    ⏳ {current_count} consecutive Gemini errors. Switching permanently to Ollama... ")
                    with _gemini_lock:
                        GEMINI_EXHAUSTED = True
    else:
        _safe_print("[Local Ollama] ")

    try:
        ollama_response = ollama.chat(
            model='qwen2.5:7b',
            messages=[{'role': 'user', 'content': prompt}],
            format='json',
            options={'temperature': 0}
        )
        response_text = ollama_response['message']['content']
        
        # ניקוי הטקסט: שליפת ה-JSON בלבד במקרה ש-Ollama הוסיף טקסט מיותר מסביב
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(0)
        # Repair common local-model JSON issues: trailing commas before } or ]
        response_text = re.sub(r',\s*([}\]])', r'\1', response_text)
        return json.loads(response_text)
    except Exception as ollama_err:
        print(f"\n    ❌ Error in local Ollama analysis: {ollama_err}")
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

def get_walking_distance(address: str):
    if not address or address == "לא צוין" or len(address) < 3:
        return "No street specified", 999999
    if address.strip() in _CITY_ONLY_ADDRESSES:
        # אין רחוב, רק שם עיר — לא שווה לבזבז קריאת API על זה
        return "No street specified", 999999

    # תוספת רשת ביטחון ל-Google Maps: הבטחת אזור החיפוש
    if not any(city in address for city in ["רמת גן", "גבעתיים", "תל אביב", "רמת-גן", "ר\"ג"]):
        address = f"{address}, רמת גן, גבעתיים, ישראל"

    try:
        result = _with_retries(lambda: gmaps_client.distance_matrix(
            origins=address,
            destinations=DESTINATION_ADDRESS,
            mode="walking",
        ))
        element = result["rows"][0]["elements"][0]
        if element["status"] == "OK":
            dist_text = element["distance"]["text"]
            dist_meters = element["distance"]["value"]
            duration = element["duration"]["text"]
            return f"{dist_text} ({duration} walk)", dist_meters
        return "", float('inf')
    except Exception as e:
        print(f"\n    [Google Maps API Error]: {e}")
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

def _handle_checkpoint_if_present(page, target_url: str, group_label: str):
    """אם פייסבוק מבקש התחברות/2FA/CAPTCHA — עוצר את כל הקבוצות, לא רק את זו הנוכחית."""
    while (_is_visible(page.locator('input[name="email"]')) or
           _is_visible(page.locator('input[name="pass"]')) or
           "checkpoint" in page.url or
           _is_visible(page.locator('iframe[title*="recaptcha"]')) or
           _is_visible(page.get_by_text("I'm not a robot"))):
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
            _safe_print(f"\n⛔ [{group_label}] Facebook is asking for a password, 2FA, or CAPTCHA security check! Pausing ALL groups.")
            input("  → Please complete it in the browser tab that was brought to front, then press ENTER here to resume all groups... ")
            _resume_event.set()
        else:
            _resume_event.wait()
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception:
            pass

def _scan_group(target_url: str, group_label: str, sheet, seen_urls, storage_state_path: str, headless: bool) -> int:
    """
    סורק קבוצה אחת. כל thread מריץ instance משלו של Playwright (ה-sync API אינו
    thread-safe כשחולקים browser/context אחד בין threads) — ההתחברות משותפת דרך
    storage_state שיוצא פעם אחת מהפרופיל הראשי, לא דרך שיתוף אובייקט ה-context.
    """
    added_count = 0
    # פיזור זמן פתיחה אקראי בין הטאבים — פחות נראה כמו בוט מאשר פתיחה סימולטנית של כולם
    time.sleep(random.uniform(0.5, 3.0))

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
            _safe_print(f"\n{'='*50}\n📄 Scanning {group_label}\n{target_url}\n{'='*50}")

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                _safe_print(f"    ⚠️ [{group_label}] Navigation interrupted by Facebook. Checking for security checkpoints...")

            page.wait_for_timeout(3000)
            _handle_checkpoint_if_present(page, target_url, group_label)
            _dismiss_popups(page)

            _safe_print(f"📜  [{group_label}] Scrolling ({SCROLL_COUNT} times)...")
            for _ in range(SCROLL_COUNT):
                page.mouse.wheel(0, 4000)
                # ג'יטר על זמן ההמתנה — קצב גלילה קבוע לחלוטין נראה יותר בוטי
                jittered_delay = max(500, SCROLL_DELAY_MS + random.randint(-400, 400))
                page.wait_for_timeout(jittered_delay)
            _safe_print(f"📜  [{group_label}] Done scrolling.")

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

            _safe_print(f"🔍  [{group_label}] Found {len(articles_data)} real posts.")

            if len(articles_data) == 0:
                _safe_print(f"    📸 [{group_label}] No posts detected. Saving debug_fb.png...")
                page.screenshot(path=f"debug_fb_{group_label.replace(' ', '_').replace('/', '-')}.png")
                return added_count

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

                with _sheet_lock:
                    already_seen = post_url in seen_urls
                if already_seen:
                    _safe_print(f"    [{group_label}] Pre-filtered: Post already exists in Google Sheets (Duplicate).")
                    continue

                excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text]
                if excluded_found:
                    _safe_print(f"    [{group_label}] Pre-filtered: Contains excluded location '{excluded_found[0]}'.")
                    continue

                # --- Pre-filter: Remove obvious non-relevant posts (Sublets, Roommates, Commercial) ---
                neg_match = re.search(NEGATIVE_KEYWORDS, text)
                if neg_match:
                    _safe_print(f"    [{group_label}] Pre-filtered: Contains negative keyword '{neg_match.group(0)}'.")
                    continue

                # --- Pre-filter: Identify Sales instead of Rentals ---
                if re.search(r'ל\s*מ\s*כ\s*י\s*ר\s*ה', text):
                    # Look for prices >= 1,000,000 in any format (commas, dots, spaces) or the word "מיליון"
                    sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
                    if sale_price_match:
                        _safe_print(f"    [{group_label}] Pre-filtered: Apartment for sale (found 'למכירה' + {sale_price_match.group(0).strip()}).")
                        continue

                # --- Pre-filter: Check for room counts explicitly before heavy LLM processing ---
                if not re.search(ROOMS_PRE_FILTER_REGEX, text):
                    clean_snip = text[:100].replace('\n', ' ')
                    actual_rooms_match = re.search(r'([1-9](?:\.5)?)\s*חד', text)
                    if actual_rooms_match:
                        found_val = actual_rooms_match.group(1)
                        _safe_print(f"    [{group_label}] Pre-filtered: Post is for {found_val} rooms (not matching target {MIN_ROOMS}-{MAX_ROOMS}).\n      ↳ URL: {post_url}\n      ↳ Text: {clean_snip}...")
                    else:
                        _safe_print(f"    [{group_label}] Pre-filtered: No mention of matching room count.\n      ↳ URL: {post_url}\n      ↳ Text: {clean_snip}...")
                    continue

                _safe_print(f"🤖 [{group_label}] Analyzing post (URL: {post_url})...")
                time.sleep(2)

                data = analyze_post_with_llm(text)
                if not data:
                    _safe_print(f"    [{group_label}] Skipped: LLM failed to parse or returned no data.")
                    continue

                rooms = data.get("rooms")
                price = data.get("price")

                try:
                    rooms_val = float(rooms) if rooms is not None else 0.0
                except (TypeError, ValueError):
                    rooms_val = 0.0

                try:
                    price_val = float(price) if price is not None else 0.0
                except (TypeError, ValueError):
                    price_val = 0.0

                # --- תיקון שגיאות והזדמנות שנייה למחיר ---
                # אם המחיר שהמודל מצא לא נמצא בתקציב שלנו, או שהוא הזוי (כמו מ"ר), נחפש בטקסט עצמו!
                if not (MIN_PRICE <= price_val <= MAX_PRICE):
                    # 1. Clean invisible BIDI characters often inserted by Facebook
                    clean_text = BIDI_RE.sub('', text)

                    # 2. Remove commas, dots, or spaces ONLY if they act as thousands separators (e.g., "5 900" -> "5900")
                    clean_text = re.sub(r'(?<=[0-9])[.,\s](?=[0-9]{3}(?![0-9]))', '', clean_text)

                    # 3. Extract standalone 4-5 digit numbers (explicit [0-9] to avoid unicode digit matching issues),
                    #    then filter by MIN_PRICE/MAX_PRICE in Python — not hardcoded in the regex, so config changes stay in sync
                    possible_prices = [int(p) for p in re.findall(r'(?<![0-9])[0-9]{4,5}(?![0-9])', clean_text)]

                    if possible_prices:
                        valid_prices = [p for p in possible_prices if MIN_PRICE <= p <= MAX_PRICE]
                        if valid_prices:
                            # מצאנו מחיר תקין בתוך הטקסט! נדרוס את הטעות של המודל.
                            price_val = float(valid_prices[0])
                        elif price_val < 3000 or price_val > 30000:
                            # לא מצאנו משהו בתקציב, אבל המחיר של המודל הזוי אז נחליף כדי שהלוג יהיה הגיוני
                            price_val = float(possible_prices[0])

                # --- הזדמנות שנייה לחדרים: אם המודל טעה אך הטקסט מכיל ספירה תואמת ---
                if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS):
                    clean_text_rooms = BIDI_RE.sub('', text)
                    room_matches = [float(r) for r in re.findall(r'([1-9](?:\.5)?)\s*חד', clean_text_rooms)]
                    valid_rooms = [r for r in room_matches if MIN_ROOMS <= r <= MAX_ROOMS]
                    if valid_rooms:
                        rooms_val = valid_rooms[0]

                if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS):
                    _safe_print(f"    [{group_label}] Skipped: Room count is not suitable ({rooms_val}).")
                    continue
                if not (MIN_PRICE <= price_val <= MAX_PRICE):
                    _safe_print(f"    [{group_label}] Skipped: Price is not suitable ({int(price_val):,} ₪).")
                    continue

                arnona = _normalize_bimonthly_fee(data.get("arnona") or "לא צוין")
                vaad = _normalize_bimonthly_fee(data.get("vaad") or "לא צוין")
                _warn_if_fee_implausible("Vaad bayit", vaad, 2400)
                _warn_if_fee_implausible("Arnona", arnona, 3000)

                address = data.get("address", "לא צוין")

                # Calculate Distance (No filtering, just display)
                dist_text, _ = get_walking_distance(address)

                new_row = [
                    post_url,
                    int(price_val),
                    rooms_val,
                    dist_text,
                    data.get("entry_date") or "לא צוין",
                    data.get("floor") or "לא צוין",
                    map_bool(data.get("elevator")),
                    data.get("parking") or "לא צוין",
                    arnona,
                    vaad,
                    map_bool(data.get("shelter")),
                    map_bool(data.get("is_agent")),
                    fb_post_date,
                    address
                ]

                with _sheet_lock:
                    try:
                        _with_retries(lambda: sheet.append_row(new_row))
                        seen_urls.add(post_url)
                        added_count += 1
                        _safe_print(f"    🌟 [{group_label}] SUCCESS! Apartment added: {rooms_val} rooms | {int(price_val):,} ₪ | {dist_text} | Address: {address}")
                    except Exception as e:
                        _safe_print(f"    ❌ [{group_label}] Error writing to sheet: {e}")
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    return added_count

def run_scraper(headless: bool = False):
    sheet, seen_urls = setup_google_sheet()

    profile_dir = os.path.join(os.getcwd(), "chrome_profile")
    storage_state_path = os.path.join(profile_dir, "_session_state.json")

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

        print("🌐 Opening Facebook...")
        page.goto("https://www.facebook.com", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if headless:
            if _is_visible(page.locator('input[name="email"]')):
                print("❌ Cannot login manually in headless mode! Please run without --headless first.")
                context.close()
                sys.exit(1)
        else:
            if _is_visible(page.locator('input[name="email"]')):
                print("\n┌─────────────────────────────────────────────┐")
                print("│  Please log in to Facebook manually in the  │")
                print("│  browser window that just opened.           │")
                print("└─────────────────────────────────────────────┘\n")

                for attempt in range(LOGIN_MAX_ATTEMPTS):
                    input("  → Press ENTER here in the terminal ONLY AFTER you have fully logged in and see your feed... ")
                    page.wait_for_timeout(2000)
                    if not _is_visible(page.locator('input[name="email"]')):
                        break
                    print("❌ Facebook login form is still visible! Please complete login first.")
                else:
                    print("❌ Login not completed after several attempts. Exiting.")
                    context.close()
                    sys.exit(1)
            else:
                print("✅ Already logged into Facebook! Skipping manual login.")

        # מייצאים cookies/session לקובץ כדי ש-threads עצמאיים יוכלו להשתמש בהם —
        # לא ניתן לשתף context/browser אחד בין threads (Playwright sync API אינו thread-safe)
        context.storage_state(path=storage_state_path)
        context.close()

    print(f"✔  Continuing to scan groups ({MAX_CONCURRENT_GROUPS} in parallel)...")

    # Shuffle the target URLs to scan groups in a random order
    shuffled_urls = random.sample(TARGET_URLS, len(TARGET_URLS))
    total = len(shuffled_urls)

    total_added = 0
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GROUPS) as executor:
        futures = {
            executor.submit(_scan_group, url, f"Group {idx}/{total}", sheet, seen_urls, storage_state_path, headless): idx
            for idx, url in enumerate(shuffled_urls, 1)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                total_added += future.result()
            except Exception as e:
                _safe_print(f"\n❌ [Group {idx}/{total}] crashed: {e}")

    print(f"\n🎉 Scraping finished successfully! {total_added} apartments added.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook Apartment Scraper Bot - Realtime")
    parser.add_argument("--headless", action="store_true", help="Run without UI")
    args = parser.parse_args()

    print("\n=======================================================")
    print("  🏠  Apartment Search Bot – Real-time updates")
    print("=======================================================")
    print(f"  Groups:      {len(TARGET_URLS)}")
    print(f"  Areas (info): {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")
    
    run_scraper(headless=args.headless)
