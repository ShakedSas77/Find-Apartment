# Facebook Apartment Scraper

Scrapes Facebook apartment listing groups, parses each post with an AI model, computes walking distance to a target address via Google Maps, and appends matching listings to Google Sheets.

## Current Filters

All configurable in `config.py` — see [Customizing Your Search](#customizing-your-search) below for how to change each one.

| Criterion | Current value |
|-----------|---------------|
| Rooms | 3.0 – 3.5 |
| Price | ₪5,500 – ₪6,700 |
| Walking distance | up to 4.0 km |
| Excluded locations | Bnei Brak, etc. |
| Disqualifying keywords | roommates, sublet, studio, clinic, seeker posts |

Roommate-related posts are disqualified unless the post also mentions a couple ("זוג") nearby — e.g. "מתאים לזוג או ל-2 שותפים" (suitable for a couple or 2 roommates) still passes, since that's a landlord describing tenant-type flexibility for a whole apartment, not an actual room-share offer.

Walking distance to your `DESTINATION_ADDRESS` is computed and shown in the sheet, **and is a filter**: a listing farther than `MAX_WALKING_DISTANCE_KM` is rejected — but only once a real distance has actually been computed via Google Maps. If the address was too vague to geocode confidently, or the monthly Google Maps quota (`GMAPS_MONTHLY_CAP` in `config.py`) was reached, the listing is still added with a placeholder distance rather than being wrongly rejected as "too far" (or, if `GMAPS_ON_CAP = "halt"`, the run stops entirely instead).

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

This is a from-scratch guide — nothing here comes pre-configured. You'll need: Python 3.10+, Google Chrome installed, and a Facebook account that's already a member of the groups you want scraped.

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Create a Google Sheet

Go to [sheets.google.com](https://sheets.google.com), create a new blank spreadsheet (any name). Copy its ID out of the URL — the long string between `/d/` and `/edit`:

```
https://docs.google.com/spreadsheets/d/`SHEET_ID_GOES_HERE`/edit
```

Leave it empty — `setup_google_sheet()` writes the headers itself on first run.

### 3. Google Cloud: service account + API keys

All of this happens in one place, [console.cloud.google.com](https://console.cloud.google.com/):

1. Create a new project (or reuse one).
2. **APIs & Services → Library** — enable **Distance Matrix API**.
3. **APIs & Services → Credentials → Create Credentials → Service Account** — give it any name, no roles needed. After creating it, open it → **Keys → Add Key → Create new key → JSON**. This downloads a file — rename it to `credentials.json` and put it in the project root.
4. Back in your Google Sheet from step 2, click **Share**, and paste in the service account's email address (looks like `something@your-project.iam.gserviceaccount.com` — found on the service account's page, or inside `credentials.json` as `"client_email"`). Give it **Editor** access.
5. **APIs & Services → Credentials → Create Credentials → API key** — this is your `GMAPS_API_KEY`. Click into it and restrict it to the Distance Matrix API only.
6. **Belt and suspenders — set a hard quota cap too**, so a bug can't run up a bill: **APIs & Services → Distance Matrix API → Quotas**, find **Elements per day**, edit it down to roughly **300**.

### 4. Gemini API key

Go to [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey), sign in, create a key. This is your `GEMINI_API_KEY` — free tier is generous enough for normal use.

Optional local fallback (used automatically if the Gemini quota runs out or the API errors repeatedly):
```bash
ollama pull qwen2.5:7b
```

### 5. Put it all together in `.env`

Create a file named `.env` in the project root:

```env
GMAPS_API_KEY=your_google_maps_key_from_step_3
GEMINI_API_KEY=your_gemini_key_from_step_4
SHEET_ID=your_sheet_id_from_step_2
```

The bot refuses to start if any of these three are missing. `credentials.json` and `.env` are both gitignored — never commit them.

### 6. Configure `config.py`

This file has no real defaults for your use case — at minimum, set `TARGET_URLS` (the Facebook groups to scan — open a group in your browser while logged into the scraping account, and copy the URL straight from the address bar) and `DESTINATION_ADDRESS`. See [Customizing Your Search](#customizing-your-search) below for a full field-by-field guide to every other value worth adjusting.

## Customizing Your Search

Everything below lives in `config.py`. After changing anything here, just rerun the bot — no restart-from-scratch needed. If you want a change to also apply to posts you've already scanned (not just new ones), see `--replay` under [Running](#running) further down.

**Apartment criteria**
- `MIN_ROOMS` / `MAX_ROOMS` — room-count range (decimals allowed, e.g. `3.5`).
- `MIN_PRICE` / `MAX_PRICE` — monthly rent range in ₪.
- `MAX_WALKING_DISTANCE_KM` — listings farther than this from `DESTINATION_ADDRESS` are rejected. Set to `99.0` to effectively disable this filter.
- `INCLUDE_PRICE_UNKNOWN` — a post can pass every other filter but never state a price ("contact for details"). `True` adds it to the sheet with a blank price cell; `False` (default) rejects it like any other unsuitable price.
- `MAX_POST_AGE_DAYS` — posts older than this are skipped before ever reaching the AI.

**Where and who to scan**
- `TARGET_URLS` — the list of Facebook group URLs. Add/remove entries freely; you must be a member of each group.
- `DESTINATION_ADDRESS` — where walking distance is measured to.
- `EXCLUDED_LOCATIONS` — a list of substrings; a post containing any of them is instantly disqualified (e.g. a neighboring city you don't want results from).
- `GMAPS_TARGET_CITIES` — cities Google's address validation is allowed to resolve an address to. If you're scraping a different area, add your cities here (Hebrew names, matching how Google Maps returns them for that region).
- `LOCATIONS` — cosmetic only, shown on the startup banner. Doesn't filter anything.

**Keyword filters (regex, Hebrew)** — these are the ones to edit carefully, since they're plain-text `re` patterns matched against Hebrew post text:
- `NEGATIVE_KEYWORDS` — a `|`-separated regex; any match disqualifies the post outright (default catches sublets, studios, clinics, and people *seeking* an apartment rather than offering one).
- `ROOMMATE_KEYWORDS` — matched separately from the above; disqualifies roommate/room-share posts specifically, with a built-in exception (see the note above) for landlords describing couple-or-roommate flexibility for a whole unit.
- `ROOMS_PRE_FILTER_REGEX` — the post must match this before it's even sent to the AI (cheap early exit). If you change `MIN_ROOMS`/`MAX_ROOMS` to a range this regex doesn't cover, update it too, or valid posts will get silently pre-filtered.

To add a new disqualifying word, append it to the relevant regex with a `|`, e.g. `NEGATIVE_KEYWORDS = r'סאבלט|סטודיו|קליניקה|מחפש|...|המילה_החדשה'`. Test a regex change quickly against the fixtures in `test_posts/` before running it live.

**LLM and performance**
- `GEMINI_MODEL` — which Gemini model to call. Use a `-latest` alias (e.g. `gemini-flash-lite-latest`), not a dated snapshot — see the comment in `config.py` for why.
- `PROMPT_LANGUAGE` — see above.
- `MAX_CONCURRENT_GROUPS` — how many groups scan in parallel. Higher is faster but more bot-like (separate browser fingerprint per tab); `1` is the safest fallback if you start seeing Facebook checkpoints.
- `SCROLL_COUNT` / `SCROLL_DELAY_MS` — how much of each group's feed gets loaded per run, and how long between scrolls.

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
# Print verdict counts (added/rejected_price/rejected_rooms/rejected_distance/
# prefiltered/parse_failed/price_unknown) from the local SQLite DB and this
# month's Google Maps usage, then exit
python apartment_bot.py --stats

# Re-run the LLM + filters against raw text already stored locally for posts
# previously rejected (price/rooms/distance/no-price) or that failed to parse —
# no browser opens. Useful after tweaking config.py's filters or prompts.py.
python apartment_bot.py --reparse-rejected

# Backs up the sheet to a new tab, clears it, then rebuilds it from EVERY post
# ever stored locally (any verdict) — a full re-test of a code/filter/prompt
# change against your whole history, no browser opens. Re-calls the LLM for
# every post, so it costs more than --reparse-rejected; use that instead for
# a quick check on just the rejected posts.
python apartment_bot.py --replay
```

## How It Works

1. Loads already-seen URLs from the sheet to skip duplicates
2. Opens persistent Chromium; waits for manual FB login on first run
3. Scans group URLs in parallel tabs (shuffled order, `MAX_CONCURRENT_GROUPS` at a time in `config.py`), scrolls, expands "See more" buttons. **`MAX_CONCURRENT_GROUPS = 1`** switches to a true sequential mode instead: every group is scanned one at a time, on the same page, inside the original logged-in browser profile — no separate `browser`/`context` per group and no session-state file written. Slower, but the lowest checkpoint/ban risk since it never diverges from the real profile's fingerprint. If parallel scanning starts triggering checkpoints, this is the fallback.
4. Per post:
   - Pre-filters by excluded locations, negative keywords, sale indicators, room count, and post date (posts older than `MAX_POST_AGE_DAYS` in `config.py` are skipped)
   - Parses with Gemini (configurable via `GEMINI_MODEL` in `config.py`, default `gemini-flash-lite-latest`) → strict JSON, validated against a schema on both the Gemini and Ollama paths (one retry on validation failure, then treated as a parse failure)
   - Falls back to local Ollama `qwen2.5:7b` (schema-constrained decoding, no manual JSON repair) if Gemini quota is exhausted (429), the model is misconfigured (404), or after repeated errors
   - Secondary price fallback: regex scan if LLM price is out of range
   - Computes walking distance via Google Distance Matrix (stored as a plain km number, e.g. `1.4`)
   - Appends row to sheet only if all filters pass; unknown/missing fields are left blank
5. After scanning, deduplicates cross-posted listings (same street/rooms/price, different URL — keeps the newest post date) and sorts the sheet by post date, newest first
6. Prints an end-of-run summary: groups scanned, posts seen, pre-filtered, sent to the LLM, matches added, Maps calls used this month, and checkpoints hit

Post dates are parsed from Facebook's own timestamp text (`relative_to_date` in `apartment_bot.py`) and assume an **English-locale Facebook UI** (`"5h"`, `"3 hrs"`, `"1 day"`, `"Yesterday"`, `"July 9 at 5:50 PM"`, etc.) — if your Facebook account's UI language changes, unrecognized formats pass through unchanged and log a one-time warning per run rather than failing silently.

## Local Persistence (`bot_data.db`)

Every post that gets past the URL/date pre-check is recorded in a local SQLite DB (`storage.py`, gitignored), not just the matches that land in the sheet:

- `posts` table — one row per URL, with the raw post text, parsed JSON (when the LLM ran), and a verdict (`added`, `rejected_price`, `rejected_rooms`, `rejected_distance`, `prefiltered`, `parse_failed`, `price_unknown`).
- On every run, a post already in the DB is skipped without another LLM call — **except** `parse_failed` posts, which retry automatically up to 3 attempts.
- The Google Sheet stays the source of truth for actual matches; the local DB is the memory of everything else (rejections, pre-filters, parse failures) and the raw-text archive for prompt iteration.
