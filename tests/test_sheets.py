"""
Regression tests for sheets.dedupe_and_sort_sheet. A row with an empty URL
cell (a misaligned append wrote its data starting at column S) used to share
the key ("url", "") with every other URL-less row, so the end-of-run rewrite
merged them all into one and deleted the rest — 34 real listings were lost
this way on 2026-11-07.
"""
from datetime import date

import pytest

import sheets
import storage
from config import SHEET_HEADERS


class FakeSheet:
    """In-memory stand-in for a gspread Worksheet — the Sheets API is a network boundary."""

    id = 0

    def __init__(self, data):
        self.data = data
        self.written_rows = []
        self.spreadsheet = self

    def get_all_values(self):
        return self.data

    def batch_clear(self, ranges):
        pass

    def batch_update(self, body):
        pass

    def update(self, range_name, values, value_input_option):
        self.written_rows = values


@pytest.fixture(autouse=True)
def empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


def _row(url, address="", price="6000", rooms="3"):
    row = [""] * len(SHEET_HEADERS)
    row[0], row[1], row[2], row[12], row[13] = url, price, rooms, date.today().strftime("%d/%m"), address
    return row


def _misaligned_row(marker):
    return [""] * 18 + [marker]


def test_url_less_rows_are_all_kept():
    sheet = FakeSheet([SHEET_HEADERS, _misaligned_row("a"), _misaligned_row("b"), _misaligned_row("c")])

    sheets.dedupe_and_sort_sheet(sheet)

    assert sorted(r[18] for r in sheet.written_rows) == ["a", "b", "c"]


def test_url_less_row_is_not_merged_into_a_listing_with_the_same_address():
    url_less = _row("", address="רחוב ביאליק 14")
    sheet = FakeSheet([SHEET_HEADERS, _row("https://www.facebook.com/groups/1/posts/1/", address="רחוב ביאליק 14"), url_less])

    sheets.dedupe_and_sort_sheet(sheet)

    assert len(sheet.written_rows) == 2


def test_same_url_twice_is_still_deduped():
    url = "https://www.facebook.com/groups/1/posts/1/"
    sheet = FakeSheet([SHEET_HEADERS, _row(url), _row(url)])

    sheets.dedupe_and_sort_sheet(sheet)

    assert len(sheet.written_rows) == 1


def test_crossposts_of_the_same_listing_are_still_deduped():
    sheet = FakeSheet([
        SHEET_HEADERS,
        _row("https://www.facebook.com/groups/1/posts/1/", address="רחוב ביאליק 14"),
        _row("https://www.facebook.com/groups/2/posts/2/", address="רחוב ביאליק 14"),
    ])

    sheets.dedupe_and_sort_sheet(sheet)

    assert len(sheet.written_rows) == 1
