# Project: Facebook Apartment Scraper

Python bot that scrapes Facebook apartment listing groups, uses Gemini 2.5 Flash-Lite (Ollama qwen2.5:7b fallback) to parse Hebrew posts into structured JSON, computes walking distance via Google Distance Matrix, and appends matching listings to a Google Sheet. Target: 3–3.5 room apartments, ₪5500–6700/month, within walking distance of רחוב הדוגמה 1, Tel Aviv. All user-facing text, regex patterns, and sheet headers are Hebrew; the LLM prompt's *instructions* can be English or Hebrew (`PROMPT_LANGUAGE` in config.py), but its field-rule content, examples, and all output values are always Hebrew.

## Run Commands

```bash
# First run — headful required for manual FB login
python apartment_bot.py --headful

# Subsequent runs — headless + live is the default (--headless/--live still
# accepted as no-ops so old scripts keep working)
python apartment_bot.py
# or
run_bot.bat

# Dry run (--dry-run): classifies and prints only — no sheet writes,
# no verdict recorded for matches, no dedupe/prune. Safe way to try a
# prompt/filter/config change against real Facebook before committing.
python apartment_bot.py --dry-run

# Setup
pip install -r requirements.txt
playwright install chromium

# Ollama fallback (if Gemini quota hit)
ollama pull qwen2.5:7b

# Health check — verifies every dependency (env, credentials.json, Sheets, Gemini,
# Ollama, Telegram if enabled) without spending Google Maps/Distance Matrix quota
python doctor.py
python doctor.py --alert   # also DMs a Telegram alert if anything FAILs

# Timestamped SQLite backup of bot_data.db (the sole dedupe/verdict store) — a live
# scrape already runs this automatically once the newest backup is over 7 days old
python backup_db.py

# Telegram vote listener (only relevant if TELEGRAM_ENABLED=True) — separate
# long-running process, not part of the scrape
python bot_listener.py
```

## Architecture

