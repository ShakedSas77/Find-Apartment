"""
=== backfill_sheet ===

One-time (rerunnable) migration: fills the "ציון התאמה" (fit score) and
"הצבעות" (vote tally) columns for every row already in the sheet from before
those columns existed. Also useful any time scoring.compute_fit_score()'s
weights change and you want existing rows to reflect the new formula, or a
vote came in before bot_listener.py was running to sync it live.

    python backfill_sheet.py

No browser — uses the same Sheets client as the rest of the bot. Reuses
apartment_bot.setup_google_sheet() so the header row gets auto-corrected to
the current SHEET_HEADERS (adding the two new columns if missing) before the
data rows are touched.
"""

import gspread

import config
import scoring
import storage
from apartment_bot import setup_google_sheet

_SCORE_COL = config.SHEET_HEADERS.index("ציון התאמה") + 1
_VOTES_COL = config.SHEET_HEADERS.index("הצבעות") + 1

# Column indexes (0-based) matching _build_row()'s field order
_COL_PRICE, _COL_ROOMS, _COL_DIST_KM = 1, 2, 3
_COL_ELEVATOR, _COL_PARKING, _COL_SHELTER, _COL_IS_AGENT = 6, 7, 10, 11


def _fields_from_row(row: list) -> dict:
    def cell(i):
        return row[i] if i < len(row) else ""

    price = cell(_COL_PRICE)
    dist_km = cell(_COL_DIST_KM)
    return {
        "price_val": float(price) if price else 0,
        "is_agent": cell(_COL_IS_AGENT) == "כן",
        "distance_meters": float(dist_km) * 1000 if dist_km else None,
        "elevator": cell(_COL_ELEVATOR) == "כן",
        "parking": cell(_COL_PARKING),
        "shelter": cell(_COL_SHELTER) == "כן",
    }


def backfill():
    sheet, _ = setup_google_sheet()
    rows = sheet.get_all_values()
    if len(rows) < 2:
        print("No data rows to backfill.")
        return

    updates = []
    for sheet_row_num, row in enumerate(rows[1:], start=2):
        url = row[0]
        if not url:
            continue
        score = scoring.compute_fit_score(_fields_from_row(row))
        votes = storage.get_votes(url)
        up = sum(1 for v in votes if v["vote"] == "up")
        down = sum(1 for v in votes if v["vote"] == "down")
        updates.append({"range": gspread.utils.rowcol_to_a1(sheet_row_num, _SCORE_COL), "values": [[score]]})
        updates.append({"range": gspread.utils.rowcol_to_a1(sheet_row_num, _VOTES_COL), "values": [[f"⭐{up} 🗑{down}"]]})

    sheet.batch_update(updates)
    print(f"Backfilled {len(rows) - 1} row(s).")


if __name__ == "__main__":
    backfill()
