"""
Tests for the local Excel stand-in for the Google Sheet: it must answer the gspread calls the bot
makes the way Sheets does. Real files in a temp folder; only openpyxl's save is faked, to simulate
Excel holding the file open.
"""
from datetime import datetime, timedelta

import gspread
import pytest

import config
from integrations import local_workbook
from integrations.local_workbook import LocalWorkbook
from integrations.sheets import dedupe_and_sort_sheet

_HEADERS = ["לינק למודעה", "מחיר", "חדרים"]


@pytest.fixture
def tab(tmp_path):
    sheet = LocalWorkbook(str(tmp_path / "listings.xlsx")).sheet1
    sheet.insert_row(_HEADERS, 1)
    return sheet


def test_appended_rows_read_back_as_text_like_sheets_returns_them(tab):
    tab.append_rows([["https://x/1", 6000, 3.5]], value_input_option="USER_ENTERED")

    assert tab.get_all_values()[1] == ["https://x/1", "6000", "3.5"]


def test_a_numeric_string_is_stored_as_a_number_under_user_entered(tab):
    tab.append_rows([["https://x/1", "6000", "3"]], value_input_option="USER_ENTERED")

    assert tab._sheet.cell(2, 2).value == 6000


def test_the_formula_guard_apostrophe_is_dropped_and_the_text_kept(tab):
    tab.append_rows([["'=HYPERLINK(1)", "", ""]], value_input_option="USER_ENTERED")

    assert tab.get_all_values()[1][0] == "=HYPERLINK(1)"


def test_rows_survive_reopening_the_file(tab, tmp_path):
    tab.append_rows([["https://x/1", 6000, 3]])

    assert LocalWorkbook(str(tmp_path / "listings.xlsx")).sheet1.col_values(1) == ["לינק למודעה", "https://x/1"]


def test_update_writes_a_block_at_an_a1_range(tab):
    tab.append_rows([["https://x/1", 6000, 3], ["https://x/2", 6100, 3]])

    tab.update(range_name="B2:B3", values=[[5000], [5100]])

    assert tab.col_values(2) == ["מחיר", "5000", "5100"]


def test_batch_update_writes_each_range(tab):
    tab.append_rows([["https://x/1", 6000, 3]])

    tab.batch_update([{"range": "C2", "values": [[2.5]]}])

    assert tab.row_values(2) == ["https://x/1", "6000", "2.5"]


def test_delete_rows_shifts_the_rows_below_up(tab):
    tab.append_rows([["https://x/1", 6000, 3], ["https://x/2", 6100, 3]])

    tab.delete_rows(2)

    assert tab.col_values(1) == ["לינק למודעה", "https://x/2"]


def test_batch_clear_empties_the_range(tab):
    tab.append_rows([["https://x/1", 6000, 3], ["https://x/2", 6100, 3]])

    tab.batch_clear(["A2:C3"])

    assert tab.get_all_values() == [_HEADERS]


def test_find_returns_the_cell_of_a_url_in_the_first_column(tab):
    tab.append_rows([["https://x/1", 6000, 3], ["https://x/2", 6100, 3]])

    assert tab.find("https://x/2", in_column=1).row == 3


def test_find_returns_none_when_the_url_is_not_there(tab):
    assert tab.find("https://x/9", in_column=1) is None


def test_a_missing_tab_raises_like_gspread(tmp_path):
    with pytest.raises(gspread.WorksheetNotFound):
        LocalWorkbook(str(tmp_path / "listings.xlsx")).worksheet("Yad2")


def test_an_added_tab_is_found_by_title(tmp_path):
    book = LocalWorkbook(str(tmp_path / "listings.xlsx"))
    book.add_worksheet(title="Groups")

    assert [sheet.title for sheet in book.worksheets()] == ["Facebook", "Groups"]


def test_rows_written_while_excel_locks_the_file_are_saved_by_the_next_write(tab, tmp_path, monkeypatch):
    real_save = tab.spreadsheet._book.save
    monkeypatch.setattr(local_workbook, "_SAVE_RETRY_S", 0)
    monkeypatch.setattr(tab.spreadsheet._book, "save", lambda path: (_ for _ in ()).throw(PermissionError()))
    tab.append_rows([["https://x/1", 6000, 3]])
    monkeypatch.setattr(tab.spreadsheet._book, "save", real_save)

    tab.append_rows([["https://x/2", 6100, 3]])

    assert LocalWorkbook(str(tmp_path / "listings.xlsx")).sheet1.col_values(1)[1:] == ["https://x/1", "https://x/2"]


def test_the_end_of_run_sort_rewrites_the_excel_tab_newest_first(tmp_path):
    tab = LocalWorkbook(str(tmp_path / "listings.xlsx")).sheet1
    tab.insert_row(config.SHEET_HEADERS, 1)
    yesterday, today = (f"{d:%d/%m}" for d in (datetime.now() - timedelta(days=1), datetime.now()))
    post_date_col = config.SHEET_HEADERS.index("תאריך פרסום")
    rows = [["https://x/old", 6000, 3] + [""] * (len(config.SHEET_HEADERS) - 3),
            ["https://x/new", 6100, 3] + [""] * (len(config.SHEET_HEADERS) - 3)]
    rows[0][post_date_col], rows[1][post_date_col] = yesterday, today
    tab.append_rows(rows, value_input_option="USER_ENTERED")

    dedupe_and_sort_sheet(tab)

    assert tab.col_values(1)[1:] == ["https://x/new", "https://x/old"]
