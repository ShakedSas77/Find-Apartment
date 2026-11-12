"""
Tests for the Telegram chat commands: who is obeyed, and what each command replies or starts.
subprocess is the boundary faked here; the run rows go through a real temporary DB.
"""
import sys
import time

import pytest

from core import storage
from integrations import telegram_commands
from integrations.telegram_commands import format_cli_output, handle_message, parse_command

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
    handle_message(_message(text, **message_kwargs), _CHAT_ID, sent.append)
    return sent


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


def test_stop_with_no_scan_started_here_says_there_is_nothing_to_stop():
    assert _replies_to("/stop") == ["אין סריקה פעילה שהופעלה מכאן"]


def test_an_unknown_command_replies_with_the_command_list():
    assert _replies_to("/dance") == [telegram_commands.format_help()]


def test_command_output_is_html_escaped():
    assert "&lt;group&gt;" in format_cli_output("stats", "<group>")


def test_long_command_output_is_cut_to_fit_one_telegram_message():
    assert len(format_cli_output("stats", "x" * 10000)) < 4096
