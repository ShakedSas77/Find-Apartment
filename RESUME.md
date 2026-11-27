# Resuming after the break (paused 2026-11-27)

## Branches (none merged yet)

Stacked, merge in this order — each PR's base is the branch before it:

1. `feat/optional-services` → `main` — Excel output, guest mode, install.bat, console wizard, user guide
2. `feat/web-app` → `feat/optional-services` — no-AI mode, Streamlit web app, add-on setup
3. `chore/simplify` → `feat/web-app` — cleanup audit (draft PR): ratings removed, Hebrew prompt removed,
   GraphQL capture off by default, shared sheet columns, `_location_verdict`, add-on saves in `core/addon_setup.py`

If `main` moved meanwhile, rebase from the bottom of the stack up.

## Switched off for the break

| What | How to turn it back on |
|---|---|
| Scheduled scans (Task Scheduler `ApartmentScraper`) | `Enable-ScheduledTask -TaskName ApartmentScraper` (PowerShell), or Task Scheduler → Enable |
| Telegram listener (votes + chat commands) and its Startup shortcut | Run `run_listener_hidden.vbs`; re-add the Startup shortcut from the web app's Settings → Telegram |

Telegram alerts from scans don't need the listener.

## First checks when back

1. `python doctor.py` — every line PASS. Expect the Facebook session to have expired: run
   `python apartment_bot.py --login` (or the web app's Facebook step).
2. **Scheduled runs were failing before the pause**: from 2026-11-24 the task exited with code 255 and
   left no `logs\run_*.log` and no row in `--stats`. Last good run: 2026-11-24 08:00. Run `run_scheduled.bat`
   by hand and read its output before re-enabling the task.
3. `pytest` — 461 passed at pause time.
4. One `python apartment_bot.py --dry-run` before the first live run.

## Data outside git

`.env`, `credentials.json`, `config_local.py` and `bot_data.db` are gitignored. A zip of all four (DB snapshot
from 2026-11-27) was saved outside the repo at pause time; `backups/` holds the last 14 DB snapshots.

## Open follow-ups

- `--replay` can't judge distance/location (no stored distance source) — it reports stored
  `rejected_distance`/`rejected_location` posts as "→ added".
- Vote buttons only exist on old alerts (new alerts carry link buttons only) — remove or bring back.
- The console wizard has no "none" (no-AI) option; the web app does.
- `--recompute-scores` and vote actions cover only the main tab, not Yad2.
- The bot is tuned to Ramat Gan / Givatayim / Tel Aviv (prompt default city, city regexes, focus neighbourhoods).
