@echo off
:: ==============================================================================
:: Facebook Apartment Scraper Bot - Launcher
:: ==============================================================================
::
:: HOW TO RUN THIS FILE FROM TERMINAL / CMD:
::
:: 1. Open Terminal/CMD and navigate to project directory:
::    cd "<the folder you cloned the repo into>"
::
:: 2. Run Options:
::
::    [A] LIVE RUN (Default: headless, writes to Google Sheets, deletes dead links, updates DB):
::        run_bot.bat
::
::    [B] DRY RUN (Classifies & prints to console only, NO sheet/DB writes):
::        run_bot.bat --dry-run
::
::    [C] WITH BROWSER UI (First FB login, solving a checkpoint, visual debugging):
::        run_bot.bat --headful
::
::    [D] SPECIAL UTILITY FLAGS (Run standalone maintenance actions):
::        run_bot.bat --reparse-rejected   (Re-run LLM/filters on stored failed posts)
::        run_bot.bat --replay             (Test code/prompt changes against DB posts)
::        run_bot.bat --recompute-scores   (Recalculate fit scores for all sheet rows)
::        run_bot.bat --prune              (Remove rows older than MAX_POST_AGE_DAYS)
::        run_bot.bat --stats              (Print DB yield and Maps API usage)
::
:: ==============================================================================

:: Ensure script always executes inside the project directory
cd /d "%~dp0"

:: Run python using the virtual environment, passing all command-line arguments (%*)
"venv\Scripts\python.exe" apartment_bot.py %*

:: Keep window open so logs remain visible after execution completes
pause