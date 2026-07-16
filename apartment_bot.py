import argparse
import json
import sys
import time
import os
import re
import random
from pathlib import Path
from dotenv import load_dotenv

import ollama
import gspread
import googlemaps
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

from config import (
    CREDENTIALS_FILE, SHEET_ID, TARGET_URLS, TARGET_ROOMS,
    MIN_PRICE, MAX_PRICE, DESTINATION_ADDRESS,
    SCROLL_COUNT, SCROLL_DELAY_MS, LOCATIONS,
    MIN_ROOMS, MAX_ROOMS, ROOMS_PRE_FILTER_REGEX,
    NEGATIVE_KEYWORDS, EXCLUDED_LOCATIONS, MAX_WALKING_DISTANCE_METERS,
    SHEET_HEADERS
)

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

SHEET_HEADERS = [
    "לינק למודעה", "מחיר", "חדרים", "מרחק הליכה ל-רחוב הדוגמה 1", "תאריך כניסה", 
    "חניה", "ארנונה", "ועד", "מקלט/ממד", "תיווך", "פורסם ב", "כתובת"
]

GEMINI_EXHAUSTED = False

# ─── Helper Functions ─────────────────────────────────────────────────────────────

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
        # Check if the sheet is completely empty
        existing_data = sheet.get_all_values()
        
        if not existing_data:
            print("📝 Google Sheet is empty. Adding column headers...")
            sheet.insert_row(SHEET_HEADERS, 1)
            seen_urls = set()
        else:
            seen_urls = set()
            for row in existing_data[1:]:
                if row and row[0] and row[0] != "לינק למודעה":
                    seen_urls.add(row[0])
            
        print(f"    Found {len(seen_urls)} existing apartments in the sheet. Will skip them.")
        return sheet, seen_urls
    except Exception as e:
        print(f"    ❌ Error reading Google Sheet: {e}")
        return sheet, set()

def extract_post_url(article) -> str:
    try:
        links = article.locator('a[role="link"]').all()
        for link in links:
            href = link.get_attribute("href") or ""
            if any(seg in href for seg in ("/posts/", "/permalink/", "/marketplace/item/")):
                clean = href.split("?")[0]
                if clean.startswith("/"):
                    clean = "https://www.facebook.com" + clean
                return clean
    except Exception:
        pass
    return "Link not extracted"

def analyze_post_with_llm(text: str) -> dict | None:
    global GEMINI_EXHAUSTED
    prompt = f"""
קרא את מודעת הנדל"ן הבאה והוצא ממנה את הנתונים במדויק.
החזר אך ורק אובייקט JSON חוקי. אם נתון חסר, החזר null (למספרים) או "לא צוין" (למחרוזות).

- "rooms": מספר החדרים כעשרוני (למשל 3.0, 3.5. מספר בלבד).
- "price": שכר דירה חודשי בשקלים (מספר בלבד. חפש סכומים של אלפי שקלים, אל תתבלבל עם מ"ר!).
- "arnona": עלות ארנונה (מחרוזת).
- "vaad": עלות ועד בית (מחרוזת).
- "shelter": האם יש ממ"ד או מקלט? (כן / לא / לא צוין).
- "parking": חניה בבניין או ברחוב? (מחרוזת, פרט מה שכתוב).
- "entry_date": תאריך כניסה (מחרוזת).
- "post_date": תאריך פרסום המודעה מתוך הטקסט אם רשום (מחרוזת).
- "is_agent": תיווך? (כן / ללא תיווך / לא צוין).
- "address": שם הרחוב, מספר והעיר. (חובה לכלול עיר! אם לא רשום מפורשות, הנח שזה "רמת גן" או "גבעתיים" והוסף לכתובת).

הטקסט:
---
{text[:3000]}
---
"""
    if not GEMINI_EXHAUSTED:
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return json.loads(response.text)
        except Exception as gemini_err:
            error_msg = str(gemini_err)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                sys.stdout.write("\n    ⏳ Gemini quota exhausted. Switching permanently to Ollama... ")
                sys.stdout.flush()
                GEMINI_EXHAUSTED = True
            else:
                sys.stdout.write(f"\n    ⚠ Gemini Error ({error_msg}). Falling back to Ollama... ")
                sys.stdout.flush()
    else:
        sys.stdout.write("[Local Ollama] ")
        sys.stdout.flush()

    try:
        ollama_response = ollama.chat(
            model='llama3',
            messages=[{'role': 'user', 'content': prompt}],
            format='json'
        )
        response_text = ollama_response['message']['content']
        
        # ניקוי הטקסט: שליפת ה-JSON בלבד במקרה ש-Ollama הוסיף טקסט מיותר מסביב
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(0)
            
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
        return "Not found on Maps", 999999
    except Exception as e:
        print(f"\n    [Google Maps API Error]: {e}")
        return "Calculation error", 999999

def _dismiss_popups(page):
    for selector in [
        'div[aria-label="Close"]',
        'div[aria-label="סגירה"]',
        'button:has-text("Not Now")',
        'button:has-text("לא עכשיו")',
    ]:
        try:
            page.locator(selector).first.click(timeout=2000)
        except (PwTimeout, Exception):
            pass

