@echo off
:: One-time install: a private Python environment (venv), the libraries, the bot's browser,
:: then the setup wizard. Safe to run again (e.g. after pulling a new version).
cd /d "%~dp0"
echo === Apartment bot install ===
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :no_python
%PY% -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>nul || goto :no_python

if not exist venv\Scripts\python.exe (
    echo Creating the Python environment...
    %PY% -m venv venv || goto :failed
)
echo Installing libraries (a few minutes the first time)...
venv\Scripts\python.exe -m pip install --quiet --upgrade pip
venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :failed
echo Installing the bot's browser...
venv\Scripts\python.exe -m playwright install chromium || goto :failed

echo.
venv\Scripts\python.exe setup_wizard.py
pause
exit /b 0

:no_python
echo Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), or run:  winget install Python.Python.3.12
echo Then run install.bat again.
pause
exit /b 1

:failed
echo.
echo Install failed - see the messages above. Check the internet connection and run install.bat again.
pause
exit /b 1
