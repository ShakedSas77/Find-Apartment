# Plan: Harden the bot for unattended Windows Task Scheduler runs

Status: **planned, not implemented.** For Sonnet 5 to execute later.

## Context

The bot is meant to run daily headless via Windows Task Scheduler. A scheduled run has **no terminal and no human** — so any silent failure (checkpoint, lost login, crash) just produces a dead run nobody notices, and matches that were added sit unseen. Lessons distilled from the three reviewed scrapers:

- `sumentse/facebook-rental-scraper` — its `cronjob-node.sh` wraps the command in a script rather than scheduling the raw binary. Lesson: the scheduled entry should be a wrapper (`run_bot.bat`) that logs to a file and surfaces a pass/fail result — the scheduler can't show a console.
- `hyuwowo/fb-marketplace-scraper` — headless-first design; a checkpoint under headless is a *silent* failure. Reinforces: never block on `input()` unattended (already handled — headless checkpoint saves a screenshot and aborts), and make that failure *visible* to the operator.

## Current state (grounded in code)

- `run_bot.bat` (2 lines): `cd` to project, run `python apartment_bot.py --headless`. **No logging, no exit-code handling, uses `anaconda3\python.exe` (not the venv where `tf-playwright-stealth` was just installed — mismatch risk).**
- `run_scraper()` returns nothing and the process always exits 0, even when `checkpoint_skipped > 0` or every group crashed. Task Scheduler's "Last Run Result" can't distinguish success from silent failure.
- Headless checkpoint handling already exists (screenshot + abort, no `input()` hang) — good; just not surfaced as an exit code.

## Changes

### 1. Exit code reflects outcome (`apartment_bot.py`)

- `run_scraper()` returns a small result (e.g. `checkpoint_skipped`, `groups_scanned`, `total`).
- In `__main__`, after `run_scraper(...)`:
  - `sys.exit(2)` if any checkpoint was hit (`checkpoint_skipped > 0`) — distinct code so the scheduler/log shows "needs a headful rerun".
  - `sys.exit(1)` if `groups_scanned == 0` (total failure — login lost or all groups crashed).
  - `sys.exit(0)` otherwise.
- Wrap the `run_scraper` call in try/except that prints the traceback and `sys.exit(1)` — an uncaught exception must still produce a non-zero code, not a bare stack trace to a closed console.

### 2. Logging wrapper (`run_bot.bat`)

Rewrite to:
- Use the **venv** interpreter (where `tf-playwright-stealth` lives), not `anaconda3\python.exe`. Confirm the venv path (`venv\Scripts\python.exe`) at implementation time.
- Create `logs\` if missing.
- Redirect stdout+stderr to a timestamped log: `logs\run_YYYY-MM-DD_HHMM.log` (build the name from `%date%`/`%time%`, locale-safe — Windows `%date%` format varies; prefer a tiny `powershell -Command "Get-Date -Format ..."` to name it deterministically).
- After the run, echo the exit code into the log and `exit /b %ERRORLEVEL%` so the scheduler's Last Run Result carries it through.

### 3. Log retention

- Prune `logs\*.log` older than N days (reuse `MAX_POST_AGE_DAYS` or a new `LOG_RETENTION_DAYS` in config.py) so daily runs don't grow logs unbounded — same spirit as the existing DB/sheet retention. Do it in the `.bat` (PowerShell one-liner) or at the end of `run_scraper()`; batch is simpler and keeps it out of the hot path.

### 4. (Optional, flag-gated) Notification on new matches

- Nobody watches an unattended run, so N new matches sit unseen until the operator opens the sheet. If `total_added > 0`, push a one-line notice (Telegram bot API or SMTP email — Telegram is simplest, no auth server).
- Gate behind a config flag (default off) + secrets in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Keep out of `config.py` (public repo).
- Lower priority than 1–3; those are correctness/visibility, this is convenience.

### 5. Task Scheduler setup (doc + optional helper)

- Document the exact `schtasks` command (or a `setup_schedule.ps1` helper) to register a daily trigger:
  - Randomized start (e.g. daily around 09:00 with the task's built-in random-delay option) — avoids an exactly-fixed time, mild anti-bot benefit and matches the shuffle/jitter already in the scraper.
  - "Run whether user is logged on or not" needs stored creds → note that headless still needs a **prior headful login** to seed `chrome_profile/`; document that the first run must be manual and headful.
  - Single-instance: Task Scheduler's "do not start a new instance" policy, so a run that overruns the next trigger doesn't launch a second browser against the same profile (`chrome_profile/` locks anyway, but the policy fails cleanly instead of erroring).

## Files

| File | Change |
|---|---|
| `apartment_bot.py` | `run_scraper` returns outcome; `__main__` maps it to exit codes 0/1/2 + try/except guard |
| `run_bot.bat` | venv interpreter, timestamped file logging, propagate exit code |
| `config.py` | `LOG_RETENTION_DAYS` (+ notification flag if #4 done) |
| `.env` | Telegram secrets (only if #4 done) — gitignored |
| `docs/` or repo root | Scheduler setup instructions / `setup_schedule.ps1` |

## Verification

1. `run_bot.bat` produces a `logs\run_*.log` with the full run output; `echo %ERRORLEVEL%` after it is 0 on a clean run.
2. Force a checkpoint path (or stub `checkpoint_skipped`) → exit code 2, log says rerun headful.
3. Simulate all-groups-fail (bad `TARGET_URLS`) → exit code 1.
4. Register the scheduled task, run it on-demand from Task Scheduler, confirm Last Run Result matches the exit code and the log file appears.
5. (If #4) trigger a run with ≥1 match → notification arrives.

## Notes

- Priorities: **1 and 2 are the high-value core** (visibility + a real pass/fail signal for unattended runs). 3 is hygiene. 4–5 are nice-to-have.
- Don't touch the stealth/fingerprint/checkpoint logic shipped 2026-07-18 (see `docs/anti-detection-hardening.md`) — this plan is purely the operational wrapper around it.
