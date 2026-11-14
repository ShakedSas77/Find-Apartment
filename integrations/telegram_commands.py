"""
Chat commands for the Telegram bot, handled by bot_listener.py: scans (full, Facebook only, Yad2
only, or one group picked from a list), /stop, /status, /new, /groups, /pause and /resume for the
scheduled scans, and Hebrew summaries of /stats and /doctor. /help shows them all as buttons.

Only the configured chat (TELEGRAM_CHAT_ID) is obeyed, and only the fixed commands in COMMANDS,
never free-form arguments: a message's text never reaches a command line. The one value a command
line does get, a group link for a one-group scan, comes from the local Groups mirror, picked by a
button.
"""
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import GROUP_YIELD_MIN_HISTORY
from core import storage
from integrations import telegram_notifier

COMMANDS = {
    "scan": "סריקה מלאה (פייסבוק + יד2)",
    "scan_fb": "סריקת קבוצות הפייסבוק בלבד",
    "scan_yad2": "קריאת התראות יד2 מהמייל בלבד",
    "scan_group": "סריקת קבוצה אחת, לבחירה מרשימה",
    "stop": "עצירת הסריקה שרצה עכשיו",
    "status": "האם סריקה רצה, וכמה התקדמה",
    "new": "דירות שנוספו ב-24 השעות האחרונות",
    "groups": "קבוצות שדורשות טיפול",
    "pause": "השהיית הסריקות המתוזמנות",
    "resume": "חידוש הסריקות המתוזמנות",
    "stats": "סטטיסטיקה",
    "doctor": "בדיקת תקינות של כל השירותים",
    "sync_groups": "סנכרון לשונית Groups ומיון לפי אחוז התאמה",
    "help": "תפריט הפקודות",
}
# Arguments apartment_bot.py gets for each scan command, and how the reply names the scan.
_SCAN_COMMANDS = {
    "scan": ([], "סריקה מלאה"),
    "scan_fb": (["--source", "fb"], "סריקת פייסבוק"),
    "scan_yad2": (["--source", "yad2"], "קריאת התראות יד2"),
}
_RUN_MODE_LABELS = {"live": "מלאה", "live_fb": "פייסבוק", "live_yad2": "יד2", "live_group": "קבוצה",
                    "dry_run": "ניסיון", "dry_run_fb": "ניסיון", "dry_run_yad2": "ניסיון", "dry_run_group": "ניסיון"}
_RUN_STATUS_ICONS = {"ok": "✅", "running": "⏳", "stopped": "⏹", "checkpoint": "⚠️", "no_groups_scanned": "⚠️",
                     "login_required": "🔑", "crashed": "❌"}
_DOCTOR_ICONS = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_COMMAND_RE = re.compile(r"^/([a-z0-9_]+)(?:@\w+)?(?:\s|$)")
# Telegram caps a message at 4096 characters; the rest is room for the <pre> wrapper and the title.
_OUTPUT_LIMIT = 3500
_CLI_TIMEOUT_S = 300
# getUpdates hands over whatever arrived while the listener was down; an old /scan must not fire hours later.
_STALE_COMMAND_S = 300
# A run row still "running" after this long was killed without closing it, not a scan in progress.
_RUNNING_ROW_MAX_AGE = timedelta(hours=2)
# The scan rewrites its progress every PROGRESS_TICK_S (30s); older than this, it's from a scan that died.
_PROGRESS_MAX_AGE = timedelta(minutes=3)
_NEW_LISTINGS_HOURS = 24
_NEW_LISTINGS_MAX = 15
# Telegram's callback_data limit is 64 bytes; "sg:" plus a numeric group id fits, a long vanity slug may not.
_CALLBACK_DATA_MAX_BYTES = 64
_GROUP_BUTTON_MAX_CHARS = 40
# The child prints Hebrew and ₪; with stdout not a console, Windows Python would default to cp1252 and crash.
_CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_scan_lock = threading.Lock()
_scan_process: subprocess.Popen | None = None
_stop_requested = False


