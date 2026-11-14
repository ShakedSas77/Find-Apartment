"""
Tests for env.missing_env(): only the enabled features' keys and files are required.
Environment variables and the credentials file are the boundary faked here.
"""
import pytest

import config
import env

_ALL_KEYS = ("GEMINI_API_KEY", "GMAPS_API_KEY", "SHEET_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


@pytest.fixture(autouse=True)
def basic_setup(monkeypatch, tmp_path):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "OUTPUT_BACKEND", "excel")
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "GMAPS_ENABLED", False)
    monkeypatch.setattr(config, "TELEGRAM_ENABLED", False)
    monkeypatch.setattr(config, "CREDENTIALS_FILE", str(tmp_path / "credentials.json"))


def test_a_basic_setup_with_ollama_and_excel_needs_no_keys():
    assert env.missing_env() == []


def test_gemini_needs_its_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")

    assert env.missing_env()[0].startswith("GEMINI_API_KEY")


def test_google_sheets_needs_the_sheet_id_and_the_service_account_key(monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_BACKEND", "sheets")

    assert len(env.missing_env()) == 2


def test_telegram_needs_its_token_and_chat(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ENABLED", True)

    assert env.missing_env()[0].startswith("TELEGRAM_BOT_TOKEN")


def test_google_maps_key_is_needed_only_when_maps_is_on(monkeypatch):
    monkeypatch.setattr(config, "GMAPS_ENABLED", True)

    assert env.missing_env()[0].startswith("GMAPS_API_KEY")
