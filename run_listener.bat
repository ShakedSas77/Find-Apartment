@echo off
:: Telegram listener (bot_listener.py): vote taps and chat commands (/scan, /status...).
:: Started hidden at Windows login by run_listener_hidden.vbs (Startup folder shortcut).
:: Restarts a minute after it exits, e.g. when the network is not up yet at login.
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
:loop
echo [%DATE% %TIME%] listener starting >> logs\listener.log
"venv\Scripts\python.exe" -u bot_listener.py >> logs\listener.log 2>&1
echo [%DATE% %TIME%] listener exited with code %ERRORLEVEL%, restarting in 60s >> logs\listener.log
timeout /t 60 /nobreak >nul
goto loop
