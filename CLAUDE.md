# Project: Facebook Apartment Scraper

Python bot that scrapes Facebook apartment listing groups, uses Gemini 2.0 Flash (Ollama qwen2.5:7b fallback) to parse Hebrew posts into structured JSON, computes walking distance via Google Distance Matrix, and appends matching listings to a Google Sheet. Target: 3–3.5 room apartments, ₪5500–6700/month, within walking distance of רחוב הדוגמה 1, Tel Aviv. All user-facing text, regex patterns, prompts, and sheet headers are Hebrew.

## Run Commands

```bash
# First run — headful required for manual FB login
python apartment_bot.py

# Subsequent runs
python apartment_bot.py --headless
# or
run_bot.bat

# Setup
pip install -r requirements.txt
playwright install chromium

# Ollama fallback (if Gemini quota hit)
ollama pull qwen2.5:7b
```

## Architecture

| File | Purpose |
|---|---|
| `apartment_bot.py` | Entry point + all scraping, LLM parsing, Sheets writing logic |
| `config.py` | All tunables: URLs, price/room filters, sheet ID, destination, negative keywords |
| `prompts.py` | Single function `get_apartment_prompt_improved(text)` — Hebrew Gemini prompt |
| `credentials.json` | Google Service Account key (gitignored) |
| `.env` | `GEMINI_API_KEY`, `GMAPS_API_KEY` (gitignored) |
| `chrome_profile/` | Playwright persistent context — holds FB login session cookie |

## Data Flow

1. `setup_google_sheet()` — loads seen URLs from sheet for dedupe
2. Playwright launches persistent Chromium; user logs into FB manually on first run
3. Per group URL (shuffled for anti-bot): scroll, expand "קרא עוד"/"See more", collect `role="article"` elements
4. Per post:
   - Strip BIDI chars → extract URL + date
   - Skip if URL already in sheet
   - Pre-filter: excluded locations, negative keywords, sale detector, room-count regex
   - LLM parse → price fallback regex (if LLM price out of range)
   - Filter by rooms + price → compute walking distance → append row to sheet

## LLM Details

- **Primary**: `gemini-2.0-flash` via `google-genai`, forced JSON via `response_mime_type="application/json"`
- **Fallback**: `ollama.chat(model='qwen2.5:7b', format='json', options={'temperature': 0})` — triggers permanently within run on Gemini 429, or after `GEMINI_MAX_CONSECUTIVE_ERRORS` non-quota errors
- **Global flag**: `GEMINI_EXHAUSTED` (module-level bool in `apartment_bot.py`)
- **Prompt**: `prompts.py` → Hebrew, strict JSON, keys: `rooms, price, arnona, vaad, shelter, parking, entry_date, floor, elevator, is_agent, address`. Post date is computed in Python from Facebook's relative timestamp (`relative_to_date`), not asked of the LLM.
- **Text cap**: 3000 chars (prompt truncates at line 29 of `prompts.py`)

## Google Sheets

- Sheet ID in `config.py:12` — must be shared with service account email from `credentials.json`
- 14 Hebrew headers defined in `config.py:67-71`
- `setup_google_sheet()` auto-seeds headers if sheet is empty
- Uses `sheet1` (first tab)
- Rows appended via `sheet.append_row(new_row)`

## Known Issues

**Stale README** (fixed in translation): README previously claimed session doesn't persist. `launch_persistent_context` is used — session does persist via `chrome_profile/`.

## Hebrew / Locale Rules

- **Never translate or reformat** `ROOMS_PRE_FILTER_REGEX`, `NEGATIVE_KEYWORDS`, `EXCLUDED_LOCATIONS` in `config.py`, or the prompt in `prompts.py`
- **BIDI strip is mandatory** before any regex: FB injects `‎‏‪–‮⁦–⁩` — stripped via the module-level `BIDI_RE` constant in `apartment_bot.py`
- `map_bool()` → "כן" / "לא" / "לא צוין" — keep as-is
- `DESTINATION_ADDRESS` is Hebrew; distance function auto-appends "רמת גן, גבעתיים, ישראל" if no local city found

## Anti-Bot / FB Fragility

- CAPTCHA/checkpoint loop in `run_scraper` requires human intervention — cannot be automated away
- URL order shuffled via `random.sample` each run
- `time.sleep(2)` per post — do not remove; removes human pacing
- 6-selector fallback chain for article extraction (lines 293–299) — FB DOM changes without warning
- `chrome_profile/` directory locks when Chrome is running — kill zombie Chrome processes before rerun

## Conventions

- English identifiers, Hebrew user-facing strings + emojis
- Wrap every Playwright interaction in `try/except` — do not simplify
- Multi-selector fallback lists preferred over single CSS/XPath locators
- New tunables belong in `config.py`, not inline in `apartment_bot.py`
- `sys.stdout.write` + `.flush()` for inline progress lines

## Sensitive Files

- `credentials.json` and `.env` are gitignored
- Verified via `git log --all -- credentials.json .env` (2026-07-17): neither file has ever been committed. History is clean.
