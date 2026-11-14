# Adding to the bot

A short map for adding a feature. `CLAUDE.md` has the full module table, and the skills under
`.claude/skills/` hold the deep notes for each area.

## Where code goes

| Folder | What lives there |
|---|---|
| repo root | entry points you run: `apartment_bot.py` (scans + CLI flags), `bot_listener.py` (Telegram), `doctor.py`, `setup_wizard.py`, plus `config.py` and the launchers (`*.bat`, `*.vbs`) |
| `core/` | the pipeline and shared logic: `evaluate.py` (filters → row), `llm.py`, `normalize.py`, `scoring.py`, `storage.py` (SQLite); `scan_runner.py`, `local_settings.py`, `addon_setup.py` (what the web app, the wizard and Telegram share) |
| `webapp/` | the Streamlit app: `views.py` (one function per page), `components.py` (sections shared by pages), `data.py` (plain data helpers, no Streamlit — test these directly) |
| `sources/` | where listings come from: `fb_scraper.py` (Facebook), `mail_source.py` (Yad2 emails) |
| `integrations/` | outside services: `sheets.py` + `local_workbook.py` (results), `maps.py` + `distance_fallback.py`, `telegram_*.py`, `groups_sync.py` |
| `tests/` | pytest suite, one file per module |

## Settings

- A new tunable goes in `config.py` with a comment saying what it does and its default.
- A new optional feature gets an on/off switch there too (`..._ENABLED`, default off), and:
  - `env.missing_env()` asks for its keys only when it's on;
  - `doctor.py` gets a check that returns `OFF` when it's off;
  - `core/addon_setup.py` gets its checks (no UI there), used by both:
  - a section in `webapp/components.py`, shown on the Settings page (`webapp/views.py`), and
  - a step in `setup_wizard.py` (add it to `STEPS`, so `--add <name>` works).
- Personal values (your groups, your address, keys) never go in `config.py` — the repo is public.
  They go in `config_local.py` and `.env`, both gitignored, which the wizard writes.

## Common additions

- **A new listing source** (another site, another kind of email): turn each listing into the fields
  dict and call `core.evaluate.process_candidate_listing(...)` like `sources/mail_source.py` does — it
  dedupes, filters, measures distance, writes the row and sends the Telegram alert. Call it from
  `run_scraper()` (`sources/fb_scraper.py`) the way `_scan_alert_emails` is.
- **A new results destination**: implement the same methods as `LocalWorkbook` / `LocalWorksheet` in
  `integrations/local_workbook.py` (the slice of gspread the bot uses) and return it from
  `integrations.sheets.open_spreadsheet()`.
- **A new web app page**: a function in `webapp/views.py`, listed in `st.navigation` in `webapp/app.py`.
  Put its data logic in `webapp/data.py` and test it there; test the page with `streamlit.testing.v1.AppTest`
  (see `tests/test_webapp.py`). Anything that writes the results file must be disabled while a scan runs
  (`components.edits_locked()`).
- **A new Telegram command**: add it to `COMMANDS` and `run_command()` in
  `integrations/telegram_commands.py`; a button for it is `{"callback_data": "cmd:<name>"}`.

## Tests

Run `python -m pytest tests` before sending a change. Each test checks one behaviour with one
assertion, uses the real code, and fakes only what crosses the machine's edge (network, browser,
clock, files outside a temp folder).
