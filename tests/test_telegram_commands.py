"""
Tests for the Telegram chat commands: who is obeyed, and what each command replies or starts.
subprocess is the boundary faked here; the run rows go through a real temporary DB.
"""
import json
import sys
import time
from datetime import datetime, timedelta

import pytest

from core import storage
from integrations import telegram_commands
from integrations.telegram_commands import (
    format_cli_output, format_doctor, format_group_picker, format_new_listings, format_problem_groups, format_stats,
    handle_button, handle_message, menu_keyboard, parse_command,
)

_CHAT_ID = "-1001"


class _FakeProcess:
    pid = 4242

    def poll(self):
        return None

    def wait(self):
        return 0


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    monkeypatch.setattr(telegram_commands, "_scan_process", None)
    storage.init_db()


@pytest.fixture
def launched(monkeypatch):
    commands = []
    monkeypatch.setattr(telegram_commands.subprocess, "Popen",
                        lambda args, **kwargs: commands.append(args) or _FakeProcess())
    return commands


def _message(text, chat_id=_CHAT_ID, age_s=0):
    return {"text": text, "chat": {"id": int(chat_id)}, "date": int(time.time() - age_s)}


def _replies_to(text, **message_kwargs):
    sent = []
    handle_message(_message(text, **message_kwargs), _CHAT_ID, lambda text, reply_markup=None: sent.append(text))
    return sent


def _replies_to_button(data):
    sent = []
    handle_button(data, lambda text, reply_markup=None: sent.append(text))
    return sent


def _group(gid, name="", active=True):
    return {"url": f"https://www.facebook.com/groups/{gid}", "name": name, "cities": "", "active": active, "notes": ""}


@pytest.fixture
def killed(monkeypatch):
    commands = []
    monkeypatch.setattr(telegram_commands.sys, "platform", "win32")
    monkeypatch.setattr(telegram_commands.subprocess, "run", lambda args, **kwargs: commands.append(args))
    return commands


def test_a_group_chat_command_addressed_to_the_bot_is_parsed():
    assert parse_command("/scan@ApartmentBot") == "scan"


def test_plain_text_is_not_a_command():
    assert parse_command("מה עם הדירה ברינגלבלום?") is None


def test_a_command_from_another_chat_is_ignored(launched):
    _replies_to("/scan", chat_id="-999")

    assert launched == []


def test_a_command_older_than_the_stale_limit_is_ignored(launched):
    _replies_to("/scan", age_s=3600)

    assert launched == []


def test_scan_starts_the_live_scraper(launched):
    _replies_to("/scan")

    assert launched == [[sys.executable, "-u", "apartment_bot.py"]]


def test_scan_is_refused_while_a_scheduled_scan_is_running(launched):
    storage.start_run("live")

    _replies_to("/scan")

    assert launched == []


def test_status_with_no_scan_running_says_so():
    assert _replies_to("/status") == ["💤 אין סריקה פעילה"]


def test_status_reports_the_last_finished_run():
    run_id = storage.start_run("live")
    storage.finish_run(run_id, "ok", {"total_added": 3}, 0, 0)

    assert "3 דירות נוספו" in _replies_to("/status")[0]


def test_stop_with_no_scan_running_says_there_is_nothing_to_stop():
    assert _replies_to("/stop") == ["אין סריקה פעילה"]


def test_an_unknown_command_replies_with_the_command_list():
    assert _replies_to("/dance") == [telegram_commands.format_help()]


def test_command_output_is_html_escaped():
    assert "&lt;group&gt;" in format_cli_output("stats", "<group>")


def test_long_command_output_is_cut_to_fit_one_telegram_message():
    assert len(format_cli_output("stats", "x" * 10000)) < 4096


def test_stop_kills_the_scheduled_scan_by_its_recorded_pid(killed):
    run_id = storage.start_run("live")
    storage.set_bot_state(storage.SCAN_PID_KEY, f"{run_id}:5150")

    _replies_to("/stop")

    assert killed == [["taskkill", "/T", "/F", "/PID", "5150"]]


def test_stop_marks_the_scheduled_scan_row_stopped(killed):
    run_id = storage.start_run("live")
    storage.set_bot_state(storage.SCAN_PID_KEY, f"{run_id}:5150")

    _replies_to("/stop")

    assert storage.get_recent_runs(1)[0]["status"] == "stopped"


def test_stop_never_kills_a_pid_recorded_by_an_older_run(killed):
    old_run = storage.start_run("live")
    storage.finish_run(old_run, "ok", {}, 0, 0)
    storage.set_bot_state(storage.SCAN_PID_KEY, f"{old_run}:5150")
    storage.start_run("live")

    _replies_to("/stop")

    assert killed == []


def test_scan_fb_scans_only_the_facebook_groups(launched):
    _replies_to("/scan_fb")

    assert launched == [[sys.executable, "-u", "apartment_bot.py", "--source", "fb"]]


