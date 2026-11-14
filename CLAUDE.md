# Project: Facebook Apartment Scraper

Python bot that scrapes Facebook apartment listing groups, uses Gemini 2.5 Flash-Lite (Ollama qwen2.5:7b fallback) to parse Hebrew posts into structured JSON, computes walking distance via Google Distance Matrix, and appends matching listings to a Google Sheet. Target: 3–3.5 room apartments, ₪5500–6700/month, within walking distance of רחוב הדוגמה 1, Tel Aviv. All user-facing text, regex patterns, and sheet headers are Hebrew; the LLM prompt's *instructions* can be English or Hebrew (`PROMPT_LANGUAGE` in config.py), but its field-rule content, examples, and all output values are always Hebrew.

## Run Commands

```bash
# New install (Windows): venv + libraries + browser, then opens the web app on its first-run setup
install.bat
# The web app (localhost only): setup, results, map, groups, settings + add-ons, stats, tools
open_app.bat
# = streamlit run webapp/app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
# Console alternative: writes config_local.py + .env; --add <step> for one step
# (search ai results facebook telegram yad2 maps schedule)
python setup_wizard.py
# What the web app runs for Facebook: one-time login (window closes itself), and the joined groups as JSON
python apartment_bot.py --login
python apartment_bot.py --list-my-groups

# First run without the wizard — headful required for manual FB login
python apartment_bot.py --headful

# Subsequent runs — headless + live is the default (--headless/--live still
# accepted as no-ops so old scripts keep working)
python apartment_bot.py
# or
run_bot.bat

# Sync the sheet's Groups tab (group list) with the local DB: fill names/cities, refresh stats, sort by match rate
python apartment_bot.py --sync-groups

# Score the LLM's extraction of tests/fixtures/test_posts/*.txt against tests/fixtures/test_posts/expected.json
# (one real LLM call per fixture) — run before/after any prompt or model change
python apartment_bot.py --eval

# Save recent saved-search alert emails as text (read-only IMAP, no browser, no writes)
python apartment_bot.py --mail-dump

# Partial scans: only the Facebook groups, only the Yad2 alert emails (no browser), or one group from the Groups tab
python apartment_bot.py --source fb
python apartment_bot.py --source yad2
python apartment_bot.py --group https://www.facebook.com/groups/<id>

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

# Telegram listener (only relevant if TELEGRAM_ENABLED=True) — separate
# long-running process, not part of the scrape: vote taps + chat commands
# (/scan /scan_fb /scan_yad2 /scan_group /stop /status /new /groups /pause /resume /stats /doctor /sync_groups /help)
python bot_listener.py
# hidden + auto-restart, logs to logs/listener.log (Startup-folder shortcut points at the .vbs)
run_listener_hidden.vbs
```

## Architecture

Layout (reorganized 2026-11-07 — keep new modules in the matching folder):

```
apartment_bot.py bot_listener.py doctor.py setup_wizard.py backup_db.py clean_data.py   entry points (run these)
webapp/        the Streamlit web app: app.py (navigation), views.py (pages), components.py (shared sections), data.py (no Streamlit)
config.py config_local.py env.py prompts.py                            settings, secrets access, LLM prompt
core/          pipeline + shared logic: evaluate, llm, normalize, dedupe, scoring, storage, run_log, llm_eval, util, errors,
               scan_runner, local_settings, addon_setup
sources/       where listings come from: fb_scraper, browser, graphql_capture, mail_source
integrations/  external services: sheets, local_workbook, groups_sync, maps, distance_fallback, telegram_notifier
tests/         pytest suite; tests/fixtures/test_posts/ = labelled LLM fixtures
docs/archive/  past plans, kept for history
run_*.bat run_hidden.vbs                                               launchers (Task Scheduler points here — keep at root)
```