def handle_message(message: dict, allowed_chat_id: str, send):
    """
    Runs the command in a Telegram message, if any. send(html_text, reply_markup=None) replies to the
    chat. Slow commands run on a thread, so the listener keeps answering button taps meanwhile.
    """
    command = parse_command(message.get("text") or "")
    if not command or str(message.get("chat", {}).get("id")) != str(allowed_chat_id):
        return
    if time.time() - message.get("date", 0) > _STALE_COMMAND_S:
        return
    run_command(command, send)


def handle_button(data: str, send):
    """
    A tap on one of this module's buttons: "cmd:<command>" runs that command, "sg:<group id>" scans
    that group. The caller (bot_listener.py) has already checked the tap came from the allowed chat.
    """
    kind, _, value = data.partition(":")
    if kind == "cmd" and value in COMMANDS:
        run_command(value, send)
    elif kind == "sg":
        group = next((g for g in storage.get_groups() if group_id(g["url"]) == value), None)
        if group is None:
            send("הקבוצה כבר לא ברשימת הקבוצות — שלחו /scan_group לרשימה מעודכנת")
            return
        _start_scan(send, ["--group", group["url"]], f"סריקת הקבוצה {html.escape(group['name'] or value)}")


def run_command(command: str, send):
    if command in _SCAN_COMMANDS:
        args, label = _SCAN_COMMANDS[command]
        _start_scan(send, args, label)
    elif command == "scan_group":
        send(*format_group_picker(storage.get_groups()))
    elif command == "stop":
        _stop_scan(send)
    elif command == "status":
        send(format_status(_running_since(), _last_finished_run(), _fresh_progress(), _paused_since()))
    elif command == "new":
        since = (datetime.now() - timedelta(hours=_NEW_LISTINGS_HOURS)).isoformat(timespec="seconds")
        send(format_new_listings(storage.get_run_activity(since)["added"]))
    elif command == "groups":
        send(format_problem_groups(storage.get_groups(), storage.get_group_overview()))
    elif command == "pause":
        storage.set_bot_state(storage.SCANS_PAUSED_KEY, datetime.now().isoformat(timespec="seconds"))
        send("⏸ הסריקות המתוזמנות מושהות. אפשר עדיין לסרוק ידנית מכאן; /resume מחדש אותן.")
    elif command == "resume":
        storage.set_bot_state(storage.SCANS_PAUSED_KEY, "")
        send("▶️ הסריקות המתוזמנות חודשו.")
    elif command == "stats":
        counts, _, gmaps_calls = storage.get_stats()
        week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
        send(format_stats(counts, len(storage.get_run_activity(week_ago)["added"]), gmaps_calls,
                          storage.get_recent_runs(5), storage.get_groups()))
    elif command == "doctor":
        threading.Thread(target=_run_doctor, args=(send,), daemon=True).start()
    elif command == "sync_groups":
        threading.Thread(target=_run_sync_groups, args=(send,), daemon=True).start()
    else:
        send(format_help(), menu_keyboard(bool(_paused_since())))


def parse_command(text: str) -> str | None:
    """'/scan' or '/scan@SomeBot' (how a group chat sends it) -> 'scan'; None when not a command."""
    match = _COMMAND_RE.match(text.strip())
    return match.group(1) if match else None


def group_id(group_url: str) -> str:
    """'https://www.facebook.com/groups/123' (the stored canonical form) -> '123'."""
    return group_url.rstrip("/").rsplit("/", 1)[-1]


def format_help() -> str:
    return "<b>פקודות</b> — אפשר ללחוץ על כפתור או לשלוח את הפקודה\n" + "\n".join(
        f"/{name} — {html.escape(text)}" for name, text in COMMANDS.items())


