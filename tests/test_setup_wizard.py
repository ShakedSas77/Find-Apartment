"""
Tests for how setup_wizard.py writes answers: config_local.py and .env keep every line they didn't
set. Real files in a temp folder.
"""
import pytest

import config
import setup_wizard


@pytest.fixture(autouse=True)
def keep_config(monkeypatch):
    # save_settings also applies the values to the loaded config; undo that after each test.
    for name in ("MIN_PRICE", "TARGET_URLS", "LLM_PROVIDER"):
        monkeypatch.setattr(config, name, getattr(config, name))


def test_a_setting_already_in_config_local_is_replaced_in_place(tmp_path):
    path = tmp_path / "config_local.py"
    path.write_text("MIN_PRICE = 5000\nTELEGRAM_ENABLED = True\n", encoding="utf-8")

    setup_wizard.save_settings({"MIN_PRICE": 6000}, path)

    assert path.read_text(encoding="utf-8") == "MIN_PRICE = 6000\nTELEGRAM_ENABLED = True\n"


def test_a_setting_spanning_several_lines_is_replaced_whole(tmp_path):
    path = tmp_path / "config_local.py"
    path.write_text('TARGET_URLS = [\n    "a",\n    "b",\n]\nMIN_PRICE = 5000\n', encoding="utf-8")

    setup_wizard.save_settings({"TARGET_URLS": []}, path)

    assert path.read_text(encoding="utf-8") == "TARGET_URLS = []\nMIN_PRICE = 5000\n"


def test_a_new_setting_is_appended(tmp_path):
    path = tmp_path / "config_local.py"
    path.write_text("MIN_PRICE = 5000\n", encoding="utf-8")

    setup_wizard.save_settings({"LLM_PROVIDER": "ollama"}, path)

    assert path.read_text(encoding="utf-8").endswith("LLM_PROVIDER = 'ollama'\n")


def test_saved_settings_apply_to_the_running_config(tmp_path):
    setup_wizard.save_settings({"LLM_PROVIDER": "ollama"}, tmp_path / "config_local.py")

    assert config.LLM_PROVIDER == "ollama"


def test_a_secret_already_in_env_is_replaced_and_the_rest_kept(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("GEMINI_API_KEY=old\nSHEET_ID=abc\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "old")  # so the value save_secrets sets is undone after the test

    setup_wizard.save_secrets({"GEMINI_API_KEY": "new"}, path)

    assert path.read_text(encoding="utf-8") == "GEMINI_API_KEY=new\nSHEET_ID=abc\n"
