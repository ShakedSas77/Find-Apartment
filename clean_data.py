"""
Wipes local state so the next bot run starts fresh:
- deletes bot_data.db (SQLite cache: verdicts, raw text, Maps usage)
- clears all data rows in the results (Google Sheet or local Excel file), keeping the header row

Run manually whenever you want a clean slate (e.g. after a filter/prompt change
you want to test without stale cached verdicts skipping posts).
"""
import os
import sys

from core.storage import DB_PATH
from integrations.sheets import open_spreadsheet


def clean_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Deleted {DB_PATH}.")
    else:
        print(f"{DB_PATH} does not exist, nothing to delete.")


def clean_sheet():
    sheet = open_spreadsheet().sheet1

    row_count = len(sheet.get_all_values())
    if row_count <= 1:
        print(f"Sheet already has no data rows (only {row_count} row(s)). Nothing to clear.")
        return
    sheet.batch_clear([f"A2:Z{row_count}"])
    print(f"Cleared {row_count - 1} data row(s) from the sheet, kept header row.")


if __name__ == "__main__":
    clean_db()
    clean_sheet()
