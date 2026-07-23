@echo off
cd /d "C:\Users\Shaked Sasporta\Documents\antigravity\find apartment"
if not exist logs mkdir logs

for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%T
set LOGFILE=logs\run_%STAMP%.log

tasklist /fi "imagename eq ollama.exe" | find /i "ollama.exe" >nul
if errorlevel 1 (
    echo Starting Ollama (Gemini-fallback dependency, not running)... >> "%LOGFILE%"
    start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    timeout /t 5 /nobreak >nul
)

"venv\Scripts\python.exe" -u apartment_bot.py --headless --live >> "%LOGFILE%" 2>&1
set EXITCODE=%ERRORLEVEL%
echo Exit code: %EXITCODE% >> "%LOGFILE%"

powershell -NoProfile -Command "Get-ChildItem 'logs\run_*.log' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -Force"

exit /b %EXITCODE%
