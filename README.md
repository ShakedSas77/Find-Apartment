# Facebook Apartment Scraper

Scrapes Facebook apartment listing groups, parses each post with an AI model, computes walking distance to a target address via Google Maps, and appends matching listings to Google Sheets.

## Current Filters

All configurable in `config.py`.

| Criterion | Current value |
|-----------|---------------|
| Rooms | 3.0 – 3.5 |
| Price | ₪5,500 – ₪6,700 |
| Max walking distance | 4 km from רחוב הדוגמה 1, Tel Aviv |
| Excluded locations | Bnei Brak, etc. |
| Disqualifying keywords | roommates, sublet, short-term, commercial |

## Google Sheets Columns

- Listing URL
- Price
- Rooms
- Walking distance
- Entry date
- Parking
- Arnona (municipal tax)
- Vaad bayit (building fee)
- Shelter / safe room
- Agent or private
- Post date
- Address
- Floor
- Elevator

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
ollama pull llama3
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
3. Visits each group URL (shuffled order), scrolls, expands "See more" buttons
4. Per post:
   - Pre-filters by excluded locations, negative keywords, sale indicators, room count
   - Parses with Gemini 2.0 Flash (`gemini-2.0-flash`) → strict JSON
   - Falls back to local Ollama `llama3` if Gemini quota is exhausted (429)
   - Secondary price fallback: regex scan if LLM price is out of range
   - Computes walking distance via Google Distance Matrix
   - Appends row to sheet only if all filters pass
