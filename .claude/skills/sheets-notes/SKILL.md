---
name: sheets-notes
description: >
  Google Sheet layout and writes: headers, append/rewrite, dedupe_and_sort_sheet, field normalization (floor/fees/agent), formula-injection guard. Load before editing integrations/sheets.py or _build_row in core/evaluate.py.
---

# Sheets Notes

> Moved verbatim from `CLAUDE.md` (2026-11-07). Keep project-wide rules in `CLAUDE.md`;
> deep notes for these modules live here.

## Google Sheets

- `SHEET_ID` lives in `.env` (not `config.py` — repo is public), accessed via `env.get_sheet_id()` alongside `env.get_gemini_api_key()`/`env.get_gmaps_api_key()`; `env.require_env()` (called from `apartment_bot.py`'s `__main__`, not at import) exits at startup if any of the three are missing. Sheet must be shared with the service account email from `credentials.json`
- 17 Hebrew headers defined in `config.py` (`SHEET_HEADERS`) — the 14 data columns, a `זמן סריקה` (scan timestamp) column, `ציון התאמה` (fit score, see the `scoring-notes` skill), then a trailing `עלות חודשית כוללת` (`scoring.compute_total_monthly_cost()`: rent + arnona/2 + vaad/2 + rent/`TOTAL_COST_AGENT_FEE_AMORTIZE_MONTHS` (24) if agent; blank arnona/vaad fall back to `DEFAULT_ARNONA_MONTHLY_BY_CITY`/`DEFAULT_VAAD_MONTHLY` in config.py; `--recompute-scores` backfills existing rows)
- `setup_google_sheet()` auto-seeds headers if sheet is empty
- Uses `sheet1` (first tab) for Facebook listings. Yad2 listings (`sources/mail_source.py`) go to a separate `Yad2` tab (`YAD2_TAB_NAME`) with the same `SHEET_HEADERS`, opened/created by `open_listings_tab()` (a dry run never creates it); `_end_of_run_upkeep` runs `dedupe_and_sort_sheet()` on it too. Not covered there: `--recompute-scores`, dead-link pruning (Facebook-only anyway) and `bot_listener.py`'s vote actions, which only look at `sheet1`.
- No conditional formatting/colors/filters/frozen row are applied — plain sheet, as-is. (A `format_google_sheet()` step existed briefly and was removed; don't re-add it without being asked.)
- Rows appended via `sheet.append_row(new_row)` during scanning
- After each run, `dedupe_and_sort_sheet()` rewrites the whole data range: collapses cross-posted duplicates (same normalized street + rooms + price, different URL — keeps the newest post date) and sorts by post date, newest first. Callable standalone against an existing sheet, not just at the end of a scrape. Rows with an empty URL cell are never deduped, only kept as-is (2026-11-07: they all shared the key `("url", "")` and were merged into one — combined with an append that landed at column S because of a manual note there, this silently deleted every live-run row for a week). Appends pass `table_range="A1"` so a note past the last header column can't shift them again.
- Walking distance is stored as a plain km number (e.g. `"1.4"`), not the full Google-provided text — computed from `dist_meters` in `get_walking_distance()`, not string-parsed
- Posts older than `MAX_POST_AGE_DAYS` (config.py) are pre-filtered before LLM analysis
- Floor (`_parse_floor`), arnona/vaad (`_normalize_bimonthly_fee`), and agent/private (`_detect_agent`) are all normalized in Python after the LLM call, not trusted as free text: floor becomes an integer ("קרקע" = 0, explicit digit wins over an incidental "קרקע" mention), fees become bi-monthly integers with no currency/unit text, and agent detection adds a deterministic "תיווך"/"נדל\"ן" text-signal check on top of the LLM's `is_agent` judgment (explicit negations like "ללא תיווך" are excluded from triggering it)
- **Formula-injection guard** (`_sheet_safe_cell()`, security audit 2026-07-18): rows are written with `value_input_option="USER_ENTERED"` (needed for price/rooms to land as real numbers, not text), which also lets Sheets read a cell starting with `=`/`+`/`-`/`@` as a formula. Row content ultimately comes from scraped Facebook text via an LLM, so `_build_row()` (`core/evaluate.py`) runs every field through `_sheet_safe_cell()` (`core/normalize.py`), which prefixes such a string with a literal `'`. This is deliberate defense-in-depth — every current field already happens to be safe by construction (fixed value sets, numeric, or Hebrew-only text that excludes those characters) but that was incidental, not designed, before this guard existed.
