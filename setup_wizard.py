"""
=== Setup wizard ===

Asks a few questions and writes the answers: settings to config_local.py, secrets (keys, passwords)
to .env — both gitignored. Lines it doesn't own in either file are kept as they are. Re-run it any
time; `--add <step>` runs one step, e.g. to switch on an add-on later.

    python setup_wizard.py                 # the basic setup: search, AI, results, Facebook
    python setup_wizard.py --add telegram  # one step: search ai results facebook telegram yad2 maps schedule
"""
import argparse
import ast
import getpass
import imaplib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import requests
from google import genai

import config
from core import storage
from integrations.distance_fallback import nominatim_geocode
from integrations.groups_sync import add_groups, canonical_group_url
from integrations.sheets import open_spreadsheet
from sources.fb_scraper import log_in_to_facebook
from sources.mail_source import _quote_folder

_PROJECT_DIR = Path(__file__).resolve().parent
_CONFIG_LOCAL = _PROJECT_DIR / "config_local.py"
_ENV_FILE = _PROJECT_DIR / ".env"
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_CHAT_WAIT_S = 120


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set up the apartment bot, or add one feature to it.")
    parser.add_argument("--add", choices=list(STEPS), help="run only this step")
    args = parser.parse_args(argv)
    os.chdir(_PROJECT_DIR)
    storage.init_db()  # a fresh install has no bot_data.db yet, and the Groups step records groups in it
    print("=== Apartment bot setup ===  (Enter keeps the value in [brackets]; Ctrl+C quits, saved answers stay)\n")
    steps = [args.add] if args.add else ["search", "ai", "results", "facebook"]
    for name in steps:
        print(f"\n--- {STEPS[name][0]} ---")
        STEPS[name][1]()
    if not args.add:
        _offer_add_ons()
        _finish()


# ─── Steps ────────────────────────────────────────────────────────────────────

def step_search():
    print("What are you looking for? Listings outside these are skipped.")
    min_rooms = ask_number("Fewest rooms", config.MIN_ROOMS)
    max_rooms = ask_number("Most rooms", config.MAX_ROOMS)
    min_price = int(ask_number("Lowest rent, ₪ a month", config.MIN_PRICE))
    max_price = int(ask_number("Highest rent, ₪ a month", config.MAX_PRICE))
    max_km = ask_number("Longest walk to your destination, km", config.MAX_WALKING_DISTANCE_KM)
    settings = {"MIN_ROOMS": min_rooms, "MAX_ROOMS": max_rooms, "MIN_PRICE": min_price, "MAX_PRICE": max_price,
                "MAX_WALKING_DISTANCE_KM": max_km}
    print("\nWalking distance is measured to one address: your work, a school, a parent's home...")
    # config.py's own value is an example, not a default worth keeping: only a saved address is offered.
    current = config.DESTINATION_ADDRESS if _is_saved("DESTINATION_ADDRESS") else None
    while True:
        address = ask("Destination address (street and number, city)", current)
        if address == current:
            break
        found = nominatim_geocode(address)
        if found is None:
            print("Couldn't find that address on the map - try writing it differently, with the city name.")
            continue
        lat, lon, _ = found
        print(f"Found it at {lat:.5f}, {lon:.5f} (https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=17)")
        if ask_yes_no("Is that the right place?", True):
            # The south-of cutoff in config.py belongs to the original Tel Aviv destination.
            settings.update(DESTINATION_ADDRESS=address, DESTINATION_LAT=lat, DESTINATION_LON=lon,
                            EXCLUDE_SOUTH_OF_LAT=None)
            break
    save_settings(settings)


def step_ai():
    print("The bot uses an AI to read each Facebook post (price, rooms, address).")
    print("  1) Gemini - Google's AI with a free key. Fast and accurate. (recommended)")
    print("  2) Ollama - runs on this computer, no account. Needs a ~5 GB download and a decent PC; slower.")
    choice = ask("Which one?", "1" if config.LLM_PROVIDER == "gemini" else "2")
    if choice == "2":
        _set_up_ollama()
        save_settings({"LLM_PROVIDER": "ollama"})
        return
    print("\nGet a free key: sign in with a Google account, click 'Create API key', copy it.")
    _open("https://aistudio.google.com/app/apikey")
    while True:
        key = ask_secret("Paste the Gemini key", os.getenv("GEMINI_API_KEY"))
        if _gemini_key_works(key):
            break
        print("That key didn't work - check you copied all of it.")
    save_secrets({"GEMINI_API_KEY": key})
    save_settings({"LLM_PROVIDER": "gemini"})
    print("Saved. (If you also install Ollama, it takes over when Gemini's free quota runs out.)")