| File | Purpose |
|---|---|
| `apartment_bot.py` | CLI entrypoint only: `--stats`/`--reparse-rejected`/`--replay`/`--prune`/`--recompute-scores`/`--backfill-ratings`/`__main__` dispatch to `run_scraper()`; `--login` / `--list-my-groups` (handled before the key check, since setup runs them first) call `fb_scraper.log_in_to_facebook()` / `list_my_groups()`. As of the Phase 0-9 extraction (2026-07-26), the scraping/LLM/Sheets logic that used to all live here has moved to the modules below — see each for what it owns. |
| `sources/fb_scraper.py` | Playwright/FB-DOM-specific: checkpoint/CAPTCHA handling, dead-link pruning, post-URL canonicalization, article selectors, "See more" expansion, `_scan_group_page`/`_scan_group`, `run_scraper()` |
| `core/evaluate.py` | Source-agnostic listing pipeline: `process_candidate_listing()` (LLM parse → `_evaluate_post_data` → `_build_row` → queue), `_content_prefilter()` |
| `core/llm.py` | Gemini/Ollama dual-path orchestration: `analyze_post_with_llm()`, `ApartmentData` schema, `get_gemini_client()`, `GEMINI_EXHAUSTED`. `LLM_PROVIDER = "none"` (no-AI mode) makes no model call: it returns an empty `ApartmentData` with only `core.normalize.street_from_text()`'s street (a name after רחוב/רח'/שדרות, + number, + a named city), and `_evaluate_post_data`'s regex second chance fills price and rooms (measured 2026-11-14 on 361 stored posts: 160 of the AI's 179 matches, 2 false matches) |
| `core/scan_runner.py` | Starting/stopping/watching scans from another process (Telegram commands, web app): `start_scan(args, on_failure)`, `stop_scan()` (ours, or the scheduled one by `SCAN_PID_KEY`), `running_since()`, `fresh_progress()`, `paused_since()`, `last_finished_run()`, `run_cli()` -> (code, stdout, stderr) |
| `core/local_settings.py` | `save_settings()` (ast-located assignment rewrite in `config_local.py`, also applied to the running `config`), `save_secrets()` (`.env` lines, also `os.environ`), `is_saved()`; paths resolved at call time so tests can redirect them |
| `core/addon_setup.py` | Non-UI checks behind the AI and add-ons, shared by the wizard and the web app: Gemini key check, Ollama lookup, Telegram API call / chat discovery, listener Startup shortcut, Yad2 mailbox check (read-only), Task Scheduler `ApartmentBot HHMM` tasks (create/list/delete), service-account email, sheet id from a link |
| `webapp/` | Streamlit app, localhost only (`open_app.bat`; RTL Hebrew via injected CSS, code blocks kept LTR). Setup page until `data.setup_missing()` is empty (saved destination, an AI that needs no missing key, at least one group); then Results (filters, sort, editable tracking status + note -> `storage.listing_tracking`), Map (pydeck: listings placed from the address cache's lat/lon, colour by score, destination + walking-radius circle, click -> card), Groups (pause/remove/add incl. `--list-my-groups` picker), Settings (setup sections + every add-on via `core.addon_setup`), Stats (incl. `doctor.py --json`), Tools (scan logs from `runs.log_path`, maintenance CLI buttons). Sidebar scan panel uses `core.scan_runner`; a 5s fragment refreshes progress. Writes are disabled while a scan runs (the scan rewrites the results file at its end). Listings are cached 60s (`st.cache_data`). Tests: `tests/test_webapp.py` with `streamlit.testing.v1.AppTest` |
| `integrations/maps.py` | Distance/geocoding: `get_walking_distance()`, `_validate_address_with_geocoding()`, `_format_address_display()`, `get_gmaps_client()` |
| `integrations/sheets.py` | Results I/O: `open_output()` (listings tab + seen URLs; `open_spreadsheet()` picks the backend from `config.OUTPUT_BACKEND` at call time), `dedupe_and_sort_sheet()`, row-rewrite helpers |
| `integrations/local_workbook.py` | `OUTPUT_BACKEND = "excel"` (the public default): `LocalWorkbook`/`LocalWorksheet`, an openpyxl stand-in for the slice of gspread the bot calls, so every sheet code path writes `EXCEL_PATH` unchanged. Saves after each write; a save blocked by Excel holding the file (PermissionError) is retried, then left in memory with a WARNING and saved by the next write. Numeric strings become numbers and the formula-guard `'` is dropped under USER_ENTERED, like Sheets. `bot_listener.py` reopens it per tap (a long-held copy would save stale rows over a scan's) |
| `setup_wizard.py` | First-run setup (`install.bat` ends with it), re-runnable, `--add <step>`: search criteria (destination geocoded via Nominatim and confirmed; config.py's example address is never offered as a default), AI (Gemini key checked, or Ollama installed + model pulled), results (Excel, or Google Sheets: service-account JSON copied, sheet link parsed), Facebook (`fb_scraper.log_in_to_facebook()` or guest mode; links into the Groups tab via `groups_sync.add_groups()`), add-ons Telegram (chat id from getUpdates, test message, Startup listener shortcut), Yad2 (read-only IMAP check), Maps, schedule (`schtasks` per time). `save_settings()` rewrites only the assignments it sets in `config_local.py` (located with `ast`, multi-line ones included), `save_secrets()` only its lines in `.env` |
| `core/normalize.py` | Pure text/domain normalization: dates, floor, fees, parking, address formatting, `BIDI_RE`, `_sheet_safe_cell` |
| `core/dedupe.py` | Cross-post dedup: `_text_dedup_hash()`, `_listing_dedupe_key()` |
| `sources/browser.py` | Stealth patching, Chrome launch args, `_is_visible()`, `_dismiss_popups()` — used by `sources/fb_scraper.py` |
| `core/util.py` / `core/errors.py` | Shared low-level helpers (`_safe_print`, `_with_retries`, locks) and signal exceptions (`GmapsQuotaHalted`, `HeadlessCheckpointAbort`) — dependency-free, importable from anywhere without a cycle |
| `env.py` | `.env` access with no import-time validation (`require_env()` is called explicitly from `apartment_bot.py`'s `__main__`, not at import) — this is what makes every other module importable/testable without credentials. `missing_env()` asks only for what the enabled features need (Gemini key for `LLM_PROVIDER = "gemini"`, `SHEET_ID` + `credentials.json` for `OUTPUT_BACKEND = "sheets"`, Maps key for `GMAPS_ENABLED`, IMAP login for `ALERTS_SCAN_ENABLED`, Telegram vars for `TELEGRAM_ENABLED`) |
| `config.py` | All tunables: feature switches first (`OUTPUT_BACKEND` excel/sheets, `LLM_PROVIDER` gemini/ollama, `FACEBOOK_LOGIN`; Telegram, Yad2 emails and Google Maps default off), price/room filters, destination, negative keywords. Public defaults suit a new user; personal values go in gitignored `config_local.py` (the wizard writes it) |
| `prompts.py` | `get_apartment_prompt_improved(text)` — dispatches to `get_apartment_prompt_english()` or `get_apartment_prompt_hebrew()` per `config.PROMPT_LANGUAGE` |
| `core/storage.py` | SQLite persistence (`bot_data.db`, gitignored) — every scanned post's verdict + raw text, not just sheet matches |
| `core/scoring.py` | `compute_fit_score(fields)` — 0-100 fit score (price/distance/amenities) appended as the sheet's last column |
| `sources/mail_source.py` | Second listing source: Yad2's daily digest emails ("התראות יד2 - ריכוז מודעות יומי") from a Gmail label over **read-only** IMAP (`select(readonly=True)` + `BODY.PEEK`, never marks/moves/deletes mail). `scan_alert_emails()` runs at the start of every `run_scraper()` (before the browser; `ALERTS_SCAN_ENABLED`; a mailbox failure is only a WARNING) over the last `ALERTS_LOOKBACK_DAYS`: `parse_yad2_digest()` reads each listing (price, street, neighborhood, rooms, m²) from the text and its `yad2.co.il/realestate/item/<id>` link from the HTML (decoded out of the click-tracking redirect, never followed); the city comes from the saved search's title. A digest whose listings and links don't pair up one to one is skipped with a warning. Each listing goes through `_content_prefilter` and `process_candidate_listing(parsed=...)` — **no LLM call**, the email states the fields — so it's keyed by its Yad2 URL, deduped against Facebook listings by street/rooms/price, and alerted like a post — but written to its own **`Yad2` tab** (`YAD2_TAB_NAME`, same columns, created on the first live run via `sheets.open_listings_tab()`; dedupe/stale-prune/sort at run end like the main tab, and owed Telegram alerts are re-sent from either tab) (DB `group_url` = `YAD2_ALERTS_SOURCE`). `--mail-dump` saves raw email text to `ALERTS_DUMP_DIR` (gitignored) for when the layout changes. Replaces the removed Madlan API source (bot-protection 403) |
| `sources/graphql_capture.py` | Passive listener on the feed's own `/api/graphql/` responses (post id, message text, exact `creation_time`; no extra requests). **Observational only** (`GRAPHQL_CAPTURE_ENABLED`): `_scan_group_page` prints per group how many DOM posts it matched and whether dates agree — proving the undocumented payload parse before any post data is taken from it. Approach from FaustRen/facebook-graphql-scraper |
| `core/run_log.py` | Every scrape (any entry point) tees console output to `logs/bot_<timestamp>.log`, each line `[HH:MM:SS]`-prefixed, pruned after `LOG_RETENTION_DAYS`; counts `ERROR:`/`WARNING:` lines for the end-of-run summary and the Telegram run-finished message. `apartment_bot.py` records each run in the `runs` table (`storage.start_run`/`finish_run`: mode, status ok/checkpoint/no_groups_scanned/crashed, counts, log path) — `--stats` lists the last 10, `doctor.py`'s `last run` check FAILs after `RUN_STALE_HOURS` without a successful live run |
| `integrations/groups_sync.py` | The sheet's **Groups** tab is the source of truth for which FB groups get scanned (replaces `TARGET_URLS`, which now only seeds an empty tab). User columns: קישור לקבוצה (any link shape or bare id, canonicalized), שם הקבוצה / ערים (filled from the scanned page title only when blank), פעיל ("לא" pauses), הערות. Bot columns: posts scanned, added, match %, last added, last scan, סטטוס (תקין / לא חבר/ה בקבוצה — the scan saw FB's Join button and no posts / לא נמצאו פוסטים / ממתינה לסריקה ראשונה; `storage.set_group_status`). Adding a group needs only its link: invisible direction marks are stripped, a blank פעיל means active. Duplicates (same group under any link shape) merge into the first row — its cells win, blanks are filled from the copy — are reported and dropped on the next write. Known limit: a vanity-slug link and the numeric-id link of the same group can't be matched. `sync_groups()` runs at the start of `run_scraper()` (dry runs read-only) and after a live run's dedupe; mirrors the tab into the `groups` DB table, falling back to that mirror (then `TARGET_URLS`) when the sheet is unreachable. Standalone: `--sync-groups`, which also sorts the rows by match rate (best first, unscanned groups last; `sort_by_yield=True` — the in-run syncs keep the tab order) |
| `integrations/distance_fallback.py` | Free Nominatim geocode + calibrated haversine estimate, used whenever Google is off (the default) or over `GMAPS_MONTHLY_CAP`; tiers by match precision (house/street/neighbourhood) and gives each estimate a ± (`estimate_error_km`), shown in the sheet's `דיוק מרחק` column and the Telegram alert |
| `integrations/telegram_notifier.py` | Optional (`TELEGRAM_ENABLED`) — pushes new matches to a shared Telegram chat with ⭐/🗑 vote buttons |
| `bot_listener.py` | Separate long-running process — long-polls Telegram for vote button taps (records them, updates the tally shown on the message) and chat commands (`integrations/telegram_commands.py`) |
| `integrations/telegram_commands.py` | Chat commands from the configured chat only, fixed whitelist (`COMMANDS`), no free-form arguments, also reachable as inline buttons (`/help` menu, `cmd:<name>` callback data; `handle_button`): `/scan` `/scan_fb` `/scan_yad2` launch `apartment_bot.py` (`--source`) as a background process (refused while one runs — ours, or a Task Scheduler run seen through its `running` row under 2h old), `/scan_group` lists the active groups as buttons (`sg:<group id>` → `--group <url from the groups mirror>`), `/stop` kills the scan's process tree — ours, or the scheduled one by the pid `apartment_bot.py` records with its run id (`storage.SCAN_PID_KEY`) — and marks its run row `stopped`, `/status` (with the live progress line the scan writes to `SCAN_PROGRESS_KEY` every tick), `/new` (last 24h), `/groups` (needs-a-hand list), `/pause` `/resume` (`SCANS_PAUSED_KEY`; `run_scheduled.bat` passes `--scheduled`, which exits while paused), `/stats` (Hebrew, from storage), `/doctor` (`doctor.py --json`, Hebrew), `/sync_groups` (CLI output in `<pre>`). Commands older than 5 min (sent while the listener was down) are ignored; a button tap Telegram refuses to answer (too old) is ignored too |
| `doctor.py` | Dependency health check (env, credentials.json, Sheets, Gemini, Ollama, Telegram) — run manually or `--alert` to DM on failure. Never calls Geocoding/Distance Matrix (shares `GMAPS_MONTHLY_CAP`'s counter) |
| `backup_db.py` | Timestamped `bot_data.db` snapshot to `backups/` (gitignored), keeps newest 14. A live `run_scraper()` calls `backup_if_stale()` at the end, so a snapshot is taken automatically once the newest is older than `BACKUP_MAX_AGE_DAYS` (7); still runnable by hand |
| `clean_data.py` | Standalone: deletes `bot_data.db` and clears the sheet's data rows (keeps header). Run manually for a fresh slate. |
| `credentials.json` | Google Service Account key (gitignored) |
| `.env` | Only what enabled features need: `GEMINI_API_KEY` (gemini), `SHEET_ID` (sheets), `GMAPS_API_KEY` (maps); `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional, only if `TELEGRAM_ENABLED=True`) ; `ALERTS_IMAP_USER`/`ALERTS_IMAP_PASSWORD` (a Gmail **app password**)/`ALERTS_IMAP_FOLDER` (optional, for `sources/mail_source.py`) — all gitignored |
| `chrome_profile/` | Playwright persistent context — holds FB login session cookie |

## Data Flow

1. `open_output()` — opens the Google Sheet or `listings.xlsx`, loads seen URLs for dedupe
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
| `scraper-notes` | `sources/fb_scraper.py`, `sources/browser.py`, concurrency/scroll/stealth settings |
| `llm-notes` | `core/llm.py`, `prompts.py`, LLM text prep in `core/normalize.py` |
| `sheets-notes` | `integrations/sheets.py`, `_build_row` in `core/evaluate.py` |
| `storage-notes` | `core/storage.py`, `core/evaluate.py`, `core/dedupe.py`, `integrations/maps.py`, `apartment_bot.py` CLI flags |
| `scoring-notes` | `core/scoring.py`, entry-date normalization, `SCORE_*` settings |
| `telegram-notes` | `integrations/telegram_notifier.py`, `integrations/telegram_commands.py`, `bot_listener.py` |

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
