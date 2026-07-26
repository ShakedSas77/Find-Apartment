"""
=== Cross-post dedup ===

Catching the same listing reposted (same or different group, different URL,
possibly reworded by hand) — both a text-fingerprint check (before an LLM
call) and an address+rooms+price key check (after parsing, against every
listing ever added). No gspread/Sheets — only storage.py + core.normalize.
"""
import hashlib
import re

import storage
from core.normalize import _PHONE_RE, _PRICE_CONTEXT_RE, _ROOMS_EXTRACT_RE, _normalize_address_key, _CORNER_HINT_RE

# Strips the volatile per-repost header (author name, online-status/"Follow"
# UI noise, relative timestamp) up through FB's constant "Shared with Public
# group" marker — so the same listing crossposted to multiple groups
# normalizes to the same text even though each post's own header differs.
_POST_HEADER_RE = re.compile(r'^.*?Shared with Public group', re.DOTALL)

# Below this length a normalized text is too short/generic to trust as a
# dedup signal — collision risk on short strings isn't worth the LLM-call
# savings, and a false-positive here would silently drop a real listing.
_TEXT_DEDUP_MIN_LEN = 40

def _normalize_dedup_text(text: str) -> str:
    core = _POST_HEADER_RE.sub('', text, count=1)
    return re.sub(r'\s+', ' ', core).strip()

def _phone_price_rooms_fingerprint(text: str) -> str:
    """
    Phone number(s) + price + room count, when all three are present — far
    more robust to manual rewording between crossposts than comparing the ad
    text itself, since a poster's contact number/price/room count stay fixed
    even when the description around them gets reworded. Combining all three
    (not phone alone) protects against the real risk of one landlord/agent
    posting multiple DIFFERENT apartments under the same number — those will
    very likely differ in price or room count, so they won't collide here.
    """
    phones = sorted(set(re.sub(r'[-\s]', '', m) for m in _PHONE_RE.findall(text)))
    if not phones:
        return ""
    price_match = _PRICE_CONTEXT_RE.search(text)
    price = (price_match.group(1) or price_match.group(2)) if price_match else None
    rooms_match = _ROOMS_EXTRACT_RE.search(text)
    rooms = rooms_match.group(1) if rooms_match else None
    if not price or not rooms:
        return ""
    return f"{'|'.join(phones)}#{price}#{rooms}"

def _text_dedup_hash(text: str) -> str:
    """
    Fingerprint used to catch a crosspost of the same listing to a different
    group before it burns an LLM call. Prefers the phone+price+rooms
    fingerprint (survives hand-rewording of the ad text); falls back to the
    header-stripped exact text when no phone/price/rooms combination is
    found. Both are deliberately conservative about false positives: a
    missed duplicate just costs one extra LLM call (no regression), while a
    wrong match would silently drop a real, distinct listing.
    """
    fingerprint = _phone_price_rooms_fingerprint(text)
    if fingerprint:
        return hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
    normalized = _normalize_dedup_text(text)
    if len(normalized) < _TEXT_DEDUP_MIN_LEN:
        return ""
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

def _listing_dedupe_key(address: str, rooms, price) -> tuple | None:
    """
    Normalized (address, rooms, price) key identifying the same physical
    listing regardless of URL/repost wording. None when the address is too
    weak to key on (missing/short/generic) — such listings are never merged
    on address+rooms+price alone, only on exact URL, so distinct apartments
    with a thin address don't get accidentally treated as the same one.
    """
    address_key = _normalize_address_key(address)
    if not address_key or len(address_key) < 4:
        return None
    try:
        rooms_key = f"{float(rooms):.1f}"
    except (TypeError, ValueError):
        rooms_key = str(rooms)
    try:
        price_key = str(int(float(price)))
    except (TypeError, ValueError):
        price_key = str(price)
    return (address_key, rooms_key, price_key)

def _listing_key(row: list, db_data: dict[str, dict] | None = None) -> tuple:
    """
    Keys a row by address+rooms+price sourced from the bot's own original
    parse in the DB (db_data, keyed by URL) when available, falling back to
    the live sheet cells otherwise. Matching off the DB record means a manual
    edit to a sheet cell (e.g. fixing a price) can't stop a later crosspost
    of the same listing, added under a different URL, from still being
    recognized as a duplicate.
    """
    url = row[0] if len(row) > 0 else ""
    db_row = (db_data or {}).get(url)

    raw_address = db_row["address"] if db_row and db_row.get("address") else (row[13] if len(row) > 13 else "")
    raw_rooms = db_row["rooms_val"] if db_row and db_row.get("rooms_val") is not None else (row[2] if len(row) > 2 else "")
    raw_price = db_row["price_val"] if db_row and db_row.get("price_val") is not None else (row[1] if len(row) > 1 else "")

    key = _listing_dedupe_key(raw_address, raw_rooms, raw_price)
    return ("listing",) + key if key else ("url", url)

def _known_added_listing_keys() -> set[tuple]:
    """
    Dedupe keys for every listing ever added, across the DB's whole history —
    not just what's still in the (pruned/rewritten) sheet. Used to catch a
    listing reposted under a brand-new URL (common for agents farming
    multiple groups daily) before it burns an LLM call and re-adds a row that
    dedupe_and_sort_sheet would just merge away anyway.
    """
    keys = set()
    for row in storage.get_added_listing_data().values():
        key = _listing_dedupe_key(row.get("address"), row.get("rooms_val"), row.get("price_val"))
        if key:
            keys.add(key)
    return keys

def _maybe_enrich_duplicate_address(dup_key: tuple, new_address: str):
    """
    A rejected duplicate sometimes states a more specific address (adds a
    "פינת X" corner qualifier) than the originally-added post did —
    _listing_dedupe_key deliberately treats both as the same listing so the
    duplicate is never added as a second row, but that shouldn't mean losing
    the more useful address text. If this duplicate's address has a corner
    qualifier the stored one lacks, update the original's stored address so
    the next dedupe_and_sort_sheet() pass can sync it into the sheet's כתובת
    cell.
    """
    if not _CORNER_HINT_RE.search(new_address):
        return
    for url, row in storage.get_added_listing_data().items():
        existing_address = row.get("address") or ""
        key = _listing_dedupe_key(existing_address, row.get("rooms_val"), row.get("price_val"))
        if key == dup_key and not _CORNER_HINT_RE.search(existing_address):
            storage.update_added_listing_address(url, new_address)
            return
