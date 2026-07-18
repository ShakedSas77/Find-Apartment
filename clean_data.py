"""
Wipes local state so the next bot run starts fresh:
- deletes bot_data.db (SQLite cache: verdicts, raw text, Maps usage)
- clears all data rows in the Google Sheet, keeping the header row

Run manually whenever you want a clean slate (e.g. after a filter/prompt change
you want to test without stale cached verdicts skipping posts).
"""
import os
import sys

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

from config import CREDENTIALS_FILE
from storage import DB_PATH

load_dotenv()

SHEET_ID = os.getenv("SHEET_ID")


def clean_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Deleted {DB_PATH}.")
    else:
        print(f"{DB_PATH} does not exist, nothing to delete.")


def clean_sheet():
    if not SHEET_ID:
        print("ERROR: SHEET_ID missing from .env — skipping sheet clean.")
        return
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SHEET_ID).sheet1

    row_count = len(sheet.get_all_values())
    if row_count <= 1:
        print(f"Sheet already has no data rows (only {row_count} row(s)). Nothing to clear.")
        return
    sheet.batch_clear([f"A2:Z{row_count}"])
    print(f"Cleared {row_count - 1} data row(s) from the sheet, kept header row.")


if __name__ == "__main__":
    clean_db()
    clean_sheet()
