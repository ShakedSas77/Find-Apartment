"""
A local Excel file (OUTPUT_BACKEND = "excel") standing in for the Google Sheet: LocalWorkbook / LocalWorksheet
implement the slice of gspread's Spreadsheet / Worksheet API the bot calls, so sheets.py, groups_sync.py,
mail_source.py and the scraper write to either one unchanged.

Every write is saved at once. Excel locks a file it has open; then the save is retried briefly and, if
the file is still locked, the change stays in memory, a WARNING says to close the file, and the next
write saves it.
"""
import re
import threading
import time
from pathlib import Path

import gspread
from gspread.utils import a1_range_to_grid_range, rowcol_to_a1
from openpyxl import Workbook, load_workbook

_NUMBER_RE = re.compile(r"-?\d+(\.\d+)?")
_SAVE_ATTEMPTS = 3
_SAVE_RETRY_S = 1.0


class LocalWorkbook:
    """The Excel file at `path`, created (with a first tab named `first_tab`) when it doesn't exist yet."""

    def __init__(self, path: str, first_tab: str = "Facebook"):
        self.path = Path(path)
        self._lock = threading.RLock()
        if self.path.exists():
            self._book = load_workbook(self.path)
        else:
            self._book = Workbook()
            self._book.active.title = first_tab
            self._book.active.sheet_view.rightToLeft = True
            self.save()

    @property
    def sheet1(self) -> "LocalWorksheet":
        return LocalWorksheet(self, self._book.worksheets[0])

    def worksheets(self) -> list["LocalWorksheet"]:
        return [LocalWorksheet(self, sheet) for sheet in self._book.worksheets]

    def worksheet(self, title: str) -> "LocalWorksheet":
        if title not in self._book.sheetnames:
            raise gspread.WorksheetNotFound(title)
        return LocalWorksheet(self, self._book[title])

    def add_worksheet(self, title: str, rows: int = 0, cols: int = 0) -> "LocalWorksheet":
        with self._lock:
            sheet = self._book.create_sheet(title)
            sheet.sheet_view.rightToLeft = True
            self.save()
        return LocalWorksheet(self, sheet)

    def batch_update(self, body: dict):
        # The Sheets API formatting requests (plain-text date columns); cells here are stored as typed,
        # so a date-like string already stays text.
        return None

    def save(self):
        with self._lock:
            for attempt in range(_SAVE_ATTEMPTS):
                try:
                    self._book.save(self.path)
                    return
                except PermissionError:
                    if attempt < _SAVE_ATTEMPTS - 1:
                        time.sleep(_SAVE_RETRY_S)
            print(f"WARNING: {self.path} is open in another program (Excel?) - close it; "
                  f"the new rows are kept and saved on the next write.")


class LocalWorksheet:
    def __init__(self, workbook: LocalWorkbook, sheet):
        self.spreadsheet = workbook
        self._sheet = sheet

    @property
    def title(self) -> str:
        return self._sheet.title

    @property
    def id(self) -> str:
        return self._sheet.title

    def get_all_values(self) -> list[list[str]]:
        rows = [[_text(value) for value in row] for row in self._sheet.iter_rows(values_only=True)]
        while rows and not any(rows[-1]):
            rows.pop()
        width = max((_trimmed_len(row) for row in rows), default=0)
        return [row[:width] + [""] * (width - len(row[:width])) for row in rows]

    def row_values(self, row: int) -> list[str]:
        rows = self.get_all_values()
        values = rows[row - 1] if row <= len(rows) else []
        return values[:_trimmed_len(values)]

    def col_values(self, col: int) -> list[str]:
        values = [row[col - 1] if col <= len(row) else "" for row in self.get_all_values()]
        return values[:_trimmed_len(values)]

    def find(self, query: str, in_column: int | None = None):
        for row_index, row in enumerate(self.get_all_values(), start=1):
            for col_index, value in enumerate(row, start=1):
                if value == query and in_column in (None, col_index):
                    return gspread.Cell(row_index, col_index, value)
        return None

    def append_row(self, values: list, value_input_option: str = "RAW", table_range: str | None = None):
        self.append_rows([values], value_input_option)

    def append_rows(self, rows: list[list], value_input_option: str = "RAW", table_range: str | None = None):
        with self.spreadsheet._lock:
            next_row = len(self.get_all_values()) + 1
            for offset, values in enumerate(rows):
                self._write_row(next_row + offset, 1, values, value_input_option)
            self.spreadsheet.save()

    def insert_row(self, values: list, index: int = 1, value_input_option: str = "RAW"):
        with self.spreadsheet._lock:
            self._sheet.insert_rows(index)
            self._write_row(index, 1, values, value_input_option)
            self.spreadsheet.save()

    def delete_rows(self, start_index: int, end_index: int | None = None):
        with self.spreadsheet._lock:
            self._sheet.delete_rows(start_index, (end_index or start_index) - start_index + 1)
            self.spreadsheet.save()

    def update(self, range_name: str = "A1", values: list[list] | None = None, value_input_option: str = "RAW"):
        with self.spreadsheet._lock:
            self._write_range(range_name, values or [], value_input_option)
            self.spreadsheet.save()

    def batch_update(self, data: list[dict], value_input_option: str = "RAW"):
        with self.spreadsheet._lock:
            for item in data:
                self._write_range(item["range"], item["values"], value_input_option)
            self.spreadsheet.save()

    def update_cell(self, row: int, col: int, value):
        self.update(rowcol_to_a1(row, col), [[value]], "USER_ENTERED")

    def batch_clear(self, ranges: list[str]):
        with self.spreadsheet._lock:
            for range_name in ranges:
                grid = a1_range_to_grid_range(range_name)
                for row in range(grid["startRowIndex"] + 1, grid["endRowIndex"] + 1):
                    for col in range(grid.get("startColumnIndex", 0) + 1, grid["endColumnIndex"] + 1):
                        self._sheet.cell(row, col).value = None
            self.spreadsheet.save()

    def _write_range(self, range_name: str, values: list[list], value_input_option: str):
        grid = a1_range_to_grid_range(range_name)
        for offset, row_values in enumerate(values):
            self._write_row(grid["startRowIndex"] + 1 + offset, grid.get("startColumnIndex", 0) + 1,
                            row_values, value_input_option)

    def _write_row(self, row: int, first_col: int, values: list, value_input_option: str):
        for offset, value in enumerate(values):
            cell = self._sheet.cell(row, first_col + offset)
            cell.value = _cell_value(value, value_input_option == "USER_ENTERED")
            if isinstance(cell.value, str):
                # A string starting with "=" would otherwise be saved as a formula.
                cell.data_type = "s"


def _cell_value(value, user_entered: bool):
    """What Sheets would store: under USER_ENTERED a numeric string becomes a number and a leading "'"
    (core.normalize._sheet_safe_cell's formula guard) marks plain text and is dropped."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not user_entered:
        return value
    if value.startswith("'"):
        return value[1:]
    if _NUMBER_RE.fullmatch(value):
        return float(value) if "." in value else int(value)
    return value


def _text(value) -> str:
    # Sheets shows 3.0 as "3": the bot parses these strings back, so match its formatting.
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _trimmed_len(values: list[str]) -> int:
    length = len(values)
    while length and not values[length - 1]:
        length -= 1
    return length
