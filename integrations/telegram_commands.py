"""
Chat commands for the Telegram bot, handled by bot_listener.py: /scan starts a live scan in the
background, /stop ends one, /status reports, and /stats, /doctor, /sync_groups run the matching
CLI command and reply with its output.

Only the configured chat (TELEGRAM_CHAT_ID) is obeyed, and only the fixed commands in COMMANDS,
never free-form arguments: a message's text never reaches a command line.
"""
import html
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from core import storage

COMMANDS = {
    "scan": "הפעלת סריקה מלאה",
    "stop": "עצירת הסריקה שהופעלה מכאן",
    "status": "האם סריקה רצה, ומה הייתה הסריקה האחרונה",
    "stats": "סטטיסטיקה מהמאגר",
    "doctor": "בדיקת תקינות של כל השירותים",
    "sync_groups": "סנכרון לשונית Groups ומיון לפי אחוז התאמה",
    "help": "רשימת הפקודות",
}

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_CLI_COMMANDS = {
    "stats": ["apartment_bot.py", "--stats"],
    "doctor": ["doctor.py"],
    "sync_groups": ["apartment_bot.py", "--sync-groups"],
}
_COMMAND_RE = re.compile(r"^/([a-z_]+)(?:@\w+)?(?:\s|$)")
# Telegram caps a message at 4096 characters; the rest is room for the <pre> wrapper and the title.
_OUTPUT_LIMIT = 3500
_CLI_TIMEOUT_S = 300
# getUpdates hands over whatever arrived while the listener was down; an old /scan must not fire hours later.
_STALE_COMMAND_S = 300
# A run row still "running" after this long was killed without closing it, not a scan in progress.
_RUNNING_ROW_MAX_AGE = timedelta(hours=2)
# The child prints Hebrew and ₪; with stdout not a console, Windows Python would default to cp1252 and crash.
_CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_scan_lock = threading.Lock()
_scan_process: subprocess.Popen | None = None
_stop_requested = False


def handle_message(message: dict, allowed_chat_id: str, send):
    """
    Runs the command in a Telegram message, if any. send(html_text) replies to the chat. Slow
    commands run on a thread, so the listener keeps answering vote taps meanwhile.
    """
    command = parse_command(message.get("text") or "")
    if not command or str(message.get("chat", {}).get("id")) != str(allowed_chat_id):
        return
    if time.time() - message.get("date", 0) > _STALE_COMMAND_S:
        return
    if command == "scan":
        _start_scan(send)
    elif command == "stop":
        _stop_scan(send)
    elif command == "status":
        send(format_status(_running_since(), _last_finished_run()))
    elif command in _CLI_COMMANDS:
        threading.Thread(target=_run_cli, args=(command, send), daemon=True).start()
    else:
        send(format_help())


def parse_command(text: str) -> str | None:
    """'/scan' or '/scan@SomeBot' (how a group chat sends it) -> 'scan'; None when not a command."""
    match = _COMMAND_RE.match(text.strip())
    return match.group(1) if match else None


def format_help() -> str:
    return "<b>פקודות</b>\n" + "\n".join(f"/{name} — {html.escape(text)}" for name, text in COMMANDS.items())


def format_status(running_since: str | None, last_run: dict | None) -> str:
    lines = [f"⏳ סריקה רצה עכשיו (התחילה {running_since})" if running_since else "💤 אין סריקה פעילה"]
    if last_run:
        lines.append(f"⏮ סריקה אחרונה: {last_run['started_at'].replace('T', ' ')[:16]} · {last_run['status']}"
                     f" · {last_run.get('added') or 0} דירות נוספו")
    return "\n".join(lines)


def _start_scan(send):
    global _scan_process, _stop_requested
    with _scan_lock:
        running_since = _running_since()
        if running_since:
            send(f"⏳ סריקה כבר רצה (התחילה {running_since})")
            return
        _stop_requested = False
        _scan_process = subprocess.Popen(
            [sys.executable, "-u", "apartment_bot.py"], cwd=_PROJECT_DIR, env=_CHILD_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        process = _scan_process
    send("▶️ מפעיל סריקה — הודעת ההתחלה עם ההתקדמות תגיע בעוד רגע")
    threading.Thread(target=_report_scan_exit, args=(process, send), daemon=True).start()


def _report_scan_exit(process: subprocess.Popen, send):
    # A clean or checkpoint finish already sends its own run-finished message; this covers what
    # doesn't, e.g. a lost Facebook login (exit 1) failing before the run-started alert.
    code = process.wait()
    if code not in (0, 2) and not _stop_requested:
        send(f"❌ הסריקה נכשלה (קוד {code}) — פרטים בקובץ הלוג בתיקייה logs")


def _stop_scan(send):
    global _stop_requested
    with _scan_lock:
        process = _scan_process
        if process is None or process.poll() is not None:
            note = " (הופעלה מחוץ לבוט — אפשר לעצור רק סריקה שהופעלה מכאן)" if _running_since() else ""
            send(f"אין סריקה פעילה שהופעלה מכאן{note}")
            return
        _stop_requested = True
        _kill_process_tree(process)
        _close_running_rows()
    send("⏹ הסריקה נעצרה")


def _kill_process_tree(process: subprocess.Popen):
    # Playwright's driver and Chromium are child processes; killing only Python would leave them
    # holding chrome_profile's lock, and the next scan would fail to launch.
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()
    process.wait()


def _close_running_rows():
    # A killed scan never reaches finish_run(); left "running", its row would read as a scan in
    # progress for the next _RUNNING_ROW_MAX_AGE.
    for run in storage.get_recent_runs(5):
        if run["status"] == "running":
            storage.finish_run(run["id"], "stopped", {}, 0, 0)


def _running_since() -> str | None:
    # A scan started from here, or one the Task Scheduler started (seen only through its run row).
    if _scan_process is not None and _scan_process.poll() is None:
        return "מהבוט"
    for run in storage.get_recent_runs(1):
        started = datetime.fromisoformat(run["started_at"])
        if run["status"] == "running" and datetime.now() - started < _RUNNING_ROW_MAX_AGE:
            return f"{started:%H:%M}"
    return None


def _last_finished_run() -> dict | None:
    return next((run for run in storage.get_recent_runs(10) if run["status"] != "running"), None)


def _run_cli(command: str, send):
    send(f"⏳ מריץ /{command}...")
    try:
        result = subprocess.run(
            [sys.executable, *_CLI_COMMANDS[command]], cwd=_PROJECT_DIR, env=_CHILD_ENV, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=_CLI_TIMEOUT_S, creationflags=_NO_WINDOW,
        )
        output = (result.stdout + result.stderr).strip() or "(אין פלט)"
    except subprocess.TimeoutExpired:
        output = f"(נעצר אחרי {_CLI_TIMEOUT_S} שניות)"
    send(format_cli_output(command, output))


def format_cli_output(command: str, output: str) -> str:
    if len(output) > _OUTPUT_LIMIT:
        output = output[:_OUTPUT_LIMIT] + "\n..."
    return f"<b>/{command}</b>\n<pre>{html.escape(output)}</pre>"