def menu_keyboard(is_paused: bool) -> dict:
    """The /help menu: every command as a button, with pause or resume depending on the current state."""
    def button(text, command):
        return {"text": text, "callback_data": f"cmd:{command}"}
    return {"inline_keyboard": [
        [button("▶️ סריקה מלאה", "scan"), button("⏹ עצירה", "stop")],
        [button("📘 רק פייסבוק", "scan_fb"), button("📧 רק יד2", "scan_yad2"), button("👥 קבוצה אחת", "scan_group")],
        [button("ℹ️ סטטוס", "status"), button("🆕 חדשות", "new"), button("📊 סטטיסטיקה", "stats")],
        [button("🩺 בדיקת תקינות", "doctor"), button("🚩 קבוצות", "groups"), button("🔄 סנכרון קבוצות", "sync_groups")],
        [button("▶️ חידוש סריקות מתוזמנות", "resume") if is_paused else button("⏸ השהיית סריקות מתוזמנות", "pause")],
    ]}


def format_group_picker(groups: list[dict]) -> tuple[str, dict | None]:
    """The active groups as buttons, one per row in the Groups tab's order (by match rate after /sync_groups)."""
    rows = []
    for group in groups:
        data = f"sg:{group_id(group['url'])}"
        if not group["active"] or len(data.encode()) > _CALLBACK_DATA_MAX_BYTES:
            continue
        name = group["name"] or group["cities"] or group_id(group["url"])
        if len(name) > _GROUP_BUTTON_MAX_CHARS:
            name = name[:_GROUP_BUTTON_MAX_CHARS - 1] + "…"
        rows.append([{"text": name, "callback_data": data}])
    if not rows:
        return "אין קבוצות פעילות ברשימה — שלחו /sync_groups", None
    return "👥 איזו קבוצה לסרוק?", {"inline_keyboard": rows}


def format_status(running_since: str | None, last_run: dict | None, progress: str | None = None,
                  paused_since: str | None = None) -> str:
    lines = [f"⏳ סריקה רצה עכשיו (התחילה {running_since})" if running_since else "💤 אין סריקה פעילה"]
    if running_since and progress:
        lines.append(progress)
    if last_run:
        mode = _RUN_MODE_LABELS.get(last_run.get("mode"), last_run.get("mode") or "")
        icon = _RUN_STATUS_ICONS.get(last_run["status"], last_run["status"])
        lines.append(f"⏮ סריקה אחרונה: {last_run['started_at'].replace('T', ' ')[:16]} · {mode} {icon}"
                     f" · {last_run.get('added') or 0} דירות נוספו")
    if paused_since:
        lines.append(f"⏸ הסריקות המתוזמנות מושהות מאז {paused_since.replace('T', ' ')[:16]} (/resume)")
    return "\n".join(lines)


def format_new_listings(added: list[dict]) -> str:
    """added: storage.get_run_activity()["added"] rows, oldest first; shown newest first."""
    if not added:
        return f"🆕 לא נוספו דירות ב-{_NEW_LISTINGS_HOURS} השעות האחרונות"
    newest = list(reversed(added))
    lines = [f"🆕 <b>{len(added)} דירות נוספו ב-{_NEW_LISTINGS_HOURS} השעות האחרונות</b>"]
    lines += [telegram_notifier._run_listing_line(listing) for listing in newest[:_NEW_LISTINGS_MAX]]
    if len(added) > _NEW_LISTINGS_MAX:
        lines.append(f"  ועוד {len(added) - _NEW_LISTINGS_MAX} — בגיליון")
    return "\n".join(lines)


