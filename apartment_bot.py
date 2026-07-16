import argparse
import json
import sys
import time
import os
import re
import random
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

from config import (
    CREDENTIALS_FILE, SHEET_ID, TARGET_URLS,
    MIN_PRICE, MAX_PRICE, DESTINATION_ADDRESS,
    SCROLL_COUNT, SCROLL_DELAY_MS, LOCATIONS,
    MIN_ROOMS, MAX_ROOMS, ROOMS_PRE_FILTER_REGEX,
    NEGATIVE_KEYWORDS, EXCLUDED_LOCATIONS,
    GEMINI_MAX_CONSECUTIVE_ERRORS, LOGIN_MAX_ATTEMPTS,
    SHEET_HEADERS
)
from prompts import get_apartment_prompt_improved

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
        # Check if the sheet is empty or missing headers
        existing_data = sheet.get_all_values()
        
        if not existing_data:
            print("📝 Missing column headers in Google Sheet. Adding them to row 1...")
            sheet.insert_row(SHEET_HEADERS, 1)
            existing_data = sheet.get_all_values()
        elif len(existing_data[0]) != len(SHEET_HEADERS) or existing_data[0][0] != "לינק למודעה":
            print("📝 Outdated column headers in Google Sheet. Updating row 1...")
            if existing_data[0] and existing_data[0][0] == "לינק למודעה":
                sheet.delete_rows(1)
            sheet.insert_row(SHEET_HEADERS, 1)
            existing_data = sheet.get_all_values()
            
        seen_urls = set()
        if len(existing_data) > 1:
            for row in existing_data[1:]:
                if row and row[0] and row[0] != "לינק למודעה":
                    seen_urls.add(row[0])
            
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
    prompt = get_apartment_prompt_improved(text)
    if not GEMINI_EXHAUSTED:
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            result = json.loads(response.text)
            GEMINI_ERROR_COUNT = 0
            return result
        except Exception as gemini_err:
            error_msg = str(gemini_err)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                sys.stdout.write("\n    ⏳ Gemini quota exhausted. Switching permanently to Ollama... ")
                sys.stdout.flush()
                GEMINI_EXHAUSTED = True
            else:
                GEMINI_ERROR_COUNT += 1
                sys.stdout.write(f"\n    ⚠ Gemini Error ({error_msg}). Falling back to Ollama... ")
                sys.stdout.flush()
                if GEMINI_ERROR_COUNT >= GEMINI_MAX_CONSECUTIVE_ERRORS:
                    sys.stdout.write(f"\n    ⏳ {GEMINI_ERROR_COUNT} consecutive Gemini errors. Switching permanently to Ollama... ")
                    sys.stdout.flush()
                    GEMINI_EXHAUSTED = True
    else:
        sys.stdout.write("[Local Ollama] ")
        sys.stdout.flush()

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

def get_walking_distance(address: str):
    if not address or address == "לא צוין" or len(address) < 3:
        return "No street specified", 999999
        
    # תוספת רשת ביטחון ל-Google Maps: הבטחת אזור החיפוש
    if not any(city in address for city in ["רמת גן", "גבעתיים", "תל אביב", "רמת-גן", "ר\"ג"]):
        address = f"{address}, רמת גן, גבעתיים, ישראל"

    try:
        result = gmaps_client.distance_matrix(
            origins=address,
            destinations=DESTINATION_ADDRESS,
            mode="walking",
        )
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

