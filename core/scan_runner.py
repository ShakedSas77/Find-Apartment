"""
Starting, stopping and watching scans from another process — the Telegram listener
(integrations/telegram_commands.py) and the web app (webapp/). A scan is `apartment_bot.py` run as a
child process. Whether one is running is read from the `runs` table, so a scan started by the Task
Scheduler, the chat or the web app is seen by all of them; stopping one started elsewhere uses the
pid apartment_bot.py records with its run id (storage.SCAN_PID_KEY).
"""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from core import storage

PROJECT_DIR = Path(__file__).resolve().parent.parent
# The child prints Hebrew and ₪; with stdout not a console, Windows Python would default to cp1252 and crash.
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# A run row still "running" after this long was killed without closing it, not a scan in progress.
_RUNNING_ROW_MAX_AGE = timedelta(hours=2)
# The scan rewrites its progress every PROGRESS_TICK_S (30s); older than this, it's from a scan that died.
_PROGRESS_MAX_AGE = timedelta(minutes=3)
_CLI_TIMEOUT_S = 300

_scan_lock = threading.Lock()
_scan_process: subprocess.Popen | None = None
_scan_started: datetime | None = None
_stop_requested = False


def start_scan(args: list[str], on_failure=None) -> str | None:
    """
    Starts `apartment_bot.py <args>` in the background. Returns None when started, or the start time
    of the scan already running (then nothing is started). on_failure(exit_code) is called from a
    watcher thread when the scan ends with a code other than 0/2 and wasn't stopped — those two send
    their own run-finished message; this covers e.g. a lost Facebook login failing before it starts.
    """
    global _scan_process, _scan_started, _stop_requested
    with _scan_lock:
        running = running_since()
        if running:
            return running
        _stop_requested = False
        _scan_started = datetime.now()
        _scan_process = subprocess.Popen(
            [sys.executable, "-u", "apartment_bot.py", *args], cwd=PROJECT_DIR, env=CHILD_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )
        process = _scan_process
    threading.Thread(target=_watch_exit, args=(process, on_failure), daemon=True).start()
    return None


def stop_scan() -> bool:
    """Stops the running scan (started here or anywhere else) and closes its run row. False when none runs."""
    global _stop_requested
    with _scan_lock:
        process = _scan_process if _scan_process is not None and _scan_process.poll() is None else None
        pid = process.pid if process else scheduled_scan_pid()
        if pid is None:
            return False
        _stop_requested = True
        _kill_process_tree(pid)
        if process:
            process.wait()
        _close_running_rows()
    return True


def running_since() -> str | None:
    """"HH:MM" the running scan started at, or None when no scan runs."""
    if _scan_process is not None and _scan_process.poll() is None:
        return f"{_scan_started:%H:%M}"
    for run in storage.get_recent_runs(1):
        started = datetime.fromisoformat(run["started_at"])
        if run["status"] == "running" and datetime.now() - started < _RUNNING_ROW_MAX_AGE:
            return f"{started:%H:%M}"
    return None


def fresh_progress() -> str | None:
    """The running scan's progress line (telegram_notifier.format_run_progress), or None when stale."""
    try:
        progress = json.loads(storage.get_bot_state(storage.SCAN_PROGRESS_KEY) or "{}")
        updated = datetime.fromisoformat(progress["updated"])
    except (ValueError, KeyError):
        return None
    return progress.get("text") if datetime.now() - updated < _PROGRESS_MAX_AGE else None


def paused_since() -> str | None:
    return storage.get_bot_state(storage.SCANS_PAUSED_KEY) or None


def last_finished_run() -> dict | None:
    return next((run for run in storage.get_recent_runs(10) if run["status"] != "running"), None)


def scheduled_scan_pid() -> int | None:
    # A scan started elsewhere (the Task Scheduler's) is known only by the pid apartment_bot.py recorded
    # with its run id; it counts only while that run's row is still a live "running" one, so a stale
    # pid (reused by an unrelated process since) is never killed.
    runs = storage.get_recent_runs(1)
    recorded = storage.get_bot_state(storage.SCAN_PID_KEY) or ""
    run_id, _, pid = recorded.partition(":")
    if not runs or not pid.isdigit() or str(runs[0]["id"]) != run_id:
        return None
    started = datetime.fromisoformat(runs[0]["started_at"])
    if runs[0]["status"] != "running" or datetime.now() - started >= _RUNNING_ROW_MAX_AGE:
        return None
    return int(pid)


def run_cli(args: list[str], timeout_s: int = _CLI_TIMEOUT_S) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of a project script run to completion with this Python; -1 on timeout."""
    try:
        result = subprocess.run(
            [sys.executable, *args], cwd=PROJECT_DIR, env=CHILD_ENV, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout_s, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"(stopped after {timeout_s} seconds)"
    return result.returncode, result.stdout, result.stderr


def _watch_exit(process: subprocess.Popen, on_failure):
    code = process.wait()
    if code not in (0, 2) and not _stop_requested and on_failure:
        on_failure(code)


def _kill_process_tree(pid: int):
    # Playwright's driver and Chromium are child processes; killing only Python would leave them
    # holding chrome_profile's lock, and the next scan would fail to launch.
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, 9)


def _close_running_rows():
    # A killed scan never reaches finish_run(); left "running", its row would read as a scan in
    # progress for the next _RUNNING_ROW_MAX_AGE.
    for run in storage.get_recent_runs(5):
        if run["status"] == "running":
            storage.finish_run(run["id"], "stopped", {}, 0, 0)
