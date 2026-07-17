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

Walking distance to רחוב הדוגמה 1, Tel Aviv is computed and shown in the sheet for information only — it is not a filter. If the monthly Google Maps quota (`GMAPS_MONTHLY_CAP` in `config.py`) is reached, the distance column shows `מכסה חודשית הסתיימה` and the listing is still added (or, if `GMAPS_ON_CAP = "halt"`, the run stops entirely).

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
SHEET_ID=your_google_sheet_id
```

`SHEET_ID` is the long ID in your sheet's URL (`https://docs.google.com/spreadsheets/d/`**`SHEET_ID`**`/edit`). It lives in `.env`, not `config.py`, since the repo is public — the bot refuses to start without it.

For Google Sheets access:
- Create a **Service Account** in Google Cloud Console
- Download the JSON key and save it as `credentials.json` in the project root
- Share your Google Sheet with the service account email
- Enable the **Distance Matrix API** for your Google Maps key

**Belt and suspenders — set a hard cap in Google Cloud Console too.** The bot tracks its own monthly usage locally (`GMAPS_MONTHLY_CAP` in `config.py`, default 9000, safely under the free tier's ~10,000 elements/month), but that local counter can't see calls made by any other tool sharing the same key. Set an authoritative cap in the console:

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) → **APIs & Services** → **Distance Matrix API**
2. Click **Quotas** (left sidebar, under "Manage")
3. Find **Elements per day**, click the pencil/edit icon, and set it to roughly **300**
4. Save

For the Gemini fallback (Ollama):
```bash
ollama pull qwen2.5:7b
```

### 3. Configure `config.py`

Edit `config.py` to set your Facebook group URLs, target address, price/room range, excluded locations, and negative keywords.

`PROMPT_LANGUAGE` (`"en"` or `"hebrew"`) picks which of the two LLM prompt variants in `prompts.py` is used — both extract the same JSON keys and always produce Hebrew output values; only the instruction language to the model differs, for A/B testing extraction quality.

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

If Facebook throws a login/2FA/CAPTCHA checkpoint mid-run in `--headless` mode, there's no window to solve it in — that group is skipped (a `checkpoint_<group>.png` screenshot is saved, and other groups skip immediately too instead of each hanging on the same wall) and the run finishes normally with a summary line telling you to rerun without `--headless` to resolve it.

Or use the Windows launcher:

```bash
run_bot.bat
```

**Other flags:**

```bash
# Print verdict counts (added/rejected_price/rejected_rooms/prefiltered/parse_failed)
# from the local SQLite DB and this month's Google Maps usage, then exit
python apartment_bot.py --stats

# Re-run the LLM + filters against raw text already stored locally for posts
# previously rejected on price/rooms or that failed to parse — no browser opens.
# Useful for iterating on prompts.py without re-scraping Facebook.
python apartment_bot.py --reparse-rejected
```

## How It Works

1. Loads already-seen URLs from the sheet to skip duplicates
2. Opens persistent Chromium; waits for manual FB login on first run
3. Scans group URLs in parallel tabs (shuffled order, `MAX_CONCURRENT_GROUPS` at a time in `config.py`), scrolls, expands "See more" buttons. **`MAX_CONCURRENT_GROUPS = 1`** switches to a true sequential mode instead: every group is scanned one at a time, on the same page, inside the original logged-in browser profile — no separate `browser`/`context` per group and no session-state file written. Slower, but the lowest checkpoint/ban risk since it never diverges from the real profile's fingerprint. If parallel scanning starts triggering checkpoints, this is the fallback.
4. Per post:
   - Pre-filters by excluded locations, negative keywords, sale indicators, room count, and post date (posts before `RELEVANT_SINCE_DATE` in `config.py` are skipped)
   - Parses with Gemini 2.5 Flash-Lite (`gemini-2.5-flash-lite`, configurable via `GEMINI_MODEL` in `config.py`) → strict JSON, validated against a schema on both the Gemini and Ollama paths (one retry on validation failure, then treated as a parse failure)
   - Falls back to local Ollama `qwen2.5:7b` (schema-constrained decoding, no manual JSON repair) if Gemini quota is exhausted (429), the model is misconfigured (404), or after repeated errors
   - Secondary price fallback: regex scan if LLM price is out of range
   - Computes walking distance via Google Distance Matrix (stored as a plain km number, e.g. `1.4`)
   - Appends row to sheet only if all filters pass; unknown/missing fields are left blank
5. After scanning, deduplicates cross-posted listings (same street/rooms/price, different URL — keeps the newest post date) and sorts the sheet by post date, newest first
6. Prints an end-of-run summary: groups scanned, posts seen, pre-filtered, sent to the LLM, matches added, Maps calls used this month, and checkpoints hit

Post dates are parsed from Facebook's own timestamp text (`relative_to_date` in `apartment_bot.py`) and assume an **English-locale Facebook UI** (`"5h"`, `"3 hrs"`, `"1 day"`, `"Yesterday"`, `"July 9 at 5:50 PM"`, etc.) — if your Facebook account's UI language changes, unrecognized formats pass through unchanged and log a one-time warning per run rather than failing silently.

## Local Persistence (`bot_data.db`)

Every post that gets past the URL/date pre-check is recorded in a local SQLite DB (`storage.py`, gitignored), not just the matches that land in the sheet:

- `posts` table — one row per URL, with the raw post text, parsed JSON (when the LLM ran), and a verdict (`added`, `rejected_price`, `rejected_rooms`, `prefiltered`, `parse_failed`).
- On every run, a post already in the DB is skipped without another LLM call — **except** `parse_failed` posts, which retry automatically up to 3 attempts.
- The Google Sheet stays the source of truth for actual matches; the local DB is the memory of everything else (rejections, pre-filters, parse failures) and the raw-text archive for prompt iteration.