def step_results():
    print("Where should the listings go?")
    print(f"  1) An Excel file on this computer ({config.EXCEL_PATH}) - nothing to set up. (recommended)")
    print("  2) A Google Sheet - open it from any device and share it; takes ~10 minutes to set up.")
    if ask("Which one?", "2" if config.OUTPUT_BACKEND == "sheets" else "1") != "2":
        save_settings({"OUTPUT_BACKEND": "excel"})
        print(f"Listings will be saved to {_PROJECT_DIR / config.EXCEL_PATH}")
        return
    _set_up_google_sheet()


def step_facebook():
    print("The bot reads apartment groups on Facebook.")
    print("  1) Log in with your Facebook account (in the bot's own browser window, once). (recommended)")
    print("  2) No login - only what Facebook shows visitors. Usually it asks to log in and shows nothing,")
    print("     so most groups will be marked 'needs login'.")
    if ask("Which one?", "1" if config.FACEBOOK_LOGIN else "2") == "2":
        save_settings({"FACEBOOK_LOGIN": False})
    else:
        save_settings({"FACEBOOK_LOGIN": True})
        if ask_yes_no("Open the browser to log in now?", not (_PROJECT_DIR / "chrome_profile").exists()):
            log_in_to_facebook()
    print("\nWhich groups? Open each group on Facebook and copy the address bar (facebook.com/groups/...).")
    print("Paste one link per line; an empty line when done.")
    links = []
    while line := input("> ").strip():
        if canonical_group_url(line):
            links.append(line)
        else:
            print("  That isn't a Facebook group link - skipped.")
    if links:
        added = add_groups(open_spreadsheet(), links)
        print(f"Added {len(added)} group(s) to the Groups tab. Pause one later by writing 'לא' in its פעיל column.")


def step_telegram():
    print("Telegram sends each new listing to your phone (or to a group chat you share with a partner),")
    print("and lets you run the bot from the chat (/scan, /status, /new...).")
    print("1. In Telegram, open @BotFather, send /newbot, pick a name; it replies with a token.")
    _open("https://t.me/BotFather")
    while True:
        token = ask_secret("Paste the bot token", os.getenv("TELEGRAM_BOT_TOKEN"))
        me = _telegram(token, "getMe")
        if me:
            break
        print("Telegram didn't accept that token - copy the whole line BotFather sent.")
    print(f"2. Open your bot (@{me['username']}) and send it any message. For a group chat: add the bot to")
    print("   the group and send a message there.")
    chat_id = _wait_for_chat(token)
    if chat_id is None:
        print("No message arrived - run `python setup_wizard.py --add telegram` to try again.")
        return
    _telegram(token, "sendMessage", {"chat_id": chat_id, "text": "✅ הבוט מחובר — דירות חדשות יגיעו לכאן"})
    save_secrets({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat_id)})
    save_settings({"TELEGRAM_ENABLED": True})
    print("Connected - check the chat for a test message.")
    if ask_yes_no("Also answer chat commands (/scan, /status...) whenever this PC is on?", True):
        _add_listener_to_startup()


def step_yad2():
    print("Yad2 can email you new listings for a saved search; the bot reads those emails (read-only).")
    print("1. On yad2.co.il, save your search with email alerts on.")
    print("2. In Gmail, put those emails under a label (a filter on the sender alerts@yad2.co.il), e.g. 'yad2'.")
    print("3. Create a Gmail app password (needs 2-step verification on the Google account).")
    _open("https://myaccount.google.com/apppasswords")
    user = ask("Gmail address", os.getenv("ALERTS_IMAP_USER"))
    folder = ask("Gmail label with the Yad2 emails", os.getenv("ALERTS_IMAP_FOLDER") or "INBOX")
    while True:
        password = ask_secret("App password (16 letters)", os.getenv("ALERTS_IMAP_PASSWORD")).replace(" ", "")
        problem = _mailbox_problem(user, password, folder)
        if not problem:
            break
        print(f"Couldn't read the mailbox: {problem}")
    save_secrets({"ALERTS_IMAP_USER": user, "ALERTS_IMAP_PASSWORD": password, "ALERTS_IMAP_FOLDER": folder})
    save_settings({"ALERTS_SCAN_ENABLED": True})
    print("Saved - every scan now reads the Yad2 emails too, into a 'Yad2' tab.")


