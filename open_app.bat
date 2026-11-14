@echo off
:: The bot's web app: opens in your browser, on this PC only. Keep this window open while you use it;
:: closing it closes the app (scans already running finish on their own).
cd /d "%~dp0"
echo Starting the apartment bot app... (it opens in your browser; close this window to stop it)
"venv\Scripts\python.exe" -m streamlit run webapp\app.py --server.address 127.0.0.1 --browser.gatherUsageStats false --server.headless false --client.toolbarMode minimal
pause