def test_scan_yad2_reads_only_the_alert_emails(launched):
    _replies_to("/scan_yad2")

    assert launched == [[sys.executable, "-u", "apartment_bot.py", "--source", "yad2"]]


def test_a_group_button_scans_that_group_by_its_stored_link(launched):
    storage.replace_groups([_group("123", "דירות בגבעתיים")])

    _replies_to_button("sg:123")

    assert launched == [[sys.executable, "-u", "apartment_bot.py", "--group", "https://www.facebook.com/groups/123"]]


def test_a_group_button_for_a_group_no_longer_listed_starts_nothing(launched):
    storage.replace_groups([_group("123")])

    _replies_to_button("sg:999")

    assert launched == []


def test_a_command_button_runs_that_command():
    assert _replies_to_button("cmd:status") == ["💤 אין סריקה פעילה"]


def test_a_button_naming_an_unknown_command_does_nothing():
    assert _replies_to_button("cmd:rm") == []


def test_group_picker_offers_only_active_groups():
    _, keyboard = format_group_picker([_group("1", "פעילה"), _group("2", "מושהית", active=False)])

    assert [row[0]["text"] for row in keyboard["inline_keyboard"]] == ["פעילה"]


def test_group_picker_button_carries_the_group_id():
    _, keyboard = format_group_picker([_group("123", "פעילה")])

    assert keyboard["inline_keyboard"][0][0]["callback_data"] == "sg:123"


def test_pause_records_when_scheduled_scans_were_paused():
    _replies_to("/pause")

    assert storage.get_bot_state(storage.SCANS_PAUSED_KEY)


def test_resume_clears_the_pause():
    _replies_to("/pause")

    _replies_to("/resume")

    assert storage.get_bot_state(storage.SCANS_PAUSED_KEY) == ""


def test_status_says_scheduled_scans_are_paused():
    _replies_to("/pause")

    assert "⏸" in _replies_to("/status")[0]


def test_menu_offers_resume_while_paused():
    buttons = [button["callback_data"] for row in menu_keyboard(True)["inline_keyboard"] for button in row]

    assert "cmd:resume" in buttons


def test_status_shows_the_running_scans_progress():
    storage.start_run("live")
    storage.set_bot_state(storage.SCAN_PROGRESS_KEY, json.dumps(
        {"text": "⏳ 23/54 קבוצות", "updated": datetime.now().isoformat(timespec="seconds")}))

    assert "23/54" in _replies_to("/status")[0]


def test_status_ignores_progress_left_by_a_scan_that_died():
    storage.start_run("live")
    stale = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    storage.set_bot_state(storage.SCAN_PROGRESS_KEY, json.dumps({"text": "⏳ 23/54 קבוצות", "updated": stale}))

    assert "23/54" not in _replies_to("/status")[0]


def test_new_listings_says_when_nothing_was_added():
    assert "לא נוספו" in format_new_listings([])


def test_new_listings_shows_the_newest_first():
    added = [{"url": "https://www.facebook.com/groups/1/posts/1/", "address": "ישנה", "rooms_val": 3, "price_val": 6000},
             {"url": "https://www.facebook.com/groups/1/posts/2/", "address": "חדשה", "rooms_val": 3, "price_val": 6000}]

    text = format_new_listings(added)

    assert text.index("חדשה") < text.index("ישנה")


def test_problem_groups_lists_a_group_not_joined():
    text = format_problem_groups([_group("1", "סגורה")], {"https://www.facebook.com/groups/1": {"status": "not_member"}})

    assert "סגורה" in text


def test_problem_groups_flags_many_posts_with_no_match():
    overview = {"https://www.facebook.com/groups/1": {"status": "ok", "last_scanned": "2026-11-13", "posts": 500, "added": 0}}

    assert "כדאי להשהות" in format_problem_groups([_group("1", "ריקה")], overview)


def test_problem_groups_says_all_is_well_when_nothing_needs_a_hand():
    overview = {"https://www.facebook.com/groups/1": {"status": "ok", "last_scanned": "2026-11-13", "posts": 500, "added": 4}}

    assert format_problem_groups([_group("1", "טובה")], overview) == "✅ כל הקבוצות הפעילות תקינות"


def test_stats_reports_listings_added_this_week():
    assert "2 בשבוע האחרון" in format_stats({storage.VERDICT_ADDED: 40}, 2, 0, [], [])


def test_doctor_summary_counts_the_failures():
    results = [["env", "PASS", "ok", ""], ["gemini", "FAIL", "unreachable", "check the key"]]

    assert format_doctor(results).startswith("🩺 <b>1 תקלות</b>")


def test_doctor_details_are_html_escaped():
    assert "&lt;key&gt;" in format_doctor([["env", "FAIL", "missing <key>", ""]])
