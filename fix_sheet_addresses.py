"""
Fixes addresses in the Google Sheet via the Google Geocoding API.

Runs geocoding on the address column of every row, normalizes street/city
names, and updates the distance column to match the corrected address.
Tel Aviv addresses are kept only if they're within a 4km walk of
DESTINATION_ADDRESS; beyond that the row is removed. Addresses where
geocoding only found a city-level match (no street) are left unchanged —
so as not to replace a specific address with a less precise general match.

Usage:
    python fix_sheet_addresses.py            # dry run — shows what would change, doesn't write
    python fix_sheet_addresses.py --write    # runs for real and writes to the sheet
"""
import re
import sys
import time

import googlemaps
import gspread
from google.oauth2.service_account import Credentials

from config import CREDENTIALS_FILE, SHEET_HEADERS
from apartment_bot import SHEET_ID, gmaps_client, get_walking_distance, dedupe_and_sort_sheet

DRY_RUN = "--write" not in sys.argv

TARGET_CITIES = ["רמת גן", "גבעתיים"]
MAX_TLV_DISTANCE_M = 4000


def clean_address_string(raw_addr: str) -> str:
    addr = raw_addr.split("__")[0]
    addr = re.sub(r'\bרג\b', 'רמת גן', addr)
    addr = re.sub(r'\bגיבעתיים\b', 'גבעתיים', addr)
    addr = re.sub(r'\bשיינקין\b', 'שינקין', addr)
    return addr.strip()


def geocode_with_retry(query, attempts=4, base_delay=3.0):
    last_err = None
    for attempt in range(attempts):
        try:
            return gmaps_client.geocode(query, language='iw', region='il')
        except googlemaps.exceptions.ApiError as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
    raise last_err


def city_of(result):
    for comp in result.get('address_components', []):
        if 'locality' in comp.get('types', []):
            return comp['long_name']
    return None


def matched_target(res):
    c = city_of(res)
    return bool(c) and any(city in c for city in TARGET_CITIES)


_STREET_LEVEL_TYPES = {'route', 'street_address', 'premise', 'subpremise'}


def has_street_precision(result) -> bool:
    """True only if the match includes an actual street (not just a city-level fallback)."""
    if _STREET_LEVEL_TYPES & set(result.get('types', [])):
        return True
    return any(
        _STREET_LEVEL_TYPES & set(comp.get('types', []))
        for comp in result.get('address_components', [])
    )


def geocode_address(raw_address: str):
    """Returns (canonical_formatted_address_or_None, city_or_None). None if only a city-level match (no street precision)."""
    cleaned = clean_address_string(raw_address)
    if not cleaned:
        return None, None

    results = geocode_with_retry(cleaned)

    if not results or not any(matched_target(r) for r in results):
        if not any(city in cleaned for city in TARGET_CITIES):
            for city in TARGET_CITIES:
                forced = geocode_with_retry(f"{cleaned}, {city}")
                if forced and any(matched_target(r) for r in forced):
                    results = forced
                    break

    if not results:
        return None, None

    best = results[0]
    if not has_street_precision(best):
        return None, None
    return best.get('formatted_address', ''), city_of(best)


def main():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SHEET_ID).sheet1
    data = sheet.get_all_values()
    rows = data[1:]

    kept_rows = []
    updated_count = 0
    removed_tlv_far = 0
    unresolved_count = 0
    changes_log = []

    for i, row in enumerate(rows, start=2):
        row = list(row) + [""] * (14 - len(row))
        raw_address = row[13].strip()
        if not raw_address:
            kept_rows.append(row)
            continue

        if "נמל התעופה" in raw_address:
            # known recurring LLM hallucination (see apartment_bot._reject_hallucinated_address) --
            # we no longer have the source post text to verify against, so treat as unconditionally wrong
            changes_log.append(f"row {i}: BLANKED known hallucination -- {raw_address!r}")
            row[13] = ""
            row[3] = ""
            kept_rows.append(row)
            continue

        try:
            canonical, city = geocode_address(raw_address)
        except googlemaps.exceptions.ApiError as e:
            print(f"row {i}: geocode ERROR for {raw_address!r}: {e}")
            unresolved_count += 1
            kept_rows.append(row)
            continue

        if not canonical or not city:
            unresolved_count += 1
            changes_log.append(f"row {i}: UNRESOLVED, left as-is -- {raw_address!r}")
            kept_rows.append(row)
            continue

        if any(c in city for c in TARGET_CITIES):
            dist_text, dist_meters = get_walking_distance(canonical)
            if canonical != raw_address or dist_text != row[3]:
                changes_log.append(f"row {i}: UPDATE [{city}] {raw_address!r} -> {canonical!r} | dist {row[3]!r} -> {dist_text!r}")
            row[13] = canonical
            row[3] = dist_text
            updated_count += 1
            kept_rows.append(row)
        elif "תל אביב" in city:
            dist_text, dist_meters = get_walking_distance(canonical)
            if dist_meters <= MAX_TLV_DISTANCE_M:
                changes_log.append(f"row {i}: TLV KEEP ({dist_text}) {raw_address!r} -> {canonical!r}")
                row[13] = canonical
                row[3] = dist_text
                updated_count += 1
                kept_rows.append(row)
            else:
                changes_log.append(f"row {i}: TLV DROP ({dist_text}, >{MAX_TLV_DISTANCE_M}m) {raw_address!r} -> {canonical!r}")
                removed_tlv_far += 1
        else:
            changes_log.append(f"row {i}: OTHER CITY '{city}', left as-is -- {raw_address!r} -> {canonical!r}")
            kept_rows.append(row)

        time.sleep(0.15)

    print("\n".join(changes_log))
    print()
    print(f"Total rows: {len(rows)}")
    print(f"Updated (canonicalized): {updated_count}")
    print(f"Removed (Tel Aviv, >4km): {removed_tlv_far}")
    print(f"Unresolved (left as-is): {unresolved_count}")
    print(f"Rows after fix: {len(kept_rows)}")

    if DRY_RUN:
        print("\nDRY RUN -- no changes written. Re-run with --write to apply.")
        return

    last_col = chr(ord('A') + len(SHEET_HEADERS) - 1)
    sheet.batch_clear([f"A2:{last_col}{len(rows) + 1}"])
    if kept_rows:
        sheet.update(range_name=f"A2:{last_col}{len(kept_rows) + 1}", values=kept_rows)
    print("\nWrote changes to sheet.")

    removed_dupes, final_count = dedupe_and_sort_sheet(sheet)
    print(f"Post-canonicalization dedupe: removed {removed_dupes} newly-created duplicate(s), final {final_count} listings.")


if __name__ == "__main__":
    main()