def format_problem_groups(groups: list[dict], overview: dict[str, dict]) -> str:
    """
    Active groups that need a hand: not joined, no posts found, never scanned, or at least
    GROUP_YIELD_MIN_HISTORY posts with no match (worth pausing in the Groups tab).
    """
    sections = {"not_member": [], "no_posts": [], "no_matches": [], "unscanned": []}
    for group in groups:
        if not group["active"]:
            continue
        seen = overview.get(group["url"], {})
        link = f'<a href="{html.escape(group["url"], quote=True)}">' \
               f'{html.escape(group["name"] or seen.get("name") or group_id(group["url"]))}</a>'
        if seen.get("status") == "not_member":
            sections["not_member"].append(link)
        elif seen.get("status") == "no_posts":
            sections["no_posts"].append(link)
        elif not seen.get("last_scanned"):
            sections["unscanned"].append(link)
        elif seen.get("posts", 0) >= GROUP_YIELD_MIN_HISTORY and not seen.get("added"):
            sections["no_matches"].append(f"{link} ({seen['posts']:,} פוסטים)")
    titles = {
        "not_member": "🚪 לא חבר/ה בקבוצה — להצטרף או להשהות",
        "no_posts": "📭 לא נמצאו פוסטים",
        "no_matches": "🥱 הרבה פוסטים, אף התאמה — כדאי להשהות",
        "unscanned": "⏳ ממתינות לסריקה ראשונה",
    }
    lines = [f"<b>{titles[key]}</b>\n" + "\n".join(f"• {item}" for item in items)
             for key, items in sections.items() if items]
    if not lines:
        return "✅ כל הקבוצות הפעילות תקינות"
    paused = sum(1 for group in groups if not group["active"])
    footer = f"\n\n⏸ {paused} קבוצות מושהות בלשונית Groups" if paused else ""
    return "🚩 <b>קבוצות שדורשות טיפול</b>\n\n" + "\n\n".join(lines) + footer


def format_stats(counts: dict, added_this_week: int, gmaps_calls: int, recent_runs: list[dict],
                 groups: list[dict]) -> str:
    """counts: storage.get_stats()'s verdict counts; recent_runs: storage.get_recent_runs(), newest first."""
    total_posts = sum(counts.values())
    lines = [
        "📊 <b>סטטיסטיקה</b>",
        f"🏠 {counts.get(storage.VERDICT_ADDED, 0):,} דירות נוספו בסך הכל · {added_this_week} בשבוע האחרון",
        f"🔎 {total_posts:,} פוסטים במאגר · {counts.get(storage.VERDICT_PREFILTERED, 0):,} סוננו בלי AI",
        f"👥 {sum(1 for g in groups if g['active'])} קבוצות פעילות מתוך {len(groups)}",
    ]
    if gmaps_calls:
        lines.append(f"🗺 {gmaps_calls} קריאות Google Maps החודש")
    if recent_runs:
        lines.append("\n<b>סריקות אחרונות</b>")
        lines += [_run_line(run) for run in recent_runs]
    return "\n".join(lines)


def format_doctor(results: list[list[str]]) -> str:
    """results: doctor.py --json rows, [name, status, detail, remediation]."""
    failures = sum(1 for _, status, _, _ in results if status == "FAIL")
    lines = ["🩺 <b>הכל תקין</b>" if not failures else f"🩺 <b>{failures} תקלות</b>"]
    for name, status, detail, remediation in results:
        line = f"{_DOCTOR_ICONS.get(status, status)} {html.escape(name)}: {html.escape(detail)}"
        if status == "FAIL" and remediation:
            line += f"\n    ← {html.escape(remediation)}"
        lines.append(line)
    return "\n".join(lines)


def format_cli_output(command: str, output: str) -> str:
    if len(output) > _OUTPUT_LIMIT:
        output = output[:_OUTPUT_LIMIT] + "\n..."
    return f"<b>/{command}</b>\n<pre>{html.escape(output)}</pre>"


def _run_line(run: dict) -> str:
    started = datetime.fromisoformat(run["started_at"])
    duration = ""
    if run.get("finished_at"):
        minutes = (datetime.fromisoformat(run["finished_at"]) - started).total_seconds() / 60
        duration = f" · {minutes:.0f} דק'"
    mode = _RUN_MODE_LABELS.get(run.get("mode"), run.get("mode") or "")
    icon = _RUN_STATUS_ICONS.get(run["status"], run["status"])
    return f"{icon} {started:%d/%m %H:%M} · {mode} · {run.get('added') or 0} נוספו{duration}"