| File | Purpose |
|---|---|
| `apartment_bot.py` | CLI entrypoint only: `--stats`/`--reparse-rejected`/`--replay`/`--prune`/`--recompute-scores`/`--backfill-ratings`/`__main__` dispatch to `run_scraper()`. As of the Phase 0-9 extraction (2026-07-26), the scraping/LLM/Sheets logic that used to all live here has moved to the modules below — see each for what it owns. |
| `fb_scraper.py` | Playwright/FB-DOM-specific: checkpoint/CAPTCHA handling, dead-link pruning, post-URL canonicalization, article selectors, "See more" expansion, `_scan_group_page`/`_scan_group`, `run_scraper()` |
| `core/evaluate.py` | Source-agnostic listing pipeline: `process_candidate_listing()` (LLM parse → `_evaluate_post_data` → `_build_row` → queue), `_content_prefilter()` |
| `core/llm.py` | Gemini/Ollama dual-path orchestration: `analyze_post_with_llm()`, `ApartmentData` schema, `get_gemini_client()`, `GEMINI_EXHAUSTED` |
| `maps.py` | Distance/geocoding: `get_walking_distance()`, `_validate_address_with_geocoding()`, `_format_address_display()`, `get_gmaps_client()` |
| `sheets.py` | Google Sheets I/O: `setup_google_sheet()`, `dedupe_and_sort_sheet()`, row-rewrite helpers |
| `core/normalize.py` | Pure text/domain normalization: dates, floor, fees, parking, address formatting, `BIDI_RE`, `_sheet_safe_cell` |
| `core/dedupe.py` | Cross-post dedup: `_text_dedup_hash()`, `_listing_dedupe_key()` |
| `browser.py` | Stealth patching, Chrome launch args, `_is_visible()`, `_dismiss_popups()` — shared by `fb_scraper.py` and `yad2_scraper.py` |
| `core/util.py` / `core/errors.py` | Shared low-level helpers (`_safe_print`, `_with_retries`, locks) and signal exceptions (`GmapsQuotaHalted`, `HeadlessCheckpointAbort`) — dependency-free, importable from anywhere without a cycle |
| `env.py` | `.env` access with no import-time validation (`require_env()` is called explicitly from `apartment_bot.py`'s `__main__`, not at import) — this is what makes every other module importable/testable without credentials |
| `config.py` | All tunables: URLs, price/room filters, sheet ID, destination, negative keywords |
| `prompts.py` | `get_apartment_prompt_improved(text)` — dispatches to `get_apartment_prompt_english()` or `get_apartment_prompt_hebrew()` per `config.PROMPT_LANGUAGE` |
| `storage.py` | SQLite persistence (`bot_data.db`, gitignored) — every scanned post's verdict + raw text, not just sheet matches |
| `scoring.py` | `compute_fit_score(fields)` — 0-100 fit score (price/distance/amenities) appended as the sheet's last column |
| `distance_fallback.py` | Free Nominatim geocode + calibrated haversine estimate, used only when `GMAPS_MONTHLY_CAP` is hit |
| `telegram_notifier.py` | Optional (`TELEGRAM_ENABLED`) — pushes new matches to a shared Telegram chat with ⭐/🗑 vote buttons |
| `bot_listener.py` | Separate long-running process — long-polls Telegram for vote button taps, records them, updates the tally shown on the message |
| `doctor.py` | Dependency health check (env, credentials.json, Sheets, Gemini, Ollama, Telegram) — run manually or `--alert` to DM on failure. Never calls Geocoding/Distance Matrix (shares `GMAPS_MONTHLY_CAP`'s counter) |
| `backup_db.py` | Timestamped `bot_data.db` snapshot to `backups/` (gitignored), keeps newest 14. A live `run_scraper()` calls `backup_if_stale()` at the end, so a snapshot is taken automatically once the newest is older than `BACKUP_MAX_AGE_DAYS` (7); still runnable by hand |
| `clean_data.py` | Standalone: deletes `bot_data.db` and clears the sheet's data rows (keeps header). Run manually for a fresh slate. |
| `credentials.json` | Google Service Account key (gitignored) |
| `.env` | `GEMINI_API_KEY`, `GMAPS_API_KEY`, `SHEET_ID` (required); `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional, only if `TELEGRAM_ENABLED=True`) — all gitignored |
| `chrome_profile/` | Playwright persistent context — holds FB login session cookie |

## Data Flow

1. `setup_google_sheet()` — loads seen URLs from sheet for dedupe
2. Playwright launches persistent Chromium; user logs into FB manually on first run
3. Groups are then scanned, shuffled order for anti-bot — either in parallel (`MAX_CONCURRENT_GROUPS > 1`: worker threads, each running `_scan_group()` on its own `context.new_page()` tab) or sequentially (`MAX_CONCURRENT_GROUPS == 1`: one page, inside the same login context — see the `scraper-notes` skill)
4. Per group (`_scan_group_page`): scroll, expand "קרא עוד"/"See more", collect `role="article"` elements
5. Per post:
   - Strip BIDI chars → extract URL + date
   - Skip if URL already in sheet or cached in SQLite with a non-retryable verdict
   - Pre-filter: excluded locations, negative keywords, sale detector, room-count regex, price pre-filter (`_price_prefilter_reject()` in `core/evaluate.py`: rejects without an LLM call when every marker-adjacent rent-sized price (≥ `PRICE_PREFILTER_MIN_RENT`) is outside `MIN_PRICE`–`MAX_PRICE` **and** no bare 4-5 digit number anywhere in the text is in range, so old-price/stepped-price posts still reach the LLM; recorded as `rejected_price` so `--reparse-rejected` re-checks it)
   - LLM parse → price/room-count fallback regex (if LLM value out of range)
   - Filter by rooms + price → compute walking distance → append row to sheet
6. End of run: a summary line reports groups scanned, posts seen, pre-filtered, sent to the LLM, matches added, this month's Maps calls used, and checkpoints hit; a final `Timing:` line gives the run's wall time plus cumulative per-phase seconds across group threads (`scroll`, `llm`, `maps`, `sheets`, via `core.util.timed()`) — read it before optimizing anything, since parallel groups make the phase sums exceed wall time

## Domain notes (skills)

`CLAUDE.md` holds what every session needs. Deep per-module notes live in project skills under
`.claude/skills/`, loaded on demand — **load the matching skill before editing those files**.
`tests/test_docs_integrity.py` fails if a skill isn't listed here or a listed one doesn't exist.

| skill | load before touching |
|---|---|
| `scraper-notes` | `fb_scraper.py`, `browser.py`, concurrency/scroll/stealth settings |
| `llm-notes` | `core/llm.py`, `prompts.py`, LLM text prep in `core/normalize.py` |
| `sheets-notes` | `sheets.py`, `_build_row` in `core/evaluate.py` |
| `storage-notes` | `storage.py`, `core/evaluate.py`, `core/dedupe.py`, `maps.py`, `apartment_bot.py` CLI flags |
| `scoring-notes` | `scoring.py`, entry-date normalization, `SCORE_*` settings |
| `telegram-notes` | `telegram_notifier.py`, `bot_listener.py` |

## Known Issues

**Stale README** (fixed in translation): README previously claimed session doesn't persist. `launch_persistent_context` is used — session does persist via `chrome_profile/`.

## Hebrew / Locale Rules

- **Never translate or reformat** `ROOMS_PRE_FILTER_REGEX`, `NEGATIVE_KEYWORDS`, `ROOMMATE_KEYWORDS`, `EXCLUDED_LOCATIONS` in `config.py`, or the prompt in `prompts.py`
- `ROOMMATE_KEYWORDS` (`שות[פף]`, split out of `NEGATIVE_KEYWORDS`) is checked separately from the rest of the negative-keyword list, with a negation-style exception: `_ROOMMATE_COUPLE_EXCEPTION_RE` in `core/normalize.py` suppresses the filter when "זוג" appears within ~30 chars (either order) — a landlord describing tenant-type flexibility ("מתאים לזוג או ל-2 שותפים"), not an actual room-share offer. Anchored with `(?<!מי)`/`(?!י)` to avoid false hits on `מיזוג` (A/C) and `זוגי`/`זוגית` (double bed). The two checks (roommate, then the rest of `NEGATIVE_KEYWORDS`) run independently and sequentially, so a post that's genuinely seeking a roommate but happens to mention "זוג" for an unrelated reason still gets caught by the other branch if it also matches e.g. `מחפש`
- **BIDI strip is mandatory** before any regex: FB injects `‎‏‪–‮⁦–⁩` — stripped via the module-level `BIDI_RE` constant in `core/normalize.py`
- `map_bool()` → "כן" / "לא" / "" (blank for unknown) — keep as-is. Unknown/missing values throughout the sheet are blank, not "לא צוין" text.
- `DESTINATION_ADDRESS` is Hebrew; distance function auto-appends "רמת גן, גבעתיים, ישראל" if no local city found

## Conventions

- English identifiers, Hebrew user-facing strings, English console/log output. No emojis in prints — plain text with `ERROR:`/`WARNING:`/`SUCCESS:` prefixes where relevant
- Wrap every Playwright interaction in `try/except` — do not simplify
- Multi-selector fallback lists preferred over single CSS/XPath locators
- New tunables belong in `config.py`, not inline in `apartment_bot.py`
- All output goes through `_safe_print()` (lock-protected, prefixed with `[Group N/M]`) — plain `print`/`sys.stdout.write` from within `_scan_group` will interleave garbled across threads

## Sensitive Files

- `credentials.json` and `.env` are gitignored
- Verified via `git log --all -- credentials.json .env` (2026-07-17): neither file has ever been committed. History is clean.
- `backups/` (bot_data.db snapshots from `backup_db.py`) is gitignored too — same DB contents, same reasoning.
