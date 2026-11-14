"""
Tests for the web app: its data helpers directly, and the pages through Streamlit's AppTest (the app
runs in-process, so it sees this test's temp database, Excel file and settings file).
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import config
from core import local_settings, storage
from integrations.local_workbook import LocalWorkbook
from webapp import data

_APP = str(Path(__file__).resolve().parent.parent / "webapp" / "app.py")
_G1 = "https://www.facebook.com/groups/111"


def _row(url, price="", rooms="", distance="", address=""):
    row = [""] * len(config.SHEET_HEADERS)
    row[0], row[1], row[2], row[3], row[13] = url, price, rooms, distance, address
    return row


@pytest.fixture
def basic_user(tmp_path, monkeypatch):
    """A finished quick setup: no-AI, Excel results with one listing, one group, a saved destination."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()
    storage.replace_groups([{"url": _G1, "name": "דירות", "active": True}])
    monkeypatch.setattr(local_settings, "_CONFIG_LOCAL", tmp_path / "config_local.py")
    monkeypatch.setattr(local_settings, "_ENV_FILE", tmp_path / ".env")
    (tmp_path / "config_local.py").write_text("DESTINATION_ADDRESS = 'ביאליק 30, רמת גן'\n", encoding="utf-8")
    for name, value in {"OUTPUT_BACKEND": "excel", "EXCEL_PATH": str(tmp_path / "listings.xlsx"),
                        "LLM_PROVIDER": "none", "MIN_PRICE": config.MIN_PRICE, "MAX_PRICE": config.MAX_PRICE}.items():
        monkeypatch.setattr(config, name, value)
    sheet = LocalWorkbook(config.EXCEL_PATH).sheet1
    sheet.insert_row(config.SHEET_HEADERS, 1)
    sheet.append_rows([_row("https://www.facebook.com/groups/111/posts/1/", 6000, 3, 1.2, "ביאליק 30, רמת גן")],
                      value_input_option="USER_ENTERED")
    return tmp_path


def _app():
    return AppTest.from_file(_APP, default_timeout=60)


# ─── data helpers ────────────────────────────────────────────────────────────

def test_listings_from_both_tabs_carry_their_source():
    frame = data.listings_frame([("פייסבוק", [config.SHEET_HEADERS, _row("https://f/1")]),
                                 ("יד2", [config.SHEET_HEADERS, _row("https://y/1")])])

    assert list(frame[data.SOURCE]) == ["פייסבוק", "יד2"]


def test_listing_numbers_are_read_as_numbers():
    frame = data.listings_frame([("פייסבוק", [config.SHEET_HEADERS, _row("https://f/1", "6,100", "3.5")])])

    assert frame[data.PRICE].iloc[0] == 6100


def test_a_listing_with_no_distance_is_kept_by_the_distance_filter():
    frame = data.listings_frame([("פייסבוק", [config.SHEET_HEADERS, _row("https://f/1", 6000, 3, "")])])

    assert len(data.filter_listings(frame, max_km=1.0)) == 1


def test_a_listing_farther_than_the_limit_is_filtered_out():
    frame = data.listings_frame([("פייסבוק", [config.SHEET_HEADERS, _row("https://f/1", 6000, 3, "4.5")])])

    assert data.filter_listings(frame, max_km=2.0).empty


def test_setup_is_complete_for_the_basic_user(basic_user):
    assert data.setup_missing() == []


def test_setup_asks_for_a_destination_until_one_is_saved(basic_user):
    (basic_user / "config_local.py").write_text("", encoding="utf-8")

    assert data.setup_missing() == ["destination"]


# ─── pages ───────────────────────────────────────────────────────────────────

def test_a_new_user_sees_the_setup_page(basic_user):
    (basic_user / "config_local.py").write_text("", encoding="utf-8")

    assert "ברוכים הבאים" in _app().run().title[0].value


def test_the_results_page_lists_the_listings_from_the_excel_file(basic_user):
    assert len(_app().run().dataframe[0].value) == 1


def test_saving_the_budget_writes_it_to_the_settings_file(basic_user):
    settings = AppTest.from_function(_settings_page, default_timeout=60).run()
    settings.number_input(key="max_price").set_value(6500)
    settings.button[0].click().run()

    assert "MAX_PRICE = 6500" in (basic_user / "config_local.py").read_text(encoding="utf-8")


def test_settings_cannot_be_saved_while_a_scan_runs(basic_user):
    storage.start_run("live")

    assert AppTest.from_function(_settings_page, default_timeout=60).run().button[0].disabled


def _settings_page():
    from webapp import components
    components.budget_section(components.edits_locked())


def test_a_listing_is_placed_where_its_address_was_located(basic_user):
    storage.save_address_cache("ביאליק 30, רמת גן", "", "", "high", "", "1.2", 1200, "street_estimate", "ok", 32.08, 34.81)
    frame = data.listings_frame([("פייסבוק", [config.SHEET_HEADERS, _row("https://f/1", address="ביאליק 30, רמת גן")])])

    assert data.with_coordinates(frame)[[data.LAT, data.LON]].iloc[0].tolist() == [32.08, 34.81]


def test_the_score_filter_drops_lower_fits():
    rows = [_row("https://f/1"), _row("https://f/2")]
    rows[0][config.SHEET_HEADERS.index("ציון התאמה")], rows[1][config.SHEET_HEADERS.index("ציון התאמה")] = "40", "80"
    frame = data.listings_frame([("פייסבוק", [config.SHEET_HEADERS, *rows])])

    assert list(data.filter_listings(frame, min_score=60)["לינק למודעה"]) == ["https://f/2"]


def test_the_age_filter_keeps_a_listing_with_no_post_date():
    frame = data.listings_frame([("פייסבוק", [config.SHEET_HEADERS, _row("https://f/1")])])

    assert len(data.filter_listings(frame, max_age_days=1)) == 1


def test_the_map_page_places_the_located_listings(basic_user):
    storage.save_address_cache("ביאליק 30, רמת גן", "", "", "high", "", "1.2", 1200, "street_estimate", "ok", 32.08, 34.81)
    at = AppTest.from_function(_map_page, default_timeout=60).run()

    assert at.caption[0].value.startswith("1 דירות על המפה")


def _map_page():
    from webapp import views
    views.map_page()