def step_maps():
    print("Without Google Maps, walking distance is a free estimate shown with its error (e.g. ~1.2 km ±0.3).")
    print("A Google Maps key gives the real walking route. Google asks for a billing account, but the bot")
    print(f"stops at {config.GMAPS_MONTHLY_CAP} calls a month, inside Google's free monthly credit.")
    print("Create a key with the 'Distance Matrix API' and 'Geocoding API' enabled:")
    _open("https://console.cloud.google.com/apis/credentials")
    key = ask_secret("Paste the Google Maps key", os.getenv("GMAPS_API_KEY"))
    save_secrets({"GMAPS_API_KEY": key})
    save_settings({"GMAPS_ENABLED": True})


def step_schedule():
    print("Windows can run a scan by itself every day (when the PC is on).")
    times = ask("At what times? (24h, comma separated)", "11:00, 20:00")
    vbs = _PROJECT_DIR / "run_hidden.vbs"
    for at in [t.strip() for t in times.split(",") if t.strip()]:
        if not re.fullmatch(r"\d{1,2}:\d{2}", at):
            print(f"  '{at}' isn't a time like 20:00 - skipped.")
            continue
        name = f"ApartmentBot {at.replace(':', '')}"
        result = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", at.zfill(5), "/TN", name,
                                 "/TR", f'wscript.exe "{vbs}"'], capture_output=True, text=True)
        print(f"  {name}: {'scheduled' if result.returncode == 0 else result.stderr.strip()}")
    print("Pause or resume them from Telegram (/pause, /resume), or in Windows' Task Scheduler.")


STEPS = {
    "search": ("What you're looking for", step_search),
    "ai": ("The AI that reads posts", step_ai),
    "results": ("Where the listings go", step_results),
    "facebook": ("Facebook groups", step_facebook),
    "telegram": ("Telegram alerts (add-on)", step_telegram),
    "yad2": ("Yad2 email alerts (add-on)", step_yad2),
    "maps": ("Google Maps walking routes (add-on)", step_maps),
    "schedule": ("Daily automatic scans (add-on)", step_schedule),
}


# ─── Writing the answers ──────────────────────────────────────────────────────

def save_settings(values: dict, path: Path = _CONFIG_LOCAL):
    """Sets each NAME = value in config_local.py: an existing assignment (even one spanning several lines)
    is replaced in place, a new one appended; everything else in the file is kept."""
    text = path.read_text(encoding="utf-8") if path.exists() else (
        '"""Your settings (written by setup_wizard.py; gitignored). Anything here overrides config.py."""\n')
    lines = text.splitlines()
    spans = {target.id: (node.lineno, node.end_lineno)
             for node in ast.parse(text).body if isinstance(node, ast.Assign)
             for target in node.targets if isinstance(target, ast.Name)}
    for name, value in sorted(values.items(), key=lambda item: -spans.get(item[0], (0, 0))[0]):
        line = f"{name} = {value!r}"
        if name in spans:
            start, end = spans[name]
            lines[start - 1:end] = [line]
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, value in values.items():
        setattr(config, name, value)


def save_secrets(values: dict, path: Path = _ENV_FILE):
    """Sets each NAME=value line in .env, replacing an existing one and keeping every other line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for name, value in values.items():
        new_line = f"{name}={value}"
        index = next((i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == name), None)
        if index is None:
            lines.append(new_line)
        else:
            lines[index] = new_line
        os.environ[name] = value
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_saved(name: str, path: Path = _CONFIG_LOCAL) -> bool:
    """Whether config_local.py sets `name`, as opposed to config.py's example value."""
    if not path.exists():
        return False
    return any(isinstance(target, ast.Name) and target.id == name
               for node in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(node, ast.Assign)
               for target in node.targets)


# ─── Prompts ──────────────────────────────────────────────────────────────────