# ─── Core Scraper ────────────────────────────────────────────────────────

def run_scraper(headless: bool = False):
    sheet, seen_urls = setup_google_sheet()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        print("🌐 Opening Facebook...")
        page.goto("https://www.facebook.com", wait_until="domcontentloaded")
        
        if headless:
            print("❌ Cannot login manually in headless mode! Please run without --headless.")
            browser.close()
            sys.exit(1)
            
        print("\n┌─────────────────────────────────────────────┐")
        print("│  Please log in to Facebook manually in the  │")
        print("│  browser window that just opened.           │")
        print("└─────────────────────────────────────────────┘\n")
        input("  → Press ENTER here in the terminal ONLY AFTER you have fully logged in and see your feed... ")
        print("✔  Continuing to scan groups...")

        # Shuffle the target URLs to scan groups in a random order
        shuffled_urls = random.sample(TARGET_URLS, len(TARGET_URLS))

        for group_idx, target_url in enumerate(shuffled_urls, 1):
            print(f"\n{'='*50}\n📄 Scanning group {group_idx} of {len(shuffled_urls)}\n{target_url}\n{'='*50}")
            page.goto(target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            
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
            for btn_text in ["See more", "קרא עוד", "ראה עוד"]:
                btns = page.locator(f'div[role="button"]:has-text("{btn_text}")').all()
                for btn in btns:
                    try:
                        btn.click(timeout=1000)
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
                
                post_url = extract_post_url(article)
                if post_url in seen_urls and post_url != "Link not extracted":
                    print("    Pre-filtered: Post already exists in Google Sheets (Duplicate).")
                    continue

                excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text]
                if excluded_found:
                    print(f"    Pre-filtered: Contains excluded location '{excluded_found[0]}'.")
                    continue

                # --- Pre-filter: Check for room counts explicitly before heavy LLM processing ---
                if not re.search(ROOMS_PRE_FILTER_REGEX, text):
                    clean_snip = text[:100].replace('\n', ' ')
                    print(f"    Pre-filtered: No mention of matching room count.\n      ↳ URL: {post_url}\n      ↳ Text: {clean_snip}...")
                    continue
                    
                # --- Pre-filter: Remove obvious non-relevant posts (Sublets, Roommates, Commercial) ---
                neg_match = re.search(NEGATIVE_KEYWORDS, text)
                if neg_match:
                    print(f"    Pre-filtered: Contains negative keyword '{neg_match.group(0)}'.")
                    continue

                # --- Pre-filter: Identify Sales instead of Rentals ---
                if "למכירה" in text:
                    # Look for prices > 1,000,000 or the word "מיליון"
                    sale_price_match = re.search(r'(?<!\d)[1-9](?:\d{6}|\d{0,2},\d{3},\d{3})(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
                    if sale_price_match:
                        print(f"    Pre-filtered: Apartment for sale (found 'למכירה' + {sale_price_match.group(0)}).")
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
                if not (MIN_PRICE <= price_val <= MAX_PRICE) or price_val < 3000 or price_val > 30000:
                    # 1. Clean invisible BIDI characters often inserted by Facebook
                    clean_text = re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', text)
                    
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

                if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS):
                    print(f"    Skipped: Room count is not suitable ({rooms_val}).")
                    continue
                if not (MIN_PRICE <= price_val <= MAX_PRICE):
                    print(f"    Skipped: Price is not suitable ({int(price_val):,} ₪).")
                    continue

                address = data.get("address", "לא צוין")
                
                # Validation - Distance
                dist_text, dist_meters = get_walking_distance(address)
                if dist_meters > MAX_WALKING_DISTANCE_METERS:
                    print(f"    Skipped: Too far ({dist_text} walking from {address}).")
                    continue

                new_row = [
                    post_url,
                    price,
                    data.get("rooms", "לא צוין"),
                    dist_text,
                    data.get("entry_date", "לא צוין"),
                    data.get("parking", "לא צוין"),
                    data.get("arnona", "לא צוין"),
                    data.get("vaad", "לא צוין"),
                    data.get("shelter", "לא צוין"),
                    data.get("is_agent", "לא צוין"),
                    data.get("post_date", "לא צוין"),
                    address
                ]
                
                try:
                    sheet.append_row(new_row)
                    seen_urls.add(post_url)
                    print(f"\n    🌟 SUCCESS! Apartment added: {rooms_val} rooms | {int(price_val):,} ₪ | {dist_text} | Address: {address}")
                except Exception as e:
                    print(f"\n    ❌ Error writing to sheet: {e}")

        browser.close()
        print("\n🎉 Scraping finished successfully!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook Apartment Scraper Bot - Realtime")
    parser.add_argument("--headless", action="store_true", help="Run without UI")
    args = parser.parse_args()

    print("\n=======================================================")
    print("  🏠  Apartment Search Bot – Real-time updates")
    print("=======================================================")
    print(f"  Groups:      {len(TARGET_URLS)}")
    print(f"  Locations:   {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")
    
    run_scraper(headless=args.headless)
