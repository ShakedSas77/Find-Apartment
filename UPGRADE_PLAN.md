# Upgrade Plan — Facebook Apartment Scraper (v2)

**Audience:** an AI coding agent (Claude Sonnet) working inside the `Find-Apartment` repo with `CLAUDE.md` in context.
**Author context:** this plan was produced from a full code review of `apartment_bot.py`, `config.py`, `prompts.py`, `README.md`, and `CLAUDE.md` as of 2026-07-17.

## Ground rules for the implementing agent

1. Read `CLAUDE.md` first and obey its conventions: English identifiers, Hebrew user-facing strings, `_safe_print()` everywhere inside threaded code, multi-selector fallbacks, every Playwright interaction wrapped in try/except, new tunables go in `config.py`.
2. Do NOT translate, reformat, or "clean up" existing Hebrew regexes, prompts, or sheet headers except where a task below explicitly says so.
3. Never touch `credentials.json`, `.env`, or `chrome_profile/`. Never commit them.
4. Keep the bot runnable after every task — these are incremental upgrades to a working tool, not a rewrite.
5. After all tasks: update `README.md` and `CLAUDE.md` to reflect the new architecture, flags, and files.

---

## Task 1 — Fix the dead Gemini model (P0, ~15 min)

**Problem:** `gemini-2.0-flash` was retired by Google on 2026-06-01. Every call now errors, so each run burns 3 failed calls and silently falls back to Ollama permanently.