def run_scraper(headless: bool = False):
    sheet, seen_urls = setup_google_sheet()
    
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=os.path.join(os.getcwd(), "chrome_profile"),
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
                
        print("✔  Continuing to scan groups...")

        # Shuffle the target URLs to scan groups in a random order
        shuffled_urls = random.sample(TARGET_URLS, len(TARGET_URLS))

        for group_idx, target_url in enumerate(shuffled_urls, 1):
            print(f"\n{'='*50}\n📄 Scanning group {group_idx} of {len(shuffled_urls)}\n{target_url}\n{'='*50}")
            
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print("    ⚠️ Navigation interrupted by Facebook. Checking for security checkpoints...")
            
            page.wait_for_timeout(3000)
            
            # Check if Facebook is demanding a re-login, security check, or CAPTCHA
            while (_is_visible(page.locator('input[name="email"]')) or
                   _is_visible(page.locator('input[name="pass"]')) or
                   "checkpoint" in page.url or
                   _is_visible(page.locator('iframe[title*="recaptcha"]')) or
                   _is_visible(page.get_by_text("I'm not a robot"))):
                print("\n⛔ Facebook is asking for a password, 2FA, or CAPTCHA security check!")
                input("  → Please complete it in the browser, then press ENTER here to resume... ")
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
            
            _dismiss_popups(page)

            print(f"📜  Scrolling ({SCROLL_COUNT} times)...")
            for i in range(SCROLL_COUNT):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(SCROLL_DELAY_MS)
                sys.stdout.write(f"\r    Scroll {i + 1}/{SCROLL_COUNT}")
                sys.stdout.flush()
            print()

            # לחץ על "קרא עוד" כדי לחשוף את כל הטקסט של הפוסטים הארוכים
            print("📖  Expanding 'See more' buttons...")
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

            print(f"🔍  Found {len(articles_data)} real posts.")
            
            if len(articles_data) == 0:
                print("    📸 No posts detected. Saving debug_fb.png...")
                page.screenshot(path="debug_fb.png")
                continue

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
                
                if post_url in seen_urls:
                    print("    Pre-filtered: Post already exists in Google Sheets (Duplicate).")
                    continue

                excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text]
                if excluded_found:
                    print(f"    Pre-filtered: Contains excluded location '{excluded_found[0]}'.")
                    continue

                # --- Pre-filter: Remove obvious non-relevant posts (Sublets, Roommates, Commercial) ---
                neg_match = re.search(NEGATIVE_KEYWORDS, text)
                if neg_match:
                    print(f"    Pre-filtered: Contains negative keyword '{neg_match.group(0)}'.")
                    continue

                # --- Pre-filter: Identify Sales instead of Rentals ---
                if re.search(r'ל\s*מ\s*כ\s*י\s*ר\s*ה', text):
                    # Look for prices >= 1,000,000 in any format (commas, dots, spaces) or the word "מיליון"
                    sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
                    if sale_price_match:
                        print(f"    Pre-filtered: Apartment for sale (found 'למכירה' + {sale_price_match.group(0).strip()}).")
                        continue

                # --- Pre-filter: Check for room counts explicitly before heavy LLM processing ---
                if not re.search(ROOMS_PRE_FILTER_REGEX, text):
                    clean_snip = text[:100].replace('\n', ' ')
                    actual_rooms_match = re.search(r'([1-9](?:\.5)?)\s*חד', text)
                    if actual_rooms_match:
                        found_val = actual_rooms_match.group(1)
                        print(f"    Pre-filtered: Post is for {found_val} rooms (not matching target {MIN_ROOMS}-{MAX_ROOMS}).\n      ↳ URL: {post_url}\n      ↳ Text: {clean_snip}...")
                    else:
                        print(f"    Pre-filtered: No mention of matching room count.\n      ↳ URL: {post_url}\n      ↳ Text: {clean_snip}...")
                    continue

                sys.stdout.write(f"\n🤖 Analyzing post (URL: {post_url if post_url != 'Link not extracted' else 'Unknown'})... ")
                sys.stdout.flush()
                time.sleep(2) 
                
                data = analyze_post_with_llm(text)
                if not data:
                    print("\n    Skipped: LLM failed to parse or returned no data.")
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
                    
                    # 3. Extract the price using explicit [0-9] to avoid unicode digit matching issues
                    possible_prices = [int(p) for p in re.findall(r'(?<![0-9])(?:[3-9][0-9]{3}|1[0-9]{4})(?![0-9])', clean_text)]
                    
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
                    print(f"    Skipped: Room count is not suitable ({rooms_val}).")
                    continue
                if not (MIN_PRICE <= price_val <= MAX_PRICE):
                    print(f"    Skipped: Price is not suitable ({int(price_val):,} ₪).")
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
                
                try:
                    sheet.append_row(new_row)
                    seen_urls.add(post_url)
                    print(f"\n    🌟 SUCCESS! Apartment added: {rooms_val} rooms | {int(price_val):,} ₪ | {dist_text} | Address: {address}")
                except Exception as e:
                    print(f"\n    ❌ Error writing to sheet: {e}")

        context.close()
        print("\n🎉 Scraping finished successfully!")

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
