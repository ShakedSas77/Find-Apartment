"""
=== Google Sheets I/O ===

setup_google_sheet() (connect + dedupe/write bootstrap) and
dedupe_and_sort_sheet() (end-of-run cross-post dedupe + stale-row prune +
sort) are the two public entry points. Everything else here is the shared
row-rewrite plumbing both use.
"""
import sys

import gspread
from google.oauth2.service_account import Credentials

import env
import storage
from config import CREDENTIALS_FILE, SHEET_HEADERS
from core.util import _with_retries
from core.normalize import _sheet_safe_cell, _is_recent_post_date, _post_date_sort_key
from core.dedupe import _listing_key


def setup_google_sheet():
    """
    Connects to Google Sheets, checks existing data,
    and creates headers if the sheet is empty.
    Returns the sheet object and a set of already seen URLs.
    """
    print("\nConnecting to Google Sheets and reading existing data...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(env.get_sheet_id()).sheet1

    try:
        # Check headers via row 1 only — avoids pulling the whole sheet into memory
        headers = sheet.row_values(1)

        if not headers:
            print("Missing column headers in Google Sheet. Adding them to row 1...")
            sheet.insert_row(SHEET_HEADERS, 1)
        elif len(headers) != len(SHEET_HEADERS) or headers[0] != "לינק למודעה":
            print("Outdated column headers in Google Sheet. Updating row 1...")
            if headers and headers[0] == "לינק למודעה":
                sheet.delete_rows(1)
            sheet.insert_row(SHEET_HEADERS, 1)

        # Pull only the URL column (column 1) for dedup — not the whole table
        seen_urls = {url for url in sheet.col_values(1)[1:] if url}

        print(f"    Found {len(seen_urls)} existing apartments in the sheet. Will skip them.")
        return sheet, seen_urls
    except Exception as e:
        print(f"    ERROR: reading Google Sheet: {e}")
        print("    Aborting: cannot dedupe or write results without the sheet.")
        sys.exit(1)

def _append_rows_batch(sheet, rows: list[list]):
    if not rows:
        return
    _with_retries(lambda: sheet.append_rows(rows, value_input_option="USER_ENTERED"))

def dedupe_and_sort_sheet(sheet) -> tuple[int, int, int]:
    """
    Rewrites the sheet's data range in one pass:
    1. Removes duplicates: identical URL is always a duplicate; same apartment
       by meaningful address + rooms + price keeps the most recent post.
       Missing/weak addresses aren't merged by price+rooms alone, so real
       distinct apartments don't get accidentally deleted.
    2. Drops rows older than MAX_POST_AGE_DAYS (via _is_recent_post_date) — the
       sheet is just a "recent/relevant" view; the local DB (should_skip) is
       what permanently remembers a URL, so removing an old row here can never
       cause it to be rescanned or re-added. Rows with an unparseable/missing
       post date are kept (same "don't delete what we're not sure about" rule).
    Returns (duplicates_removed, stale_removed, kept).
    """
    data = sheet.get_all_values()
    if len(data) <= 1:
        return 0, 0, len(data) - 1 if data else 0
    rows = data[1:]

    best_by_url = {}
    for row in rows:
        url = row[0] if row else ""
        existing = best_by_url.get(url)
        if not url:
            key = f"empty-url-{len(best_by_url)}"
            best_by_url[key] = row
        elif existing is None or _post_date_sort_key(row[12] if len(row) > 12 else "") > _post_date_sort_key(existing[12] if len(existing) > 12 else ""):
            best_by_url[url] = row

    db_data = storage.get_added_listing_data()
    best_by_key = {}
    for row in best_by_url.values():
        key = _listing_key(row, db_data)
        existing = best_by_key.get(key)
        if existing is None or _post_date_sort_key(row[12] if len(row) > 12 else "") > _post_date_sort_key(existing[12] if len(existing) > 12 else ""):
            best_by_key[key] = row

    deduped_rows = list(best_by_key.values())
    duplicates_removed = len(rows) - len(deduped_rows)

    # A later crosspost's more specific address (e.g. a corner qualifier) may
    # have been folded into the DB record via _maybe_enrich_duplicate_address
    # even though its own row was never added — sync the kept row's כתובת
    # cell from that current DB value so the enrichment actually surfaces.
    # Distance is synced the same way: a formatting/geocoding-precision fix
    # can let a previously-skipped address resolve a real distance after the
    # row was already added, and this is the one place that reconciles the
    # sheet with the DB's current state rather than what was true at add time.
    for row in deduped_rows:
        url = row[0] if row else ""
        db_row = db_data.get(url)
        if not db_row:
            continue
        if db_row.get("address") and len(row) > 13:
            row[13] = db_row["address"]
        if db_row.get("distance_text") and len(row) > 3:
            row[3] = db_row["distance_text"]

    fresh_rows = [r for r in deduped_rows if _is_recent_post_date(r[12] if len(r) > 12 else "")]
    stale_removed = len(deduped_rows) - len(fresh_rows)

    fresh_rows.sort(key=lambda r: _post_date_sort_key(r[12] if len(r) > 12 else ""), reverse=True)

    _rewrite_sheet_data_rows(sheet, len(rows), fresh_rows)
    return duplicates_removed, stale_removed, len(fresh_rows)

_ENTRY_DATE_COL_INDEX = SHEET_HEADERS.index("תאריך כניסה")

_POST_DATE_COL_INDEX = SHEET_HEADERS.index("תאריך פרסום")

_SCAN_TIME_COL_INDEX = SHEET_HEADERS.index("זמן סריקה")

_TEXT_FORMAT_COL_INDICES = (_ENTRY_DATE_COL_INDEX, _POST_DATE_COL_INDEX, _SCAN_TIME_COL_INDEX)

def _rewrite_sheet_data_rows(sheet, original_row_count: int, new_rows: list[list]):
    """
    Shared full-rewrite helper: clears the data range (header kept) and writes
    new_rows back. new_rows comes from sheet.get_all_values(), which always
    returns strings — writing those back requires care on two fronts:

    1. value_input_option="USER_ENTERED" (matching the original _build_row
       write) so numeric-looking strings ("6200", "3.5") land back as real
       numbers instead of permanently downgrading to text on every rewrite.
    2. Re-running every cell through _sheet_safe_cell(): the leading-apostrophe
       formula-injection guard is an input-time signal Sheets consumes, not
       stored content — get_all_values() returns a previously-escaped
       "=SUM(...)" clean, with the apostrophe already gone. Under RAW (the old
       default) that was harmless since RAW never executes formulas either
       way; under USER_ENTERED it would be executed unless re-escaped here.
    """
    last_col = chr(ord('A') + len(SHEET_HEADERS) - 1)
    _with_retries(lambda: sheet.batch_clear([f"A2:{last_col}{original_row_count + 1}"]))

    # DD/MM text ("01/09") and the "YYYY-MM-DD HH:MM" scan timestamp both look
    # date/datetime-like to Sheets — under USER_ENTERED they get auto-parsed
    # into a real date/datetime serial (losing the deliberate literal-text
    # format) unless the column is already declared Plain text *before* the
    # value write below. Must run first, not after: setting the format on an
    # already-date-typed cell only changes how it displays, it doesn't turn
    # the stored value back into the original literal string.
    _with_retries(lambda: sheet.spreadsheet.batch_update({
        "requests": [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet.id,
                        "startColumnIndex": col_index,
                        "endColumnIndex": col_index + 1,
                        "startRowIndex": 1,
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
            for col_index in _TEXT_FORMAT_COL_INDICES
        ]
    }))

    if new_rows:
        safe_rows = [[_sheet_safe_cell(v) for v in row] for row in new_rows]
        _with_retries(lambda: sheet.update(
            range_name=f"A2:{last_col}{len(safe_rows) + 1}",
            values=safe_rows,
            value_input_option="USER_ENTERED",
        ))
