# Facebook Apartment Scraper

Scrapes Facebook apartment listing groups, parses each post with an AI model, computes walking distance to a target address via Google Maps, and appends matching listings to Google Sheets.

## Current Filters

All configurable in `config.py`.

| Criterion | Current value |
|-----------|---------------|
| Rooms | 3.0 – 3.5 |
| Price | ₪5,500 – ₪6,700 |
| Excluded locations | Bnei Brak, etc. |
| Disqualifying keywords | roommates, sublet, short-term, commercial |

Walking distance to רחוב הדוגמה 1, Tel Aviv is computed and shown in the sheet for information only — it is not a filter.

## Google Sheets Columns

- Listing URL
- Price
- Rooms
- Walking distance (km, numeric only)
- Entry date
- Floor (integer; "קרקע"/ground floor = 0)
- Elevator
- Parking
- Arnona (municipal tax, bi-monthly, numeric only)
- Vaad bayit (building fee, bi-monthly, numeric only)
- Shelter / safe room
- Agent or private (checks for an explicit "תיווך"/agency-name signal in the post text, on top of the LLM's own judgment)
- Post date
- Address

Headers are auto-inserted on first run if the sheet is empty. Duplicate URLs are skipped automatically.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. API keys

Create `.env` in the project root:

```env
GMAPS_API_KEY=your_google_maps_key
GEMINI_API_KEY=your_gemini_key
```

For Google Sheets access:
- Create a **Service Account** in Google Cloud Console
- Download the JSON key and save it as `credentials.json` in the project root
- Share your Google Sheet with the service account email
- Enable the **Distance Matrix API** for your Google Maps key

For the Gemini fallback (Ollama):
```bash
ollama pull qwen2.5:7b
```

### 3. Configure `config.py`

Edit `config.py` to set your Facebook group URLs, target address, price/room range, excluded locations, and negative keywords.

## Running

**First run** — must be headful so you can log into Facebook manually:

```bash
python apartment_bot.py
```

After you see the Facebook feed, press **Enter** in the terminal to start scraping.

> Note: The bot uses a persistent Chrome profile (`chrome_profile/`) so your login session is saved for subsequent runs.

**Subsequent runs** — headless mode works once the profile is seeded:

```bash
python apartment_bot.py --headless
```

Or use the Windows launcher:

```bash
run_bot.bat
```

## How It Works

1. Loads already-seen URLs from the sheet to skip duplicates
2. Opens persistent Chromium; waits for manual FB login on first run
3. Scans group URLs in parallel tabs (shuffled order, `MAX_CONCURRENT_GROUPS` at a time in `config.py`), scrolls, expands "See more" buttons
4. Per post:
   - Pre-filters by excluded locations, negative keywords, sale indicators, room count, and post date (posts before `RELEVANT_SINCE_DATE` in `config.py` are skipped)
   - Parses with Gemini 2.0 Flash (`gemini-2.0-flash`) → strict JSON
   - Falls back to local Ollama `qwen2.5:7b` if Gemini quota is exhausted (429) or after repeated errors
   - Secondary price fallback: regex scan if LLM price is out of range
   - Computes walking distance via Google Distance Matrix (stored as a plain km number, e.g. `1.4`)
   - Appends row to sheet only if all filters pass; unknown/missing fields are left blank
5. After scanning, deduplicates cross-posted listings (same street/rooms/price, different URL — keeps the newest post date) and sorts the sheet by post date, newest first
