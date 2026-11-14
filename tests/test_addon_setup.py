"""
Tests for core.addon_setup's checks. Task Scheduler (subprocess) is the boundary faked here.
"""
from types import SimpleNamespace

from core import addon_setup


def test_the_service_account_email_is_read_from_its_key_file():
    assert addon_setup.service_account_email('{"client_email": "bot@p.iam.gserviceaccount.com"}') == "bot@p.iam.gserviceaccount.com"


def test_a_file_that_isnt_a_service_account_key_gives_no_email():
    assert addon_setup.service_account_email('{"type": "authorized_user"}') is None


def test_the_sheet_id_is_read_from_its_link():
    assert addon_setup.sheet_id_from_link("https://docs.google.com/spreadsheets/d/AbC_12-x/edit#gid=0") == "AbC_12-x"


def test_an_impossible_time_is_refused_before_task_scheduler(monkeypatch):
    calls = []
    monkeypatch.setattr(addon_setup.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    addon_setup.schedule_scan("25:99")

    assert calls == []


def test_only_the_bots_tasks_are_listed_from_task_scheduler(monkeypatch):
    output = '"\\ApartmentBot 2000","11/14/2026 8:00:00 PM","Ready"\n"\\OneDrive Update","N/A","Ready"\n'
    monkeypatch.setattr(addon_setup.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output))

    assert addon_setup.scheduled_scans() == [{"name": "ApartmentBot 2000", "next_run": "11/14/2026 8:00:00 PM"}]
