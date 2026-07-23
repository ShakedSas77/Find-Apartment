# Project: Facebook Apartment Scraper

Python bot that scrapes Facebook apartment listing groups, uses Gemini 2.5 Flash-Lite (Ollama qwen2.5:7b fallback) to parse Hebrew posts into structured JSON, computes walking distance via Google Distance Matrix, and appends matching listings to a Google Sheet. Target: 3–3.5 room apartments, ₪5500–6700/month, within walking distance of רחוב הדוגמה 1, Tel Aviv. All user-facing text, regex patterns, and sheet headers are Hebrew; the LLM prompt's *instructions* can be English or Hebrew (`PROMPT_LANGUAGE` in config.py), but its field-rule content, examples, and all output values are always Hebrew.

## Run Commands

```bash
# First run — headful required for manual FB login
python apartment_bot.py --live

# Subsequent runs
python apartment_bot.py --headless --live
# or
run_bot.bat

# Dry run (default, no --live): classifies and prints only — no sheet writes,
# no verdict recorded for matches, no dedupe/prune. Safe way to try a
# prompt/filter/config change against real Facebook before committing.
python apartment_bot.py --headless

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
| `prompts.py` | `get_apartment_prompt_improved(text)` — dispatches to `get_apartment_prompt_english()` or `get_apartment_prompt_hebrew()` per `config.PROMPT_LANGUAGE` |
| `storage.py` | SQLite persistence (`bot_data.db`, gitignored) — every scanned post's verdict + raw text, not just sheet matches |
| `clean_data.py` | Standalone: deletes `bot_data.db` and clears the sheet's data rows (keeps header). Run manually for a fresh slate. |
| `credentials.json` | Google Service Account key (gitignored) |
| `.env` | `GEMINI_API_KEY`, `GMAPS_API_KEY`, `SHEET_ID` (gitignored) |
| `chrome_profile/` | Playwright persistent context — holds FB login session cookie |

## Data Flow

1. `setup_google_sheet()` — loads seen URLs from sheet for dedupe
2. Playwright launches persistent Chromium; user logs into FB manually on first run
3. Groups are then scanned, shuffled order for anti-bot — either in parallel (`MAX_CONCURRENT_GROUPS > 1`: worker threads, each running `_scan_group()` on its own `context.new_page()` tab) or sequentially (`MAX_CONCURRENT_GROUPS == 1`: one page, inside the same login context — see Concurrency)
4. Per group (`_scan_group_page`): scroll, expand "קרא עוד"/"See more", collect `role="article"` elements
5. Per post:
   - Strip BIDI chars → extract URL + date
   - Skip if URL already in sheet or cached in SQLite with a non-retryable verdict
   - Pre-filter: excluded locations, negative keywords, sale detector, room-count regex
   - LLM parse → price/room-count fallback regex (if LLM value out of range)
   - Filter by rooms + price → compute walking distance → append row to sheet
6. End of run: a summary line reports groups scanned, posts seen, pre-filtered, sent to the LLM, matches added, this month's Maps calls used, and checkpoints hit

## Concurrency

- **`MAX_CONCURRENT_GROUPS > 1` (default, 3)**: groups scan in parallel via `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GROUPS)`. Each worker thread (`_scan_group`) launches its *own* `browser`+`context` (cookies injected from `storage_state_path`, exported once from the login phase) — multiple simultaneous browser instances from one FB account, higher checkpoint/ban risk than the real profile's own fingerprint.
- **`MAX_CONCURRENT_GROUPS == 1`**: true sequential mode. `run_scraper()` never exports `storage_state` or closes the login `context` early — it scans every group one at a time, on the same `page`, inside the original `launch_persistent_context()` session (`chrome_profile/`), with a randomized 5–15s pause between groups. Lowest checkpoint risk since nothing ever diverges from the real profile.
- Both modes share the actual per-group scanning logic via `_scan_group_page(page, target_url, group_label, sheet, seen_urls, headless, live)` — `_scan_group` (parallel) wraps it with browser/context lifecycle; the sequential loop in `run_scraper()` calls it directly. Keep this function as the single place per-post logic lives; don't duplicate it into either caller.
- Shared state guarded by module-level locks in `apartment_bot.py`: `_sheet_lock` (covers both `seen_urls` reads/writes and `sheet.append_row`, kept together to avoid duplicate-URL races), `_gemini_lock` (guards `GEMINI_EXHAUSTED`/`GEMINI_ERROR_COUNT`), `_gmaps_cap_lock` (guards the one-time over-cap notice print), `_print_lock` (via `_safe_print`, prevents interleaved terminal output). `storage.py` has its own internal lock for all SQLite access (both `posts` and `api_usage`), so the Maps-usage counter is safe to increment from every group thread.
- Checkpoint/CAPTCHA on any tab (headful mode): `_checkpoint_lock` + `_resume_event` elect one thread as "leader" to prompt once; other threads block on the event, then all retry navigation once resolved (`_handle_checkpoint_if_present`). Headless mode instead uses `_checkpoint_lock` to guard `_headless_checkpoint_hit`, a one-shot flag that makes every other group abort immediately (see Anti-Bot section); the sequential loop also breaks out early on a headless checkpoint, marking the remaining un-scanned groups as checkpoint-skipped too.
- Raising `MAX_CONCURRENT_GROUPS` speeds up a run but increases simultaneous requests from one FB account — real risk of triggering a checkpoint faster; `1` is the safe fallback

## LLM Details

- **Primary**: `gemini-flash-lite-latest` (config: `GEMINI_MODEL` in `config.py`) via `google-genai`, forced JSON via `response_mime_type="application/json"`. `gemini-2.0-flash` was retired 2026-06-01, and as of 2026-07-18 dated-snapshot names (`gemini-2.5-flash`, `gemini-2.5-flash-lite`) 404 for this key ("no longer available to new users") even though `client.models.list()` still lists them — only the rolling `-latest` aliases (`gemini-flash-latest`, `gemini-flash-lite-latest`) actually respond. Use a `-latest` alias here going forward, not a dated snapshot. A 404/NOT_FOUND response from Gemini triggers a permanent switch to Ollama without counting toward `GEMINI_MAX_CONSECUTIVE_ERRORS`.
- **Fallback**: `ollama.chat(model='qwen2.5:7b', format=ApartmentData.model_json_schema(), options={'temperature': 0, 'num_ctx': 4096}, keep_alive='10m')` — schema-constrained decoding, no manual JSON repair. Triggers permanently within run on Gemini 429/404, or after `GEMINI_MAX_CONSECUTIVE_ERRORS` non-quota errors
- **Global flag**: `GEMINI_EXHAUSTED` (module-level bool in `apartment_bot.py`)
- **Validation**: both paths' raw output is validated against the `ApartmentData` Pydantic model in `analyze_post_with_llm()` — on failure, one retry (fresh LLM call), then `None` (verdict `parse_failed`)
- **Prompt**: `prompts.py` → strict JSON, keys: `rooms, price, arnona, vaad, shelter, parking, entry_date, floor, elevator, is_agent, address`. Post date is computed in Python from Facebook's relative timestamp (`relative_to_date`), not asked of the LLM. Assumes an English-locale FB UI: handles both short (`6h`, `1d`, `3w`) and long (`3 hrs`, `1 day`, `2 wks`) relative forms plus `Yesterday` and absolute dates (`July 9 at 5:50 PM`, via `_parse_absolute_fb_date`). An unrecognized format passes through unchanged (never crashes) and logs one `_safe_print` warning per run (`_unparsed_date_logged` flag) rather than degrading silently — if that warning starts appearing, the FB account's UI locale likely changed.
- **Text cap**: 3000 chars (prompt truncates the input text, not the instructions)
- **Comment-section stripping**: `article.inner_text()` also captures the comments thread below a post. `_strip_comment_section()` (apartment_bot.py, right after the `BIDI_RE` strip) cuts the text at the first comment-section marker (`View more comments`, `View N repl`, `Write a comment`, etc.) before it's stored or analyzed, so a commenter's number/price can't contaminate the LLM's extraction of the listing itself. Trade-off: a price that exists *only* in a reply is lost — deliberate, since third-party comment numbers aren't reliable anyway.
- **Sale-listing pre-filter is price-based, not keyword-based**: many agent posts never say "למכירה" and just state a 7-digit price ("מחיר: 3,395,000 ₪"). The pre-filter in `_scan_group_page` matches a 7+-digit price (or "X מיליון") on its own — no monthly rental costs that much, so the word isn't required. The `[.,]\d{3}` separator requirement keeps it from matching phone numbers.
- **Test fixtures**: `test_posts/*.txt` — 8 fabricated Hebrew posts covering full/partial fields, agent vs. private, street vs. landmark vs. no address, an old-price distractor, and roommate/couple-flexibility edge cases; used to sanity-check both prompt variants against the Ollama path

## Google Sheets

- `SHEET_ID` lives in `.env` (not `config.py` — repo is public), loaded via `os.getenv` in `apartment_bot.py` alongside `GEMINI_API_KEY`/`GMAPS_API_KEY`; the bot exits at startup if any of the three are missing. Sheet must be shared with the service account email from `credentials.json`
- 15 Hebrew headers defined in `config.py` (`SHEET_HEADERS`) — the 14 data columns plus a trailing `זמן סריקה` (scan timestamp)
- `setup_google_sheet()` auto-seeds headers if sheet is empty
- Uses `sheet1` (first tab)
- No conditional formatting/colors/filters/frozen row are applied — plain sheet, as-is. (A `format_google_sheet()` step existed briefly and was removed; don't re-add it without being asked.)
- Rows appended via `sheet.append_row(new_row)` during scanning
- After each run, `dedupe_and_sort_sheet()` rewrites the whole data range: collapses cross-posted duplicates (same normalized street + rooms + price, different URL — keeps the newest post date) and sorts by post date, newest first. Callable standalone against an existing sheet, not just at the end of a scrape.
- Walking distance is stored as a plain km number (e.g. `"1.4"`), not the full Google-provided text — computed from `dist_meters` in `get_walking_distance()`, not string-parsed
- Posts older than `MAX_POST_AGE_DAYS` (config.py) are pre-filtered before LLM analysis
- Floor (`_parse_floor`), arnona/vaad (`_normalize_bimonthly_fee`), and agent/private (`_detect_agent`) are all normalized in Python after the LLM call, not trusted as free text: floor becomes an integer ("קרקע" = 0, explicit digit wins over an incidental "קרקע" mention), fees become bi-monthly integers with no currency/unit text, and agent detection adds a deterministic "תיווך"/"נדל\"ן" text-signal check on top of the LLM's `is_agent` judgment (explicit negations like "ללא תיווך" are excluded from triggering it)
- **Formula-injection guard** (`_sheet_safe_cell()`, security audit 2026-07-18): rows are written with `value_input_option="USER_ENTERED"` (needed for price/rooms to land as real numbers, not text), which also lets Sheets read a cell starting with `=`/`+`/`-`/`@` as a formula. Row content ultimately comes from scraped Facebook text via an LLM, so `_build_row()` runs every field through `_sheet_safe_cell()`, which prefixes such a string with a literal `'`. This is deliberate defense-in-depth — every current field already happens to be safe by construction (fixed value sets, numeric, or Hebrew-only text that excludes those characters) but that was incidental, not designed, before this guard existed.

## Local Persistence (SQLite)

- `storage.py` owns `bot_data.db` (gitignored), guarded by a single module-level `threading.Lock` — every scanned post is recorded here, not just sheet matches. The sheet stays the source of truth for actual matches; SQLite is the memory of everything else plus a raw-text archive for prompt iteration.
- `posts` table: `url` (PK), `group_url`, `raw_text` (post-BIDI-strip, post-comment-strip), `parsed_json` (LLM output, when it ran), `verdict`, `attempts`, `first_seen`, `last_processed`. Verdicts: `added`, `rejected_price`, `rejected_rooms`, `rejected_distance`, `prefiltered`, `parse_failed`, `price_unknown`.
- **`price_unknown` verdict**: when both the LLM and the price "second-chance" regex genuinely find no price at all (not an out-of-range value — a real number the LLM returns, even a bad one like 50000, still goes through `rejected_price`), `_evaluate_post_data()` treats it as "לפרטים בפרטי", not a rejection outright. `config.INCLUDE_PRICE_UNKNOWN` (default `False`, per user preference — no-price posts aren't useful leads) controls the outcome: `True` → the post is added to the sheet with a blank price cell; `False` → recorded as `price_unknown` and skipped, same as a normal rejection but with an honest label (and still picked up later by `--reparse-rejected` if you flip the flag).
- `api_usage` table (`month`, `gmaps_calls`) backs the Google Maps quota guard in `get_walking_distance()`: before every `distance_matrix` call it checks `storage.get_gmaps_usage()` against `GMAPS_MONTHLY_CAP` (config.py, default 9000) and increments via `storage.increment_gmaps_usage()` right before the call (1 element per call). Over cap: `GMAPS_ON_CAP = "skip"` (default) returns a placeholder string and keeps scanning; `"halt"` raises `GmapsQuotaHalted`, caught in `_scan_group` to stop that group's remaining posts cleanly (other groups stop independently on their next distance check, since the counter is shared). Quota status prints once at the start of `run_scraper()`, plus a warning at ≥80% of cap.
- **`_redact_api_key()`** (security audit 2026-07-18): `googlemaps` sends `GMAPS_API_KEY` as a URL query param (Gemini instead uses a header, so it's unaffected) — on a network-level failure the underlying `requests`/urllib3 exception's `str()` embeds the full request URL, key included. Both `except` blocks around `gmaps_client` calls (`_validate_address_with_geocoding`, `get_walking_distance`) run the exception through `_redact_api_key()` before printing, so a transient network error can't leak the live key into console output or a log file.
- `_scan_group_page` calls `storage.should_skip(url)` immediately after `extract_post_info`, before the seen-in-sheet check: any cached verdict except `parse_failed` skips the post with zero LLM calls. `parse_failed` retries automatically, capped at `storage.MAX_PARSE_ATTEMPTS` (3).
- `_evaluate_post_data()` (rooms/price threshold checks + regex second-chance + field normalization) and `_build_row()` (sheet row assembly, calls `get_walking_distance`) are shared between `_scan_group_page` and `reparse_rejected_posts()` — keep both call sites in sync if this logic changes.
- **Second-chance rooms/price override is gated on the LLM value being missing/non-numeric** — if the LLM already returned a real (if out-of-range) number, `_evaluate_post_data()` trusts it and does *not* let a regex match elsewhere in the text override it (e.g. "היה 6,500 עכשיו 7,200 ש"ח" — the LLM's correct 7200 must not get replaced by the nearby old-price 6500). `_PRICE_CONTEXT_RE` finds candidates within ~25 non-digit characters of a price marker (`₪`/`ש"ח`/`שכ"ד`/`שכר דירה`/`מחיר`/`לחודש`) — the `\D{0,25}` gap can't cross another digit sequence, so a marker attached to one number can't bleed onto a different nearby number. The "replace an absurd LLM value" branch (price `<3000` or `>30000`) stays ungated by design — see `test_posts/post_6_old_price_distractor.txt` for the regression case.
- **Dry-run by default**: `run_scraper()` takes `live: bool = False`. A dry run (no `--live`) still does everything read-side — scrolls, LLM calls, geocoding/distance calls (and their quota tracking) — so it's a real trial against live Facebook, not a no-op. It just never commits: a would-be match is printed (`DRY RUN: ... would queue`) but not appended to the sheet and not recorded as `VERDICT_ADDED`, so a dry run can never poison `should_skip()`'s cache and block a real future match. Non-match verdicts (`prefiltered`/`rejected_*`/`parse_failed`) are still recorded regardless of `live` — that's just avoiding repeat LLM cost on posts already known not to match, not a "commit". The end-of-run `dedupe_and_sort_sheet()`/`prune_old_posts()` pair is also skipped on a dry run. **The scheduled-task scripts (`run_bot.bat`, `run_scheduled.bat`) pass `--live` explicitly** — don't remove it there, or the scheduled bot silently stops writing.
- CLI flags: `--stats` prints verdict counts + this month's Maps usage and exits; `--reparse-rejected` re-runs the LLM + filters against stored raw text for `rejected_price`/`rejected_rooms`/`rejected_distance`/`parse_failed`/`price_unknown` posts with **no Playwright/browser involved** (still uses the real Sheets API client, so genuine matches found on reparse are appended for real — always live, no dry-run gate on this flag; skips rows whose `raw_text` was already pruned, see below); `--replay` is the "I changed the filters/prompt/normalization, test it without re-scraping" workflow — **read-only**: it re-runs **every** post ever stored (`storage.get_all_posts()`, any verdict) through the current code end to end — content pre-filters (`_replay_text_prefilters()`, kept in sync with `_scan_group_page`'s equivalent block), a real LLM call, `_evaluate_post_data`, and the distance check — and prints only the posts whose verdict would now differ from what's stored. No sheet writes, no DB writes (so it can't clobber a hand-edited sheet row or the DB's real cached verdict with a test run's result), no browser, so no checkpoint risk — but it does re-spend one LLM call per stored post. To actually commit anything `--replay` surfaces as newly-matching, re-run the real scraper with `--live`. Known limitation: it doesn't re-apply the `MAX_POST_AGE_DAYS` cutoff, since the stored post date is a frozen relative string (e.g. "3 days ago") that goes stale the moment real time passes; `--prune` runs the retention cleanup below standalone, no browser.
- **Data retention** (bounds unbounded growth over months of daily scans, verified 2026-07-18): `storage.prune_old_posts(MAX_POST_AGE_DAYS)` nulls `raw_text`/`parsed_json` for `posts` rows older than `MAX_POST_AGE_DAYS` (by `first_seen`) — the row itself (`url`, `verdict`, `attempts`, analysis columns) is kept forever, since `should_skip()` only ever reads `verdict`/`attempts`, never the raw text. This is the load-bearing guarantee: a pruned-old post can never be rescanned or re-added, only its bulky archived text is dropped. `VACUUM`s the file only when something was actually pruned, to skip a full-file rewrite on days nothing crossed the threshold. `dedupe_and_sort_sheet()` was extended the same way on the sheet side: in its existing rewrite pass, it now also drops rows whose post date is older than `MAX_POST_AGE_DAYS` (rows with an unparseable/missing date are kept, same caution rule already applied to weak addresses) — safe to prune freely since the sheet is just the "recent/relevant" view and the DB row is the permanent memory. Both run automatically at the end of every `run_scraper()` call; `--prune` runs them standalone.

## Known Issues

**Stale README** (fixed in translation): README previously claimed session doesn't persist. `launch_persistent_context` is used — session does persist via `chrome_profile/`.

## Hebrew / Locale Rules

- **Never translate or reformat** `ROOMS_PRE_FILTER_REGEX`, `NEGATIVE_KEYWORDS`, `ROOMMATE_KEYWORDS`, `EXCLUDED_LOCATIONS` in `config.py`, or the prompt in `prompts.py`
- `ROOMMATE_KEYWORDS` (`שות[פף]`, split out of `NEGATIVE_KEYWORDS`) is checked separately from the rest of the negative-keyword list, with a negation-style exception: `_ROOMMATE_COUPLE_EXCEPTION_RE` in `apartment_bot.py` suppresses the filter when "זוג" appears within ~30 chars (either order) — a landlord describing tenant-type flexibility ("מתאים לזוג או ל-2 שותפים"), not an actual room-share offer. Anchored with `(?<!מי)`/`(?!י)` to avoid false hits on `מיזוג` (A/C) and `זוגי`/`זוגית` (double bed). The two checks (roommate, then the rest of `NEGATIVE_KEYWORDS`) run independently and sequentially, so a post that's genuinely seeking a roommate but happens to mention "זוג" for an unrelated reason still gets caught by the other branch if it also matches e.g. `מחפש`
- **BIDI strip is mandatory** before any regex: FB injects `‎‏‪–‮⁦–⁩` — stripped via the module-level `BIDI_RE` constant in `apartment_bot.py`
- `map_bool()` → "כן" / "לא" / "" (blank for unknown) — keep as-is. Unknown/missing values throughout the sheet are blank, not "לא צוין" text.
- `DESTINATION_ADDRESS` is Hebrew; distance function auto-appends "רמת גן, גבעתיים, ישראל" if no local city found

## Anti-Bot / FB Fragility

- CAPTCHA/checkpoint loop (`_handle_checkpoint_if_present`) requires human intervention in headful mode — cannot be automated away; pauses all parallel tabs, not just the one that hit it. In `--headless` mode there's no window to solve it in, so instead: a screenshot (`checkpoint_<group_label>.png`) is saved, `_headless_checkpoint_hit` is flagged so other groups skip immediately instead of each hitting the same wall, and `_scan_group_page` returns `{"checkpoint_hit": True, ...}` for that group via `HeadlessCheckpointAbort` — the run finishes normally and prints "N group(s) skipped due to a security checkpoint — rerun headful" instead of hanging forever on `input()`
- Both Chromium launches (`_scan_group`'s per-thread browser and `run_scraper`'s persistent login context) pass `--autoplay-policy=user-gesture-required` — Chrome autoplays muted videos by default, and Facebook's feed has video posts that would otherwise start playing on scroll, wasting bandwidth/CPU during a scan
- Both launches use Playwright's bundled Chromium (no `channel="chrome"`) — cross-platform (Windows/Linux/ARM64), no separate real-Chrome install step. `_CHROME_LAUNCH_ARGS` (apartment_bot.py, near `BIDI_RE`) adds `--no-sandbox --disable-gpu` on Linux only (`sys.platform.startswith("linux")`): verified 2026-07-18 that without them, Chrome's sandbox can fail to initialize in some Linux VM environments, hanging `page.goto()` past its timeout on every navigation rather than erroring — a bare `google-chrome --no-sandbox --disable-gpu` CLI call loaded facebook.com in 11s on the same box where Playwright's launch (missing these flags) hung indefinitely. Windows keeps the original anti-detection args unchanged.
- **Both contexts use an explicit `viewport={"width": 1366, "height": 1600}`, not `no_viewport=True`.** Verified 2026-07-18: in headless mode, `no_viewport=True` leaves Chromium at its tiny default window (~784×505), which collapses Facebook's layout and breaks scroll almost immediately — `window.scrollY` gets stuck after one wheel tick and the group feed never grows past the ~4 posts that load on initial paint. This was the root cause of suspiciously low per-group post counts (avg ~5-6/group despite `SCROLL_COUNT=20`). Headful runs never showed it because a real on-screen window is already large. Don't revert to `no_viewport=True`.
- URL order shuffled via `random.sample` each run
- Per-post pacing comes from the Gemini rate lock (4s minimum between LLM calls) plus the jittered scroll delays — there is no per-post `time.sleep` anymore
- Scroll distance jittered too, not just the delay: `page.mouse.wheel(0, random.randint(3000, 5000))` — a fixed pixel distance every scroll is as bot-like a signal as a fixed delay. The scroll loop also does an occasional (~25% of iterations) `page.mouse.move(..., steps=5-15)` cursor drift — a human's mouse doesn't sit frozen while reading a feed
- **Stealth patches** (added 2026-07-18, inspired by hyuwowo/fb-marketplace-scraper): `_apply_stealth(page)` runs `tf-playwright-stealth`'s `stealth_sync` on every page (both launch sites), before first navigation. Kill switch: `STEALTH_ENABLED` in config.py. Two patches are deliberately disabled via `StealthConfig(navigator_user_agent=False, navigator_languages=False)` — the package fakes them with a hardcoded Chrome 95 UA and en-US (verified against v1.2.0), which would be a louder bot signal in 2026 than what it hides and would stomp the real-profile fingerprint below. If the package is missing, the bot warns once and runs without it.
- **Fingerprint consistency**: parallel workers get the login profile's real UA (`navigator.userAgent`, captured before `storage_state` export, "HeadlessChrome" stripped) and `navigator.language` passed into `new_context(user_agent=..., locale=...)` — otherwise the same account presents Playwright's default fingerprint on worker "devices" alongside the real one. Headless sequential mode: `launch_persistent_context` gets a corrected UA from a throwaway probe launch, since headless Chromium's UA literally says "HeadlessChrome" (HTTP header + JS) and a UA can only be set at context creation. `browserforge`-style *random* fingerprints were considered and rejected — a random identity per session diverges from the account's known device, more suspicious for a logged-in single-account scraper, not less.
- 7-selector fallback chain for article extraction in `_scan_group_page` — FB DOM changes without warning (last resort: `div[aria-labelledby][aria-describedby]`, FB post containers carry both even when `role="article"` is absent)
- **Post-URL canonicalization** (`_canonical_post_url`, inside `extract_post_info`): FB serves the same group post as both `/groups/<gid>/posts/<pid>` and `/groups/<gid>/permalink/<pid>`, on www/m/web hosts — all normalized to `https://www.facebook.com/groups/<gid>/posts/<pid>/` (trailing slash kept, matches the shape already stored in DB/sheet) before any dedupe check, so a re-encountered post under a different URL shape can't cost a duplicate LLM call. Unknown URL shapes pass through unchanged.
- `chrome_profile/` directory locks when Chrome is running — kill zombie Chrome processes before rerun
- `MAX_CONCURRENT_GROUPS` (config.py) trades speed for detection risk — multiple simultaneous tabs from one FB account is a more bot-like pattern than sequential scanning

## Conventions

- English identifiers, Hebrew user-facing strings, English console/log output. No emojis in prints — plain text with `ERROR:`/`WARNING:`/`SUCCESS:` prefixes where relevant
- Wrap every Playwright interaction in `try/except` — do not simplify
- Multi-selector fallback lists preferred over single CSS/XPath locators
- New tunables belong in `config.py`, not inline in `apartment_bot.py`
- All output goes through `_safe_print()` (lock-protected, prefixed with `[Group N/M]`) — plain `print`/`sys.stdout.write` from within `_scan_group` will interleave garbled across threads

## Sensitive Files

- `credentials.json` and `.env` are gitignored
- Verified via `git log --all -- credentials.json .env` (2026-07-17): neither file has ever been committed. History is clean.