**Changes:**
- Add `GEMINI_MODEL = "gemini-2.5-flash-lite"` to `config.py` (chosen for the largest free-tier quota; comment that `"gemini-2.5-flash"` is the higher-quality alternative).
- Replace the hardcoded `model='gemini-2.0-flash'` in `analyze_post_with_llm()` with the config value.
- On a Gemini error containing `404` / `NOT_FOUND` for the model name, print a Hebrew-friendly hint: "המודל הוגדר לא נכון או הוצא משימוש — עדכן GEMINI_MODEL ב-config.py" and treat it like a permanent switch to Ollama (same as quota exhaustion, don't count it toward the 3-error threshold).

**Acceptance:** a run with a valid API key parses at least one post via Gemini with no fallback warnings.

## Task 2 — SQLite persistence layer (P0, the big one)

**Problem:** dedup relies on the Google Sheet, which only stores *matches*. Every rejected or pre-filtered post is re-scraped and re-LLM-parsed on every run. There is also no raw-text audit trail for prompt iteration.

**Changes:**
- New file `storage.py` using stdlib `sqlite3`, DB file `bot_data.db` (add to `.gitignore`).
- Table `posts`: `url TEXT PRIMARY KEY, group_url TEXT, raw_text TEXT, parsed_json TEXT, verdict TEXT, first_seen TEXT, last_processed TEXT`.
  - `verdict` values: `added`, `rejected_price`, `rejected_rooms`, `prefiltered`, `parse_failed`.
- Table `api_usage`: `month TEXT PRIMARY KEY, gmaps_calls INTEGER` (used by Task 3).
- All DB access goes through `storage.py` functions, guarded by a single `threading.Lock` inside the module (the existing code is threaded).
- In `_scan_group`, immediately after `extract_post_info`: check SQLite first, then `seen_urls` from the sheet. Skip anything with any verdict except `parse_failed` (those retry automatically on the next run, capped at 3 attempts — store an `attempts` column).
- Record every processed post with its verdict and raw text (post-BIDI-strip). For LLM-parsed posts, store the parsed JSON too.
- New CLI flags in `apartment_bot.py`:
  - `--reparse-rejected` — re-run the LLM + filters on stored raw text for `rejected_*` and `parse_failed` posts, **without opening the browser at all**. This is the prompt-iteration workflow.
  - `--stats` — print counts per verdict and current month's Maps usage, then exit.
- The sheet remains the source of truth for matches; SQLite is the memory of everything else. On startup, still load `seen_urls` from the sheet as a secondary guard (unchanged behavior).

**Acceptance:** second consecutive run over the same feed performs zero LLM calls for previously seen posts; `--reparse-rejected` runs with no browser window; `--stats` prints a sane table.

## Task 3 — Google Maps quota guard (P0 per user requirement)

**Problem:** the free tier is ~10,000 Distance Matrix billable elements/month; the user must never be charged.

**Changes:**
- Config: `GMAPS_MONTHLY_CAP = 9000` (safety margin below 10K), `GMAPS_ON_CAP = "skip"` with allowed values `"skip"` (stop computing distances, keep finding apartments, write `"מכסה חודשית הסתיימה"` in the distance column) or `"halt"` (print a clear message and stop the run). **Default is `"skip"` — user decision, confirmed.** Implement both values.
- Before every `distance_matrix` call in `get_walking_distance`: check `api_usage` for the current `YYYY-MM`; if `gmaps_calls >= GMAPS_MONTHLY_CAP`, apply the `GMAPS_ON_CAP` behavior. Otherwise increment the counter **before** the call (count 1 element per call — origins×destinations is 1×1 here).
- Counter resets naturally by month key. Existing "city-only address → skip" logic stays (it already saves quota).
- Print remaining quota once per run at startup, and a warning at ≥80% of cap.
- **README addition (belt and suspenders):** instruct the user to also set a hard quota cap inside Google Cloud Console (APIs & Services → Distance Matrix API → Quotas → set "Elements per day" to ~300). The local counter cannot see usage from other tools using the same key; the console cap is the authoritative protection. Include the exact click path.

**Acceptance:** with `GMAPS_MONTHLY_CAP = 0` set temporarily, a run adds listings with the quota-reached placeholder in the distance column and makes zero Maps calls.

## Task 4 — Headless checkpoint deadlock (P1)

**Problem:** `_handle_checkpoint_if_present` calls `input()` + `bring_to_front()`; in `--headless` mode there is no visible window, so the run hangs forever on a CAPTCHA nobody can see.

**Changes:**
- The function receives (or reads) the `headless` flag. If a checkpoint is detected while headless: save `checkpoint_<group_label>.png`, `_safe_print` a message telling the user to rerun without `--headless`, and abort that group cleanly (return a sentinel so the group counts as skipped, not crashed). Set a module-level flag so *other* groups also abort instead of each hitting the same wall.
- Run summary at the end must state: "N groups skipped due to a security checkpoint — rerun headful."

**Acceptance:** simulated checkpoint (e.g., temporary selector that always matches) in headless mode exits within seconds with the screenshot and message; headful behavior is unchanged.

## Task 5 — Safer default concurrency + persistent-context sequential mode (P1)

**Problem:** parallel mode launches fresh browser instances with injected cookies — same session, multiple simultaneous browsers, fingerprint mismatch with the real profile. Elevated checkpoint risk.

**Changes (user decision: default stays `MAX_CONCURRENT_GROUPS = 3`):**
- Keep the default at 3, but expand the `config.py` comment to state plainly that parallel mode raises checkpoint/ban risk and that `1` is the safe fallback if checkpoints start appearing.
- Implement a true sequential mode for when the user sets `MAX_CONCURRENT_GROUPS = 1`: skip the storage-state export entirely and scan all groups **sequentially inside the original persistent context** (`launch_persistent_context`), one page, navigating group to group, with randomized 5–15s pauses between groups. This preserves the real profile fingerprint.
- When `> 1`: keep the current threaded storage-state architecture unchanged, plus a one-line risk note at startup.

**Acceptance:** with config set to 1, a run scans all 7 groups in one browser window with no `_session_state.json` written; default parallel mode behaves exactly as today.

## Task 6 — Ollama structured output + universal Pydantic validation (P1)

**Problem:** the Ollama path uses `format='json'` plus regex JSON repair, and its output is never validated; the Gemini path gets a real schema. Since Ollama is currently the only working path (Task 1), it deserves equal rigor.

**Changes:**
- Pass `format=ApartmentData.model_json_schema()` to `ollama.chat(...)` — Ollama constrains decoding to the schema. Delete the `re.search(r'\{.*\}')` and trailing-comma repair hacks.
- After **both** paths, run the dict through `ApartmentData.model_validate(...)` inside try/except; on validation failure, one retry, then verdict `parse_failed`.
- Rewrite the prompt in `prompts.py` with **English instructions + the same Hebrew field rules as few-shot content** (system-style English framing, Hebrew examples, output values stay Hebrew exactly as today — e.g., address must remain Hebrew-only). Keep the old Hebrew prompt as `get_apartment_prompt_hebrew()` and add `PROMPT_LANGUAGE = "en"` to `config.py` so the user can A/B them. Do not change the JSON keys.
- Add `keep_alive='10m'` and `num_ctx` sized ~4096 to the Ollama options.

**Acceptance:** 5 sample Hebrew posts (create `test_posts/` with realistic fabricated posts) parse through the Ollama path with valid schema on both prompt variants; no JSON-repair code remains.

## Task 7 — Narrow the second-chance price override (P2)

**Problem:** if the LLM extracts a *correct* out-of-budget price (e.g., 7,200), any unrelated 4–5 digit number inside the budget window (old price, vaad annual fee, sqm) currently overrides it and creates a false positive.

**Changes:**
- Apply the regex override only when the LLM returned `price = null`, OR when the candidate number appears within ~25 characters of a price-context token: `₪`, `ש"ח`, `שח`, `שכ"ד`, `שכר דירה`, `מחיר`, `לחודש`.
- Keep the existing thousands-separator normalization and the "replace absurd values for log sanity" branch.
- Same principle for the rooms override: only when LLM `rooms` is null or non-numeric.

**Acceptance:** unit-style test in `test_posts/`: a post with "היה 6,500 עכשיו 7,200 ש"ח" must be rejected; a post where the LLM returns null but "6,300 ₪ לחודש" appears must pass.

## Task 8 — Date-parsing robustness (descoped to P3)

**Context:** the user's Facebook UI is confirmed English, so the existing `6h`/`1d`/`3w` parser is correct for now. Do NOT build full Hebrew date parsing.

**Changes (small):**
- Extend the English regex to also cover longer forms Facebook sometimes renders: `"3 hrs"`, `"1 day"`, `"2 wks"`, `"Yesterday"`.
- When a timestamp string fails to parse, keep passing it through unchanged (current behavior) but log it once per run via `_safe_print` so a future locale change is noticed instead of silently degrading.
- One-line README note: the bot assumes an English-locale Facebook UI for post dates.

**Acceptance:** "5h", "3 hrs", "1 day", "Yesterday" all convert; an unknown string passes through and produces a single log line.

## Task 9 — Small fixes batch (P3)

1. `_warn_if_fee_implausible` → use `_safe_print` (repo convention violation).
2. `ROOMS_PRE_FILTER_REGEX` → also match `3 וחצי חד` (digit + וחצי), and add word-ish boundary so `13 חד` / `23 חד` don't match the `3` branch.
3. Move `SHEET_ID` from `config.py` to `.env` (`SHEET_ID=` key), since the repo is now public. Keep a placeholder comment in `config.py` pointing to `.env`.
4. End-of-run summary line: groups scanned, posts seen, pre-filtered, LLM-parsed, matches added, Maps calls used this month, checkpoints hit.
5. Scroll distance jitter: `page.mouse.wheel(0, random.randint(3000, 5000))` instead of fixed 4000.

## Explicitly OUT of scope

- No scheduler/daemon (bot stays manually triggered).
- No proxy/stealth-plugin additions.
- No change to sheet headers/columns or filter criteria values.
- No Gemini→local migration beyond what's specified — dual-path stays.

## Suggested implementation order

Task 1 → 2 → 3 → 6 → 4 → 5 → 7 → 8 → 9, committing after each task with a message referencing the task number. Tasks 2+3 share `storage.py`, build them together.

## Manual test checklist (run by the human after implementation)

1. `python apartment_bot.py --stats` — prints verdict counts + Maps usage.
2. Full headful run — confirm Gemini works (no fallback warning), new posts land in the sheet, SQLite grows.
3. Immediate second run — near-instant, zero LLM calls on old posts.
4. `python apartment_bot.py --reparse-rejected` — no browser opens.
5. Set `GMAPS_MONTHLY_CAP = 0`, run — distance column shows the quota placeholder, restore cap after.
6. `--headless` run — completes or aborts cleanly, never hangs.
