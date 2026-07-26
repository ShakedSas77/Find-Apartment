# Anti-Detection Hardening + General Improvements

Implemented 2026-07-18. Inspired by reviewing three similar scrapers:
- `hyuwowo/fb-marketplace-scraper` — Playwright + stealth plugin + browserforge fingerprints + Bezier mouse movement + aria-label fallback parsing
- `sumentse/facebook-rental-scraper` — Node.js, cron scheduling (nothing else novel)
- `barakplasma` gist / `kevinzg/facebook-scraper` — no-browser HTTP scraping + regex + SQLite (our bot already supersedes it)

Goal: reduce **headless security checkpoints**, the bot's biggest operational pain (headless hits checkpoint → screenshot + abort all groups).

## Rejected (deliberately not implemented)

- `browserforge` random fingerprints — random identity per session *diverges* from the account's known device; more suspicious for a logged-in single-account scraper, not less.
- httpx / no-browser scraping — FB groups need auth'd GraphQL, brittle, higher ban risk.
- `kevinzg/facebook-scraper` — its README admits group scraping "may return only one page and not work on private groups" (our targets are private); IP-ban prone, unmaintained since ~2022.
- CSV/JSON export — Google Sheet already covers.

## What shipped

| # | Change | Where |
|---|---|---|
| 1 | `tf-playwright-stealth` applied to every page via `_apply_stealth()` before first navigation. Kill switch `STEALTH_ENABLED` (config.py). Warns once + runs on if package missing. | both launch sites |
| 2 | Two stealth patches disabled: `StealthConfig(navigator_user_agent=False, navigator_languages=False)` — v1.2.0 fakes UA with hardcoded Chrome 95 + en-US, a louder bot signal than what it hides and would stomp the real fingerprint. | `_apply_stealth` |
| 3 | Parallel workers get login profile's real UA (`navigator.userAgent`, "HeadlessChrome" stripped) + `navigator.language` into `new_context(user_agent=, locale=)`. | `run_scraper` → `_scan_group` |
| 4 | Headless persistent context gets corrected UA from a throwaway probe launch (headless UA literally says "HeadlessChrome", settable only at context creation). | `run_scraper` |
| 5 | `--disable-blink-features=AutomationControlled` — already present in `_CHROME_LAUNCH_ARGS`. | both |
| 6 | `_canonical_post_url()` — `/permalink/<id>` ↔ `/posts/<id>`, m/web/www hosts → one canonical form before dedupe. Kills duplicate LLM spend on same post via different URL shape. Unknown shapes pass through. | `extract_post_info` |
| 7 | 7th article selector `div[aria-labelledby][aria-describedby]` (FB post containers carry both even without `role="article"`). | `_scan_group_page` |
| 8 | ~25% per-iteration `page.mouse.move(..., steps=5-15)` cursor drift in scroll loop. | `_scan_group_page` |

Note: the plan's "jitter per-post `time.sleep(2)`" was dropped — that sleep no longer exists in code; per-post pacing comes from the Gemini 4s rate lock + jittered scroll delays. CLAUDE.md was stale and got corrected.

## Files touched

`apartment_bot.py`, `config.py` (`STEALTH_ENABLED`), `requirements.txt` (`tf-playwright-stealth>=1.1.0`), `CLAUDE.md` (Anti-Bot section).

## Verification done

- Compile + import clean.
- `_canonical_post_url` unit cases: 6/6 pass (permalink→posts, m/web/www normalization, marketplace + non-post pass-through).
- Real headless Playwright launch with stealth: UA stays Chrome/138 (not stomped to 95), `navigator.webdriver` false, `he-IL` locale preserved.

## Not yet verified

Real FB run. True success metric = checkpoint frequency over subsequent daily runs. If anything renders oddly, set `STEALTH_ENABLED = False` in config.py.
