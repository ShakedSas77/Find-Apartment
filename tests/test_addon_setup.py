"""
Tests for core.addon_setup's checks. Task Scheduler (subprocess) is the boundary faked here.
"""
from types import SimpleNamespace

import config
from core import addon_setup, local_settings


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


def test_a_new_destination_drops_the_example_destinations_south_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(local_settings, "_CONFIG_LOCAL", tmp_path / "config_local.py")
    for name in ("DESTINATION_ADDRESS", "DESTINATION_LAT", "DESTINATION_LON", "EXCLUDE_SOUTH_OF_LAT"):
        monkeypatch.setattr(config, name, getattr(config, name))  # save_settings also sets the running config

    addon_setup.save_destination("הרצל 1, חיפה", 32.8, 35.0)

    assert "EXCLUDE_SOUTH_OF_LAT = None" in (tmp_path / "config_local.py").read_text(encoding="utf-8")
