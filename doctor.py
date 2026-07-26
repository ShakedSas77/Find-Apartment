"""
=== doctor ===

One command that checks everything the bot depends on and tells you how to
fix whatever's broken, instead of finding out mid-scheduled-run.

    python doctor.py            # PASS/FAIL/WARN table + remediation
    python doctor.py --alert    # also DM a Telegram alert if anything FAILs

Deliberately does NOT call Google Geocoding or Distance Matrix — both share
GMAPS_MONTHLY_CAP's counter (see apartment_bot.py get_walking_distance), and a
health check burning production quota would defeat its own purpose. Gmaps is
therefore a presence-only check (key set), not a live call.
"""

import json
import os
import sqlite3
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import storage

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def _check_env():
    missing = [name for name in ("GEMINI_API_KEY", "GMAPS_API_KEY", "SHEET_ID") if not os.getenv(name)]
    if missing:
        return ("env", FAIL, f"missing: {', '.join(missing)}", "add the missing key(s) to .env")
    return ("env", PASS, "GEMINI_API_KEY / GMAPS_API_KEY / SHEET_ID present", "")


def _check_credentials_file():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.CREDENTIALS_FILE)
    if not os.path.exists(path):
        return ("credentials.json", FAIL, "missing", "download the service-account key from Google Cloud Console")
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
        return ("credentials.json", PASS, "present + parses", "")
    except Exception as exc:
        return ("credentials.json", FAIL, f"unparseable: {exc}", "re-download the service-account key")


def _check_chrome_profile():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profile")
    if not os.path.isdir(path):
        return ("chrome_profile/", WARN, "not created yet", "created automatically on the first `--live` run (manual FB login)")
    return ("chrome_profile/", PASS, "present", "")


def _check_config_local():
    placeholder = [
        "https://www.facebook.com/groups/000000000000001",
        "https://www.facebook.com/groups/000000000000002",
        "https://www.facebook.com/groups/000000000000003",
    ]
    if config.TARGET_URLS == placeholder:
        return ("config_local.py", FAIL, "TARGET_URLS still the placeholder", "add your real FB group URLs to config_local.py")
    return ("config_local.py", PASS, f"{len(config.TARGET_URLS)} group URL(s) configured", "")


def _check_db():
    if not storage.DB_PATH.exists():
        return ("bot_data.db", WARN, "not created yet", "created automatically on first run")
    try:
        with sqlite3.connect(storage.DB_PATH) as conn:
            n = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        return ("bot_data.db", PASS, f"{n} posts", "")
    except Exception as exc:
        return ("bot_data.db", FAIL, f"unreadable: {exc}", "restore from a backup_db.py snapshot")


def _check_gemini():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return ("gemini", FAIL, "GEMINI_API_KEY missing", "add it to .env")
    try:
        from google import genai
        client = genai.Client(api_key=key)
        list(client.models.list())  # metadata call, not generation — doesn't spend quota
        return ("gemini", PASS, f"reachable, model={config.GEMINI_MODEL}", "")
    except Exception as exc:
        return ("gemini", FAIL, f"unreachable: {exc}", "check GEMINI_API_KEY / network")


def _ollama_base() -> str:
    return os.environ.get("LLM_BASE_URL", "http://localhost:11434")


def _check_ollama():
    try:
        ok = requests.get(f"{_ollama_base()}/api/tags", timeout=6).status_code == 200
    except Exception:
        ok = False
    if ok:
        return ("ollama", PASS, f"{_ollama_base()} reachable", "")
    return ("ollama", WARN, f"{_ollama_base()} unreachable", "start Ollama if you rely on the Gemini-quota fallback (`ollama serve`)")


def _check_sheets():
    sheet_id = os.getenv("SHEET_ID")
    if not sheet_id:
        return ("google_sheets", FAIL, "SHEET_ID missing", "add it to .env")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(config.CREDENTIALS_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        headers = gc.open_by_key(sheet_id).sheet1.row_values(1)
        return ("google_sheets", PASS, f"reachable, {len(headers)} header(s)", "")
    except Exception as exc:
        return ("google_sheets", FAIL, f"unreachable: {exc}",
                 "check SHEET_ID / credentials.json / that the sheet is shared with the service account email")


def _check_gmaps():
    if not os.getenv("GMAPS_API_KEY"):
        return ("google_maps", FAIL, "GMAPS_API_KEY missing", "add it to .env")
    return ("google_maps", PASS, "key present (not live-tested — shares the monthly quota)", "")


def _check_telegram():
    if not config.TELEGRAM_ENABLED:
        return ("telegram", WARN, "TELEGRAM_ENABLED=False", "optional — set True + TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in .env to enable")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return ("telegram", FAIL, "TELEGRAM_ENABLED=True but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing", "add them to .env")
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=6)
        if resp.json().get("ok"):
            return ("telegram", PASS, "bot token valid", "")
        return ("telegram", FAIL, "getMe rejected the token", "check TELEGRAM_BOT_TOKEN")
    except Exception as exc:
        return ("telegram", FAIL, f"unreachable: {exc}", "check network / TELEGRAM_BOT_TOKEN")


_CHECKS = [
    _check_env, _check_credentials_file, _check_chrome_profile, _check_config_local,
    _check_db, _check_gemini, _check_ollama, _check_sheets, _check_gmaps, _check_telegram,
]


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    alert = "--alert" in argv

    results = [check() for check in _CHECKS]

    print("=== doctor ===")
    any_fail = False
    for name, status, detail, remediation in results:
        print(f"  [{status:4}] {name:20} {detail}")
        if remediation:
            print(f"           -> {remediation}")
        if status == FAIL:
            any_fail = True

    if alert and any_fail:
        try:
            import telegram_notifier
            summary = "\n".join(
                f"[{status}] {name}: {detail}" for name, status, detail, _ in results if status == FAIL
            )
            telegram_notifier.send_failure_alert(summary)
        except Exception as exc:
            print(f"(could not send Telegram alert: {exc})")

    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