def _start_scan(send, args: list[str], label: str):
    global _scan_process, _stop_requested
    with _scan_lock:
        running_since = _running_since()
        if running_since:
            send(f"⏳ סריקה כבר רצה (התחילה {running_since})")
            return
        _stop_requested = False
        _scan_process = subprocess.Popen(
            [sys.executable, "-u", "apartment_bot.py", *args], cwd=_PROJECT_DIR, env=_CHILD_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        process = _scan_process
    send(f"▶️ מפעיל {label} — הודעת ההתחלה עם ההתקדמות תגיע בעוד רגע")
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
        process = _scan_process if _scan_process is not None and _scan_process.poll() is None else None
        pid = process.pid if process else _scheduled_scan_pid()
        if pid is None:
            send("אין סריקה פעילה")
            return
        _stop_requested = True
        _kill_process_tree(pid)
        if process:
            process.wait()
        _close_running_rows()
    send("⏹ הסריקה נעצרה")


def _scheduled_scan_pid() -> int | None:
    # A scan this listener didn't start (the Task Scheduler's) is known only by the pid apartment_bot.py
    # recorded with its run id; it counts only while that run's row is still a live "running" one, so a
    # stale pid (reused by an unrelated process since) is never killed.
    runs = storage.get_recent_runs(1)
    recorded = storage.get_bot_state(storage.SCAN_PID_KEY) or ""
    run_id, _, pid = recorded.partition(":")
    if not runs or not pid.isdigit() or str(runs[0]["id"]) != run_id:
        return None
    started = datetime.fromisoformat(runs[0]["started_at"])
    if runs[0]["status"] != "running" or datetime.now() - started >= _RUNNING_ROW_MAX_AGE:
        return None
    return int(pid)


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


def _running_since() -> str | None:
    # A scan started from here, or one the Task Scheduler started (seen only through its run row).
    if _scan_process is not None and _scan_process.poll() is None:
        return "מהבוט"
    for run in storage.get_recent_runs(1):
        started = datetime.fromisoformat(run["started_at"])
        if run["status"] == "running" and datetime.now() - started < _RUNNING_ROW_MAX_AGE:
            return f"{started:%H:%M}"
    return None


def _fresh_progress() -> str | None:
    try:
        progress = json.loads(storage.get_bot_state(storage.SCAN_PROGRESS_KEY) or "{}")
        updated = datetime.fromisoformat(progress["updated"])
    except (ValueError, KeyError):
        return None
    return progress.get("text") if datetime.now() - updated < _PROGRESS_MAX_AGE else None


def _paused_since() -> str | None:
    return storage.get_bot_state(storage.SCANS_PAUSED_KEY) or None


def _last_finished_run() -> dict | None:
    return next((run for run in storage.get_recent_runs(10) if run["status"] != "running"), None)


def _run_doctor(send):
    send("⏳ בודק את כל השירותים...")
    stdout, stderr = _run_cli(["doctor.py", "--json"])
    try:
        send(format_doctor(json.loads(stdout)))
    except ValueError:
        send(format_cli_output("doctor", (stdout + stderr).strip() or "(אין פלט)"))


def _run_sync_groups(send):
    send("⏳ מסנכרן את לשונית Groups...")
    stdout, stderr = _run_cli(["apartment_bot.py", "--sync-groups"])
    send(format_cli_output("sync_groups", (stdout + stderr).strip() or "(אין פלט)"))


def _run_cli(args: list[str]) -> tuple[str, str]:
    """(stdout, stderr) of a project script run with the listener's Python."""
    try:
        result = subprocess.run(
            [sys.executable, *args], cwd=_PROJECT_DIR, env=_CHILD_ENV, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=_CLI_TIMEOUT_S, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "", f"(נעצר אחרי {_CLI_TIMEOUT_S} שניות)"
    return result.stdout, result.stderr
