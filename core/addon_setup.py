"""
The checks and system steps behind setting up the AI and the add-ons — a key that works, the
Telegram chat, the Yad2 mailbox, the daily scans in Task Scheduler, the Google Sheet's key — shared by
the console wizard (setup_wizard.py) and the web app's Settings page. No prompts or UI here.
"""
import csv
import imaplib
import io
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests
from google import genai

import config
from sources.mail_source import _quote_folder

PROJECT_DIR = Path(__file__).resolve().parent.parent
TIME_RE = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")
SCHEDULE_TASK_PREFIX = "ApartmentBot"
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_STARTUP_SHORTCUT = "Apartment bot Telegram listener.lnk"


def gemini_key_works(key: str) -> bool:
    try:
        return any(True for _ in genai.Client(api_key=key).models.list())
    except Exception:
        return False


def ollama_exe() -> str | None:
    """Where Ollama is installed, or None."""
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
    return shutil.which("ollama") or (str(default) if default.exists() else None)


def telegram(token: str, method: str, payload: dict | None = None):
    """A Telegram Bot API call's result, or None when it fails (bad token, network)."""
    try:
        data = requests.post(_TELEGRAM_API.format(token=token, method=method), json=payload or {}, timeout=40).json()
    except (requests.RequestException, ValueError):
        return None
    return data.get("result") if data.get("ok") else None


def find_telegram_chat(token: str, wait_s: int) -> dict | None:
    """The first chat that messages the bot (or adds it to a group) within wait_s: {id, title}."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        for update in telegram(token, "getUpdates", {"timeout": min(20, wait_s)}) or []:
            chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat")
            if chat:
                return {"id": chat["id"], "title": chat.get("title") or chat.get("first_name") or str(chat["id"])}
    return None


def add_listener_to_startup():
    """Starts the Telegram chat listener now and at every Windows login (a Startup-folder shortcut)."""
    vbs = PROJECT_DIR / "run_listener_hidden.vbs"
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{_startup_shortcut()}'); "
              f"$s.TargetPath = 'wscript.exe'; $s.Arguments = '\"{vbs}\"'; "
              f"$s.WorkingDirectory = '{PROJECT_DIR}'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)
    subprocess.Popen(["wscript.exe", str(vbs)], cwd=PROJECT_DIR)


def listener_starts_with_windows() -> bool:
    return _startup_shortcut().exists()


def mailbox_problem(user: str, password: str, folder: str) -> str | None:
    """Why the Yad2 mailbox can't be read (opened read-only), or None when it can."""
    try:
        with imaplib.IMAP4_SSL(config.ALERTS_IMAP_HOST) as mailbox:
            mailbox.login(user, password)
            status, _ = mailbox.select(_quote_folder(folder), readonly=True)
            return None if status == "OK" else f"no label named '{folder}'"
    except imaplib.IMAP4.error as e:
        return str(e)
    except OSError as e:
        return f"network: {e}"


def schedule_scan(at: str) -> str | None:
    """Adds a daily scan at "HH:MM" to Task Scheduler. Returns why it failed, or None."""
    if not TIME_RE.fullmatch(at):
        return f"'{at}' isn't a time like 20:00"
    vbs = PROJECT_DIR / "run_hidden.vbs"
    result = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", at.zfill(5),
                             "/TN", f"{SCHEDULE_TASK_PREFIX} {at.replace(':', '')}", "/TR", f'wscript.exe "{vbs}"'],
                            capture_output=True, text=True)
    return None if result.returncode == 0 else result.stderr.strip()


def scheduled_scans() -> list[dict]:
    """The bot's tasks in Task Scheduler: [{name, next_run}], by name."""
    result = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    tasks = {}
    for row in csv.reader(io.StringIO(result.stdout)):
        name = row[0].lstrip("\\") if row else ""
        # A task with several daily times is listed once per time; one entry per task.
        if len(row) > 1 and name.startswith(SCHEDULE_TASK_PREFIX):
            tasks.setdefault(name, {"name": name, "next_run": row[1]})
    return sorted(tasks.values(), key=lambda t: t["name"])


def remove_scheduled_scan(name: str):
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True)


def service_account_email(key_json: str) -> str | None:
    """The client_email of a Google service-account key file's text, or None when it isn't one."""
    try:
        return json.loads(key_json)["client_email"]
    except (ValueError, KeyError, TypeError):
        return None


def sheet_id_from_link(link: str) -> str | None:
    match = _SHEET_ID_RE.search(link or "")
    return match.group(1) if match else None


def _startup_shortcut() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup" / _STARTUP_SHORTCUT
