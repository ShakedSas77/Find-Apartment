# Adding to the bot

A short map for adding a feature. `CLAUDE.md` has the full module table, and the skills under
`.claude/skills/` hold the deep notes for each area.

## Where code goes

| Folder | What lives there |
|---|---|
| repo root | entry points you run: `apartment_bot.py` (scans + CLI flags), `bot_listener.py` (Telegram), `doctor.py`, `setup_wizard.py`, plus `config.py` and the launchers (`*.bat`, `*.vbs`) |
| `core/` | the pipeline and shared logic: `evaluate.py` (filters → row), `llm.py`, `normalize.py`, `scoring.py`, `storage.py` (SQLite) |
| `sources/` | where listings come from: `fb_scraper.py` (Facebook), `mail_source.py` (Yad2 emails) |
| `integrations/` | outside services: `sheets.py` + `local_workbook.py` (results), `maps.py` + `distance_fallback.py`, `telegram_*.py`, `groups_sync.py` |
| `tests/` | pytest suite, one file per module |

## Settings

- A new tunable goes in `config.py` with a comment saying what it does and its default.
- A new optional feature gets an on/off switch there too (`..._ENABLED`, default off), and:
  - `env.missing_env()` asks for its keys only when it's on;
  - `doctor.py` gets a check that returns `OFF` when it's off;
  - `setup_wizard.py` gets a step (add it to `STEPS`, so `--add <name>` works).
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
- **A new Telegram command**: add it to `COMMANDS` and `run_command()` in
  `integrations/telegram_commands.py`; a button for it is `{"callback_data": "cmd:<name>"}`.

## Tests

Run `python -m pytest tests` before sending a change. Each test checks one behaviour with one
assertion, uses the real code, and fakes only what crosses the machine's edge (network, browser,
clock, files outside a temp folder).
