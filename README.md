# Facebook Apartment Scraper

Scrapes Facebook apartment listing groups, parses each post with an AI model, computes walking distance to a target address, scores each match for fit, and appends matching listings to Google Sheets. Optionally pushes new matches to Telegram.

Everything runs on free tiers: Gemini's free API quota (with a local Ollama model as fallback), and a free OpenStreetMap distance estimate when Google Maps is off.

## Current Filters

All configurable in `config.py` — see [Customizing Your Search](#customizing-your-search) below for how to change each one.

| Criterion | Current value |
|-----------|---------------|
| Rooms | 2.5 – 3.5 |
| Price | ₪5,500 – ₪6,700 |
| Walking distance | up to 5.0 km |
| Entry date | no later than 60 days from today |
| Excluded locations | Bnei Brak, Holon, Petah Tikva, south Tel Aviv neighborhoods, etc. |
| Disqualifying keywords | roommates, sublet, studio, clinic, seeker posts |

Roommate-related posts are disqualified unless the post also mentions a couple ("זוג") nearby — e.g. "מתאים לזוג או ל-2 שותפים" (suitable for a couple or 2 roommates) still passes, since that's a landlord describing tenant-type flexibility for a whole apartment, not an actual room-share offer. Likewise a landlord "looking for a tenant" (מחפשים שוכר) is an offer, not a seeker post.

Walking distance to your `DESTINATION_ADDRESS` is computed and shown in the sheet, **and is a filter**: a listing farther than `MAX_WALKING_DISTANCE_KM` is rejected — but only once a distance has actually been computed. If the address was too vague to geocode confidently, the listing is still added with a blank distance rather than being wrongly rejected as "too far". See [Distance](#distance) for where the number comes from.

## Google Sheets

### Main tab (listings)

17 Hebrew columns:

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
- Agent or private (an explicit "תיווך"/"בלעדיות"/agency signal in the text, on top of the LLM's own judgment)
- Post date
- Address
- Scan timestamp (when the bot found the post)
- Fit score (0-100, see [Fit Score](#fit-score) below)
- Total monthly cost (rent + arnona/2 + vaad/2, plus the agent fee spread over 24 months for agent listings)

Headers are auto-inserted on first run if the sheet is empty. Duplicate URLs are skipped automatically.

### Groups tab (which groups to scan)

A tab named **Groups** decides which Facebook groups get scanned. To add a group, paste its link into the first column — any link shape works (`/groups/<id>`, `m.facebook.com`, a post link inside the group, or a bare id). Everything else is optional:

- **Name / cities** — filled in by the bot from the group's page title when left blank; your own text is never overwritten.
- **Active** — write "לא" to pause a group; blank means active.
- **Notes** — free text, ignored by the bot.

The bot fills the remaining columns after every live run: posts scanned, listings added, match rate, last added, last scan, and a status (OK / not a member of the group / no posts found / waiting for first scan). A group listed twice under different link shapes is merged into its first row and the copy is dropped. The tab is mirrored into the local DB, so a run still works if the sheet can't be reached.

`TARGET_URLS` in `config.py` only seeds the tab the first time it's empty.

## Setup

This is a from-scratch guide — nothing here comes pre-configured. You'll need: Python 3.10+ and a Facebook account that's already a member of the groups you want scraped. Playwright brings its own Chromium, so no separate Chrome install is needed.

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

Leave it empty — the bot writes the headers itself on first run, and creates the Groups tab from `TARGET_URLS` if it doesn't exist.

### 3. Google Cloud: service account (+ optional Maps key)

All of this happens in one place, [console.cloud.google.com](https://console.cloud.google.com/):

1. Create a new project (or reuse one).
2. **APIs & Services → Credentials → Create Credentials → Service Account** — give it any name, no roles needed. After creating it, open it → **Keys → Add Key → Create new key → JSON**. This downloads a file — rename it to `credentials.json` and put it in the project root.
3. Back in your Google Sheet from step 2, click **Share**, and paste in the service account's email address (looks like `something@your-project.iam.gserviceaccount.com` — found on the service account's page, or inside `credentials.json` as `"client_email"`). Give it **Editor** access.

Google Maps is **optional** (`GMAPS_ENABLED`, off by default — the free fallback is used instead). To turn it on:

4. **APIs & Services → Library** — enable **Distance Matrix API** and **Geocoding API**.
5. **APIs & Services → Credentials → Create Credentials → API key** — this is your `GMAPS_API_KEY`. Restrict it to those two APIs.
6. Set a hard quota cap too, so a bug can't run up a bill: **APIs & Services → Distance Matrix API → Quotas → Elements per day**, edit it down to roughly **300**.
7. Set `GMAPS_ENABLED = True` in `config_local.py`.

### 4. Gemini API key

Go to [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey), sign in, create a key. This is your `GEMINI_API_KEY`. The bot walks a chain of Gemini/Gemma models (`GEMINI_MODELS`), each with its own daily free quota.

Recommended local fallback, used once every model in the chain is out of quota:

```bash
ollama pull aya-expanse:8b
```

The local model is `OLLAMA_MODEL` in `config.py`. To compare candidates on your own hardware, run `python apartment_bot.py --eval --ollama-model <name>` for each one (see [Running](#running)).

### 5. Put it all together in `.env`

Create a file named `.env` in the project root:

```env
GEMINI_API_KEY=your_gemini_key_from_step_4
SHEET_ID=your_sheet_id_from_step_2
GMAPS_API_KEY=your_google_maps_key_from_step_3   # any placeholder if Maps stays off

# Optional — Telegram (see Telegram below)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional — saved-search alert emails (see Alert Emails below)
ALERTS_IMAP_USER=
ALERTS_IMAP_PASSWORD=     # a Gmail app password, not your account password
ALERTS_IMAP_FOLDER=
```

The bot refuses to start if `GEMINI_API_KEY`, `SHEET_ID` or `GMAPS_API_KEY` is missing. `credentials.json` and `.env` are both gitignored — never commit them.

### 6. Configure `config.py`

At minimum set `DESTINATION_ADDRESS`, and list your groups in the sheet's Groups tab (or in `TARGET_URLS` before the first run, to seed it). See [Customizing Your Search](#customizing-your-search) for every other value worth adjusting.

Since this repo is public, put personal values (your groups, your address, machine-specific settings) in `config_local.py` (gitignored) rather than `config.py`. It's loaded last and overrides anything in `config.py`.

## Customizing Your Search

Everything below lives in `config.py`. After changing anything here, just rerun the bot. To check how a change would affect posts you've already scanned, see `--replay` under [Running](#running).

**Apartment criteria**
- `MIN_ROOMS` / `MAX_ROOMS` — room-count range (decimals allowed, e.g. `3.5`).
- `MIN_PRICE` / `MAX_PRICE` — monthly rent range in ₪.
- `MAX_WALKING_DISTANCE_KM` — listings farther than this from `DESTINATION_ADDRESS` are rejected. Set to `99.0` to effectively disable this filter.
- `SCORE_ENTRY_DATE_MAX_DAYS_AHEAD` — listings whose entry date is confidently more than this many days away are rejected.
- `INCLUDE_PRICE_UNKNOWN` — a post can pass every other filter but never state a price ("contact for details"). `True` adds it to the sheet with a blank price cell; `False` (default) rejects it.
- `MAX_POST_AGE_DAYS` — posts older than this are skipped before ever reaching the AI.

**Where to look**
- The **Groups** tab in the sheet — see [Groups tab](#groups-tab-which-groups-to-scan).
- `DESTINATION_ADDRESS` — where walking distance is measured to.
- `EXCLUDED_LOCATIONS` — a list of substrings; a post containing any of them is instantly disqualified (e.g. a neighboring city you don't want results from).
- `GMAPS_TARGET_CITIES` — cities an address is allowed to resolve to. If you're scraping a different area, add your cities here (Hebrew names).
- `EXCLUDE_SOUTH_OF_LAT` — listings south of this latitude are rejected (only applied when the address geocodes confidently). Set to `None` to disable.
- `LOCATIONS` — cosmetic only, shown on the startup banner.

**Distance**
- `GMAPS_ENABLED` — `False` (default) never calls Google; distance comes from the free estimate.
- `GMAPS_MONTHLY_CAP` / `GMAPS_ON_CAP` — with Google on: the monthly call cap, and what happens over it (`"skip"` falls back to the free estimate, `"halt"` stops the run).

**Fit score & Telegram**
- `SCORE_WEIGHT_*` — fit-score component weights, must sum to 100. See [Fit Score](#fit-score).
- `AGENT_FEE_AMORTIZE_MONTHS` — months over which an agent's one-time fee is amortized into the scoring price.
- `TELEGRAM_ENABLED` — kill switch for Telegram alerts (default off). See [Telegram](#telegram-optional).

**Keyword filters (regex, Hebrew)** — edit carefully, these are plain `re` patterns matched against Hebrew post text:
- `NEGATIVE_KEYWORDS` — a `|`-separated regex; any match disqualifies the post outright (sublets, studios, clinics, people *seeking* an apartment).
- `ROOMMATE_KEYWORDS` — matched separately; disqualifies room-share posts, with the couple exception described above.
- `ROOMS_PRE_FILTER_REGEX` — the post must match this before it's even sent to the AI (cheap early exit). If you change `MIN_ROOMS`/`MAX_ROOMS` to a range this regex doesn't cover, update it too, or valid posts will get silently pre-filtered.

To add a new disqualifying word, append it to the relevant regex with a `|`, e.g. `NEGATIVE_KEYWORDS = r'סאבלט|סטודיו|...|המילה_החדשה'`. Check the effect on already-scanned posts with `--replay` before running live.

**LLM and scanning**
- `GEMINI_MODELS` — the ordered model chain, strongest first. Use rolling aliases (`gemini-flash-latest`) or named tiers, not dated snapshots — those 404 for new keys.
- `OLLAMA_MODEL` — the local fallback model.
- `PROMPT_LANGUAGE` — `"en"` or `"he"` instructions in the prompt (the field content is Hebrew either way).
- `MAX_CONCURRENT_GROUPS` — how many groups scan in parallel. Higher is faster but more bot-like; `1` is the safest fallback if you start seeing Facebook checkpoints.
- `SCROLL_COUNT` / `FIRST_SCAN_SCROLL_COUNT` — how far each group's feed is scrolled (a group's first scan goes deeper). With `GROUP_YIELD_SCALING`, groups that rarely produce matches get scrolled less.

## Running

**First run** — must be headful so you can log into Facebook manually:

```bash
python apartment_bot.py --headful
```

After you see the Facebook feed, press **Enter** in the terminal to start scraping. The login is saved in a persistent browser profile (`chrome_profile/`) for later runs.

**Subsequent runs** — headless + live is the default:

```bash
python apartment_bot.py
# or the Windows launcher
run_bot.bat
```

If the Facebook session is gone in headless mode, the run stops right away and is recorded as `login_required` — rerun with `--headful` to log in again. If Facebook throws a login/2FA/CAPTCHA checkpoint mid-run in headless mode, that group is skipped (a `checkpoint_<group>.png` screenshot is saved, and the other groups skip immediately too) and the run finishes with a summary telling you to rerun with `--headful`.

**Exit codes:** `0` OK, `1` crashed / login required / no group scanned, `2` a checkpoint skipped groups.

**Dry run (`--dry-run`):** still scrolls, calls the LLM, and computes distance (a real trial against live Facebook) but never writes to the sheet or records a match — a would-be match just prints (`DRY RUN: ... would queue`). Non-match verdicts are still cached locally, so a dry run can't cost a duplicate LLM call later.

**Other flags:**

```bash
# Verdict counts, this month's Maps usage, per-group yield, the last 10 runs,
# and posts a pre-filter rejected that look like they might be real listings
python apartment_bot.py --stats

# Sync the sheet's Groups tab with the local DB (names, cities, stats) and exit
python apartment_bot.py --sync-groups

# Score the LLM's extraction of the labelled fixtures in tests/fixtures/test_posts/
# (one real LLM call each) — run before and after any prompt or model change.
# --ollama-model NAME scores a local model instead of the Gemini chain.
python apartment_bot.py --eval
python apartment_bot.py --eval --ollama-model aya-expanse:8b

# Re-run the LLM + filters on stored text of previously rejected / unparsed posts —
# no browser. Always live: genuine matches are appended for real.
python apartment_bot.py --reparse-rejected

# Read-only: re-runs every stored post through the current filters and prints only
# the posts whose verdict would now differ. Reuses the stored LLM parse (free, fast);
# add --llm to re-parse every post too. No sheet or DB writes, no browser.
python apartment_bot.py --replay
python apartment_bot.py --replay --llm

# Rewrite every sheet row's fit score and total cost with the current formula
python apartment_bot.py --recompute-scores

# Drop sheet rows and lighten DB rows older than MAX_POST_AGE_DAYS (runs
# automatically at the end of every live scan too)
python apartment_bot.py --prune

# Save recent saved-search alert emails as text (read-only IMAP)
python apartment_bot.py --mail-dump
```

## How It Works

1. Reads the Groups tab and the URLs already in the sheet; on a live run, re-sends any Telegram alert that failed last time, and drops sheet rows whose Facebook post is gone (`PRUNE_DEAD_LINKS_ENABLED`)
2. Opens the persistent Chromium profile; waits for manual FB login on first run
3. Scans the groups in shuffled order, `MAX_CONCURRENT_GROUPS` at a time (each in its own browser that carries the logged-in profile's fingerprint). **`MAX_CONCURRENT_GROUPS = 1`** switches to a true sequential mode: every group on the same page inside the original profile — slower, but the lowest checkpoint risk.
4. Per group: sorts the feed by new posts, scrolls until it reaches posts it has already seen, expands "See more", and reads each post's body text (not the author header or comments)
5. Per post:
   - Pre-filters (no AI call): excluded locations, keywords, sale listings, room count, price, post date
   - Parses with the Gemini model chain → strict JSON validated against a schema; falls back to the local Ollama model when the whole chain is out of quota
   - Normalizes floor, fees, entry date and agent/private in Python rather than trusting the AI's free text
   - Computes walking distance (see [Distance](#distance)) and a fit score
   - Appends the row to the sheet only if every filter passes; unknown fields are left blank
   - On a live run, pushes a Telegram alert if `TELEGRAM_ENABLED`
6. After scanning: deduplicates cross-posted listings (same street/rooms/price — keeps the newest post), sorts the sheet newest first, refreshes the Groups tab stats, and backs up the DB weekly
7. Prints an end-of-run summary (groups scanned, posts seen, pre-filtered, sent to the LLM, matches added, checkpoints) and a timing line

Post dates are parsed from Facebook's own timestamp text and assume an **English-locale Facebook UI** (`"5h"`, `"3 hrs"`, `"Yesterday"`, `"July 9 at 5:50 PM"`, etc.) — an unrecognized format passes through unchanged and logs a one-time warning per run.

### Code layout

```
apartment_bot.py bot_listener.py doctor.py backup_db.py clean_data.py   entry points (run these)
config.py config_local.py env.py prompts.py                            settings, secrets access, LLM prompt
core/          pipeline + shared logic: evaluate, llm, normalize, dedupe, scoring, storage, run_log, llm_eval
sources/       where listings come from: fb_scraper, browser, graphql_capture, mail_source
integrations/  external services: sheets, groups_sync, maps, distance_fallback, telegram_notifier
tests/         pytest suite; tests/fixtures/test_posts/ = labelled LLM fixtures
```

See `CLAUDE.md` for the full breakdown.

## Fit Score

Every match gets a 0-100 fit score — informational only, doesn't affect filtering or sort order. Six weighted factors (`SCORE_WEIGHT_*` in `config.py`):

| Factor | Weight | Scores higher when |
|---|---|---|
| Price | 25 | cheaper; falls off fast near `MAX_PRICE` |
| Location | 25 | closer; north of the destination is a bonus, south/east a penalty, Givatayim preferred over Ramat Gan |
| Entry date | 25 | sooner — immediate is full marks, decays the later it gets |
| Amenities | 10 | floor/elevator condition, plus a shelter bonus |
| Rooms | 10 | closer to 3 |
| Parking | 5 | private parking only |

Agent listings have their one-time broker's fee (`AGENT_FEE_AMORTIZE_MONTHS`, default 12) amortized into the *scoring* price, so they compete with private listings on effective monthly cost rather than sticker price.

## Distance

- **Google off (default, `GMAPS_ENABLED = False`)** — the address is geocoded for free via Nominatim (OpenStreetMap), retrying common Hebrew spelling variants (e.g. `רמבם` → `רמב"ם`), and walking distance is estimated with a calibrated straight-line formula (`STRAIGHT_LINE_CALIBRATION_FACTOR`). A rough estimate, still used for the distance filter.
- **Google on** — the address is validated with the Geocoding API and the real walking distance comes from Distance Matrix, counted against `GMAPS_MONTHLY_CAP`; over the cap the free estimate takes over (or the run halts, with `GMAPS_ON_CAP = "halt"`).

Results are cached per address; a failed lookup is retried on the next run rather than cached forever.

## Telegram (optional)

Off by default (`TELEGRAM_ENABLED = False`). When enabled, set `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env`, and every live match is pushed to your chat with a 🟢/🟡/🔴 score tier, the total monthly cost, and buttons to open the post, open the address in Google Maps (when the street is known), and message the poster on WhatsApp (when the post has a mobile number — the bot itself never messages anyone). A Telegram outage never breaks a scrape: the match is already in the sheet, and the alert is re-sent on the next run (within 48 hours).

The bot also sends a run-started / run-finished summary on every live run, including the run's error and warning counts.

**Commands from the chat.** While `python bot_listener.py` is running (to start it hidden at every Windows login, put a shortcut to `wscript.exe "<repo>\run_listener_hidden.vbs"` in the Startup folder — `run_listener.bat` logs to `logs\listener.log` and restarts the listener a minute after it exits; run only one listener at a time, two pollers make Telegram reject both with 409 Conflict), the chat accepts commands (also listed in Telegram's `/` menu):

| Command | What it does |
|---|---|
| `/scan` | Starts a full live scan in the background; the run-started message with its progress bar follows. Refused while a scan is already running. |
| `/stop` | Stops a scan started with `/scan` (not one started by the Task Scheduler). |
| `/status` | Whether a scan is running, and how the last one ended. |
| `/stats` | The `--stats` output. |
| `/doctor` | The `doctor.py` health check. |
| `/sync_groups` | Syncs the Groups tab (`--sync-groups`). |
| `/help` | Lists the commands. |

Only the chat set in `TELEGRAM_CHAT_ID` is obeyed, so everyone in that chat can run them. A command sent while the listener was down is ignored once it's over 5 minutes old.

Messages sent before the vote buttons were removed still carry ⭐/🗑 buttons; `python bot_listener.py` (a separate long-running process) still handles taps on those: 2 down-votes delete the row, 2 up-votes send a 🔥 highlight, each up-vote bumps the fit score.

## Yad2 Alert Emails

A second listing source: Yad2's daily digest email for a saved search ("התראות יד2 - ריכוז מודעות יומי").

1. On Yad2, save your search with email alerts on.
2. In Gmail, make a filter that puts those emails under a label (e.g. `apartment-alerts`).
3. In `.env`, set `ALERTS_IMAP_USER` (your full Gmail address), `ALERTS_IMAP_PASSWORD` (a Gmail [app password](https://myaccount.google.com/apppasswords), not your account password) and `ALERTS_IMAP_FOLDER` (the label).

Every scan then reads the last `ALERTS_LOOKBACK_DAYS` of digests before opening the browser, and each listing goes through the same filters, distance check and Telegram alert as a Facebook post, landing in its own **Yad2** tab of the sheet (same columns, created automatically) — with no AI call, since the email already states price, rooms and street. Access is read-only: mail is never marked, moved or deleted. If the email layout ever changes, the scan prints a warning and skips that email; `--mail-dump` saves recent emails as text under `alert_dumps/` to fix the parser against.

## Other Tools

```bash
# Health check — .env, credentials.json, Sheets, Gemini, Ollama, Telegram, and whether
# a live run has succeeded in the last RUN_STALE_HOURS. Never spends Google Maps quota.
# --alert also DMs a Telegram alert if anything fails.
python doctor.py
python doctor.py --alert

# Timestamped SQLite backup of bot_data.db to backups/ (gitignored), keeps newest 14
python backup_db.py

# Wipes local state for a fresh slate: deletes bot_data.db and clears all
# data rows in the sheet (keeps the header). Run manually, not automatic.
python clean_data.py
```

## Logs and Scheduled Runs (Windows)

Every run writes its console output to `logs/bot_<timestamp>.log` (each line time-stamped, pruned after `LOG_RETENTION_DAYS`) and is recorded in the local DB's run history — `--stats` lists the last 10 runs, and `doctor.py` fails if no live run has succeeded recently, so a scheduled task that silently stopped shows up.

`run_scheduled.bat` is the entry point for Windows Task Scheduler: uses the venv interpreter, starts Ollama if it isn't already running, and passes the exit code through so Task Scheduler's "Last Run Result" reflects success or failure. `run_hidden.vbs` wraps it to run with no visible console window. The first run still has to be manual and headful to seed `chrome_profile/` with a logged-in session.

## Local Persistence (`bot_data.db`)

Every scanned post is recorded in a local SQLite DB (`core/storage.py`, gitignored), not just the matches that land in the sheet:

- `posts` table — one row per URL, with the raw post text, parsed JSON (when the LLM ran), the model that parsed it, and a verdict (`added`, `rejected_price`, `rejected_rooms`, `rejected_distance`, `rejected_location`, `rejected_entry_date`, `prefiltered`, `parse_failed`, `price_unknown`, `duplicate_listing`, `duplicate_text`).
- A post already in the DB is skipped without another LLM call — **except** `parse_failed` posts, which retry automatically up to 3 attempts.
- Also stored: the Groups tab mirror and per-group scan state, the address/distance cache, run history, and Telegram message ids.
- The Google Sheet stays the source of truth for matches; the DB is the memory of everything else and the raw-text archive for prompt iteration.
- **Retention**: at the end of every live run, sheet rows older than `MAX_POST_AGE_DAYS` (by post date) are dropped, and old DB rows lose their raw text/parsed JSON — but keep their URL and verdict forever, so a pruned post is never rescanned or re-added.