def ask(question: str, default=None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if answer or default not in (None, ""):
            return answer or str(default)


def ask_secret(question: str, current: str | None) -> str:
    # Typed text stays hidden; Enter keeps the saved value.
    suffix = " [Enter keeps the saved one]" if current else ""
    while True:
        answer = getpass.getpass(f"{question}{suffix} (hidden while you paste): ").strip()
        if answer or current:
            return answer or current


def ask_number(question: str, default: float) -> float:
    while True:
        answer = ask(question, f"{default:g}")
        try:
            return float(answer)
        except ValueError:
            print("  Please type a number.")


def ask_yes_no(question: str, default: bool) -> bool:
    answer = ask(f"{question} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y") or answer.startswith("כ")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _offer_add_ons():
    print("\n--- Add-ons (each optional; add any later with `python setup_wizard.py --add <name>`) ---")
    for name in ("telegram", "yad2", "maps", "schedule"):
        if ask_yes_no(f"Set up {STEPS[name][0]}?", False):
            print(f"\n--- {STEPS[name][0]} ---")
            STEPS[name][1]()


def _finish():
    print("\n--- Checking everything ---", flush=True)
    subprocess.run([sys.executable, "doctor.py"])
    print("\nSetup done. Run a scan any time by double-clicking run_bot.bat (or `python apartment_bot.py`).")
    if ask_yes_no("Run the first scan now?", True):
        subprocess.run([sys.executable, "apartment_bot.py"])


def _open(url: str):
    print(f"  (opening {url})")
    webbrowser.open(url)


def _gemini_key_works(key: str) -> bool:
    try:
        return any(True for _ in genai.Client(api_key=key).models.list())
    except Exception:
        return False


def _set_up_ollama():
    exe = shutil.which("ollama") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe")
    while not Path(exe).exists() and not shutil.which("ollama"):
        print("Ollama isn't installed. Download and install it, then press Enter.")
        _open("https://ollama.com/download/windows")
        input("Press Enter once Ollama is installed... ")
        exe = shutil.which("ollama") or exe
    print(f"Downloading the AI model ({config.OLLAMA_MODEL}, a few GB - this takes a while once)...")
    subprocess.run([exe, "pull", config.OLLAMA_MODEL])


def _set_up_google_sheet():
    print("1. Create a Google Cloud service account and download its JSON key:")
    print("   console.cloud.google.com -> APIs & Services -> enable 'Google Sheets API' -> Credentials ->")
    print("   Create credentials -> Service account -> its 'Keys' tab -> Add key -> JSON.")
    _open("https://console.cloud.google.com/apis/library/sheets.googleapis.com")
    target = _PROJECT_DIR / config.CREDENTIALS_FILE
    while True:
        source = Path(ask("Path of the downloaded JSON key file", target if target.exists() else None).strip('"'))
        try:
            email = json.loads(source.read_text(encoding="utf-8"))["client_email"]
            break
        except (OSError, ValueError, KeyError):
            print("  That isn't a service-account key file - it should be the .json Google downloaded.")
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    print(f"2. Create a Google Sheet and share it (Editor) with: {email}")
    while True:
        match = _SHEET_ID_RE.search(ask("3. Paste the sheet's link"))
        if match:
            break
        print("  That isn't a Google Sheets link (docs.google.com/spreadsheets/d/...).")
    save_secrets({"SHEET_ID": match.group(1)})
    save_settings({"OUTPUT_BACKEND": "sheets"})
    try:
        title = open_spreadsheet().sheet1.title
        print(f"Connected to the sheet (first tab: {title}).")
    except Exception as e:
        print(f"Couldn't open the sheet yet ({e}) - check it's shared with {email}, then run doctor.py.")


def _telegram(token: str, method: str, payload: dict | None = None):
    try:
        data = requests.post(_TELEGRAM_API.format(token=token, method=method), json=payload or {}, timeout=40).json()
    except (requests.RequestException, ValueError):
        return None
    return data.get("result") if data.get("ok") else None


def _wait_for_chat(token: str) -> int | None:
    print(f"   Waiting up to {_CHAT_WAIT_S // 60} minutes for your message...")
    deadline = time.time() + _CHAT_WAIT_S
    while time.time() < deadline:
        for update in _telegram(token, "getUpdates", {"timeout": 20}) or []:
            chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat")
            if chat:
                print(f"   Got it: {chat.get('title') or chat.get('first_name') or chat['id']}")
                return chat["id"]
    return None


def _add_listener_to_startup():
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    shortcut = startup / "Apartment bot Telegram listener.lnk"
    vbs = _PROJECT_DIR / "run_listener_hidden.vbs"
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{shortcut}'); "
              f"$s.TargetPath = 'wscript.exe'; $s.Arguments = '\"{vbs}\"'; "
              f"$s.WorkingDirectory = '{_PROJECT_DIR}'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)
    subprocess.Popen(["wscript.exe", str(vbs)], cwd=_PROJECT_DIR)
    print("The chat listener runs now and starts with Windows. Send /help in the chat.")


def _mailbox_problem(user: str, password: str, folder: str) -> str | None:
    try:
        with imaplib.IMAP4_SSL(config.ALERTS_IMAP_HOST) as mailbox:
            mailbox.login(user, password)
            status, _ = mailbox.select(_quote_folder(folder), readonly=True)
            return None if status == "OK" else f"no label named '{folder}'"
    except imaplib.IMAP4.error as e:
        return str(e)
    except OSError as e:
        return f"network: {e}"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Your answers so far are saved; run setup_wizard.py again to continue.")
