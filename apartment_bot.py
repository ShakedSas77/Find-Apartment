import argparse
import json
import sys
import time
import os
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
    SHEET_ID, CREDENTIALS_FILE, TARGET_URLS, LOCATIONS, 
    MIN_PRICE, MAX_PRICE, DESTINATION_ADDRESS, 
    SCROLL_COUNT, SCROLL_DELAY_MS, SESSION_FILE
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

    existing_data = sheet.get_all_values()
    seen_urls = set()

    if not existing_data:
        print("    Sheet is empty - creating header row.")
        sheet.append_row(SHEET_HEADERS)
    else:
        for row in existing_data[1:]:
            if row: 
                seen_urls.add(row[0])
        print(f"    Found {len(seen_urls)} existing apartments in the sheet. Will skip them.")

    return sheet, seen_urls

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
    prompt = f"""
קרא את מודעת הנדל"ן הבאה והוצא ממנה את הנתונים במדויק.
החזר אך ורק אובייקט JSON חוקי. אם נתון חסר, החזר null (למספרים) או "לא צוין" (למחרוזות).

- "rooms": מספר החדרים כעשרוני (למשל 3.0, 3.5. מספר בלבד).
- "price": המחיר לחודש בשקלים (מספר בלבד, ללא פסיקים).
- "arnona": עלות ארנונה (מחרוזת).
- "vaad": עלות ועד בית (מחרוזת).
- "shelter": האם יש ממ"ד או מקלט? (כן / לא / לא צוין).
- "parking": חניה בבניין או ברחוב? (מחרוזת, פרט מה שכתוב).
- "entry_date": תאריך כניסה (מחרוזת).
- "post_date": תאריך פרסום המודעה מתוך הטקסט אם רשום (מחרוזת).
- "is_agent": תיווך? (כן / ללא תיווך / לא צוין).
- "address": שם הרחוב, מספר והעיר (רק הכתובת עצמה).

הטקסט:
---
{text[:3000]}
---
"""
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
            sys.stdout.write("\n    ⏳ Gemini quota exhausted. Falling back to local Ollama... ")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"\n    ⚠ Gemini Error ({error_msg}). Falling back to local Ollama... ")
            sys.stdout.flush()

        try:
            ollama_response = ollama.chat(
                model='llama3',
                messages=[{'role': 'user', 'content': prompt}],
                format='json'
            )
            response_text = ollama_response['message']['content']
            return json.loads(response_text)
        except Exception as ollama_err:
            print(f"\n    ❌ Error in local Ollama analysis: {ollama_err}")
            return None

def get_walking_distance(address: str):
    if not address or address == "לא צוין":
        return "No street specified", 999999
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
    except Exception:
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
        has_session = Path(SESSION_FILE).exists()
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=SESSION_FILE if has_session else None)
        page = context.new_page()

        if not has_session:
            print("\n┌─────────────────────────────────────────────┐")
            print("│  Browser window will open.                  │")
            print("│  Log in to Facebook, then return here       │")
            print("│  and press ENTER.                           │")
            print("└─────────────────────────────────────────────┘\n")
            page.goto("https://www.facebook.com", wait_until="domcontentloaded")
            input("  → Press ENTER after logging in... ")
            context.storage_state(path=SESSION_FILE)
            print(f"  ✔  Session saved to {SESSION_FILE}")
        else:
            print("✔  Loaded saved session - skipping login.")

        for group_idx, target_url in enumerate(TARGET_URLS, 1):
            print(f"\n{'='*50}\n📄 Scanning group {group_idx} of {len(TARGET_URLS)}\n{target_url}\n{'='*50}")
            page.goto(target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            
            if group_idx == 1:
                _dismiss_popups(page)

            print(f"📜  Scrolling ({SCROLL_COUNT} times)...")
            for i in range(SCROLL_COUNT):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(SCROLL_DELAY_MS)
                sys.stdout.write(f"\r    Scroll {i + 1}/{SCROLL_COUNT}")
                sys.stdout.flush()
            print()

            articles = page.locator('div[role="article"]').all()
            print(f"🔍  Found {len(articles)} posts - filtering by location and sending to Gemini...")
            
            for article in articles:
                post_url = extract_post_url(article)
                if post_url in seen_urls and post_url != "Link not extracted":
                    continue

                try:
                    text = article.inner_text()
                except Exception:
                    continue
                
                if not any(loc in text for loc in LOCATIONS):
                    continue
                if "בני ברק" in text:
                    continue
                if article.locator('img').count() < 2:
                    continue

                sys.stdout.write("🤖 Analyzing post with Gemini/Ollama... ")
                sys.stdout.flush()
                # Short delay to avoid Facebook block
                time.sleep(2) 
                
                data = analyze_post_with_llm(text)
                if not data:
                    print("Failed.")
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

                if not (3.0 <= rooms_val <= 3.5):
                    print("Skipped: Room count is not suitable.")
                    continue
                if not (MIN_PRICE <= price_val <= MAX_PRICE):
                    print("Skipped: Price is not suitable (or not specified).")
                    continue

                address = data.get("address", "לא צוין")
                dist_text, dist_meters = get_walking_distance(address)

                if dist_meters > 4000:
                    print(f"Skipped: Too far ({dist_text}).")
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
                    print("✔ Relevant apartment added directly to Google Sheets!")
                except Exception as e:
                    print(f"❌ Error writing to sheet: {e}")

        browser.close()
        print("\n🎉 Scraping finished successfully!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook Apartment Scraper Bot - Realtime")
    parser.add_argument("--headless", action="store_true", help="Run without UI")
    args = parser.parse_args()
    
    if args.headless and not Path(SESSION_FILE).exists():
        print("❌ No saved session found. Run once without --headless to log in.")
        sys.exit(1)

    print("\n=======================================================")
    print("  🏠  Apartment Search Bot – Real-time updates")
    print("=======================================================")
    print(f"  Groups:      {len(TARGET_URLS)}")
    print(f"  Locations:   {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")
    
    run_scraper(headless=args.headless)
