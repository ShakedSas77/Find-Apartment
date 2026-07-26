"""
=== Listing evaluation pipeline ===

process_candidate_listing() is the public entry point: takes plain data (URL,
text, date — no source-specific types), runs it through LLM parse ->
_evaluate_post_data (threshold checks + normalization) -> _build_row ->
queue-for-sheet-write. Source-agnostic by design so a second source (Yad2)
can feed the same pipeline without a hand-copied duplicate — see CLAUDE.md.
"""
import re
import threading
from datetime import datetime

import storage
import scoring
from config import (
    MIN_PRICE, MAX_PRICE, MIN_ROOMS, MAX_ROOMS, INCLUDE_PRICE_UNKNOWN,
    MAX_WALKING_DISTANCE_KM, EXCLUDED_LOCATIONS, ROOMMATE_KEYWORDS,
    NEGATIVE_KEYWORDS, ROOMS_PRE_FILTER_REGEX,
)
from core.util import _safe_print, _sheet_lock
from core.errors import GmapsQuotaHalted, HeadlessCheckpointAbort
from core.llm import analyze_post_with_llm
from core.normalize import (
    BIDI_RE, _PRICE_CONTEXT_RE, _normalize_bimonthly_fee, _warn_if_fee_implausible,
    _strip_foreign_letters, _reject_hallucinated_address, _parse_floor, _detect_agent,
    _classify_parking, _normalize_entry_date, map_bool, _sheet_safe_cell,
    _ROOMMATE_COUPLE_EXCEPTION_RE,
)
from core.dedupe import _listing_dedupe_key, _known_added_listing_keys, _maybe_enrich_duplicate_address
from maps import _format_address_display, get_walking_distance

# distance_source values trustworthy enough to gate the > MAX_WALKING_DISTANCE_KM
# rejection on. "straight_line_estimate" (distance_fallback.py, used only once
# GMAPS_MONTHLY_CAP is hit) is a real, if rougher, distance figure — same
# treatment as a Google-sourced one. Anything else ("skipped"/"quota"/"cache"
# with no real number) must NOT be gated on, or a listing with no real distance
# data would look like it passed the filter.
_DISTANCE_FILTERABLE_SOURCES = {"google_maps", "straight_line_estimate"}

def _evaluate_post_data(data: dict, text: str, group_url: str = "") -> tuple[str, dict]:
    """
    Runs the threshold checks (rooms/price, including the regex "second chance")
    and all field normalization. Shared between _scan_group and --reparse-rejected
    so the logic stays identical. Returns (verdict, fields) — fields holds what's
    needed to build a sheet row when verdict is storage.VERDICT_ADDED.
    """
    rooms = data.get("rooms")
    price = data.get("price")

    try:
        rooms_val = float(rooms) if rooms is not None else 0.0
        rooms_missing_or_invalid = rooms is None
    except (TypeError, ValueError):
        rooms_val = 0.0
        rooms_missing_or_invalid = True

    try:
        price_val = float(price) if price is not None else 0.0
        price_missing_or_invalid = price is None
    except (TypeError, ValueError):
        price_val = 0.0
        price_missing_or_invalid = True

    # --- Error correction and "second chance" for price ---
    # The "second chance" (searching the text for a number) only runs when the
    # model returned no price at all — if the model did return a real value
    # (even if out of budget, e.g. 7200), it's not overridden by some other
    # number just because it's near a price marker in the text (could be "old
    # price", annual committee fee, etc.) — that's exactly what created false
    # positives before.
    if not (MIN_PRICE <= price_val <= MAX_PRICE):
        clean_text = BIDI_RE.sub('', text)
        clean_text = re.sub(r'(?<=[0-9])[.,\s](?=[0-9]{3}(?![0-9]))', '', clean_text)
        possible_prices = [
            int(m.group(1) or m.group(2))
            for m in _PRICE_CONTEXT_RE.finditer(clean_text)
        ]
        if possible_prices:
            valid_prices = [p for p in possible_prices if MIN_PRICE <= p <= MAX_PRICE]
            if valid_prices and price_missing_or_invalid:
                price_val = float(valid_prices[0])
            elif price_val < 3000 or price_val > 30000:
                price_val = float(possible_prices[0])

    # --- Second chance for rooms: only when the model returned no valid room count at all ---
    # (doesn't override a real numeric value the model did return, even if outside the target range)
    if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS) and rooms_missing_or_invalid:
        clean_text_rooms = BIDI_RE.sub('', text)
        room_matches = [float(r) for r in re.findall(r'([1-9](?:\.5)?)\s*חד', clean_text_rooms)]
        valid_rooms = [r for r in room_matches if MIN_ROOMS <= r <= MAX_ROOMS]
        if valid_rooms:
            rooms_val = valid_rooms[0]

    if not (MIN_ROOMS <= rooms_val <= MAX_ROOMS):
        return storage.VERDICT_REJECTED_ROOMS, {"rooms_val": rooms_val, "price_val": price_val}

    # Price 0/missing after both paths (LLM + regex "second chance") = "no price
    # stated in the post" ("contact for details") — a relevant lead, not a
    # rejection like a real price that's simply out of range.
    price_unknown = price_val == 0 and price_missing_or_invalid
    if not price_unknown and not (MIN_PRICE <= price_val <= MAX_PRICE):
        return storage.VERDICT_REJECTED_PRICE, {"rooms_val": rooms_val, "price_val": price_val}
    if price_unknown and not INCLUDE_PRICE_UNKNOWN:
        return storage.VERDICT_PRICE_UNKNOWN, {"rooms_val": rooms_val, "price_val": price_val}

    arnona = _normalize_bimonthly_fee(data.get("arnona") or "")
    vaad = _normalize_bimonthly_fee(data.get("vaad") or "")
    _warn_if_fee_implausible("Vaad bayit", vaad, 2400)
    _warn_if_fee_implausible("Arnona", arnona, 3000)

    address = _strip_foreign_letters(data.get("address") or "")
    address = _reject_hallucinated_address(address, text)
    address = _format_address_display(address, group_url)

    dup_key = _listing_dedupe_key(address, rooms_val, price_val)
    if dup_key and dup_key in _known_added_listing_keys():
        _maybe_enrich_duplicate_address(dup_key, address)
        return storage.VERDICT_DUPLICATE_LISTING, {"rooms_val": rooms_val, "price_val": price_val, "address": address}

    floor = _parse_floor(data.get("floor") or "")
    is_agent = _detect_agent(text, data.get("is_agent"))
    parking = _classify_parking(data.get("parking") or "")
    entry_date = _normalize_entry_date(data.get("entry_date") or "")

    fields = {
        "rooms_val": rooms_val, "price_val": price_val, "arnona": arnona, "vaad": vaad,
        "address": address, "floor": floor, "is_agent": is_agent, "parking": parking,
        "entry_date": entry_date, "elevator": data.get("elevator"),
        "shelter": data.get("shelter"),
    }
    return storage.VERDICT_ADDED, fields

def _build_row(post_url: str, fb_post_date: str, fields: dict) -> list:
    dist_text, dist_meters, address_confidence, address_warning, distance_source = get_walking_distance(fields["address"])
    fields["distance_text"] = dist_text
    fields["distance_meters"] = dist_meters
    fields["address_confidence"] = address_confidence
    fields["address_warning"] = address_warning
    fields["distance_source"] = distance_source

    row = [
        post_url,
        int(fields["price_val"]) if fields["price_val"] else "",
        fields["rooms_val"],
        dist_text,
        fields["entry_date"],
        fields["floor"],
        map_bool(fields["elevator"]),
        fields["parking"],
        fields["arnona"],
        fields["vaad"],
        map_bool(fields["shelter"]),
        map_bool(fields["is_agent"]),
        fb_post_date,
        fields["address"],
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        scoring.compute_fit_score(fields),
    ]
    return [_sheet_safe_cell(v) for v in row]

def _analysis_from_fields(fields: dict, post_date: str, reject_reason: str = "", model_used: str = "gemini_or_ollama") -> dict:
    return {
        "price_val": fields.get("price_val"),
        "rooms_val": fields.get("rooms_val"),
        "address": fields.get("address"),
        "address_confidence": fields.get("address_confidence"),
        "distance_text": fields.get("distance_text"),
        "distance_meters": fields.get("distance_meters"),
        "post_date": post_date,
        "reject_reason": reject_reason,
        "model_used": model_used,
    }

def process_candidate_listing(post_url: str, text: str, post_date: str, group_url: str, label: str, live: bool,
                               seen_urls: set, pending_rows: list, pending_seen_urls: set,
                               pending_records: list, stats: dict, local_lock: threading.Lock,
                               pending_telegram: list | None = None) -> None:
    """
    Runs one scraped listing (any source — FB post, Yad2 ad) through LLM parse
    -> _evaluate_post_data -> _build_row -> queue-for-sheet-write. Pulled out of
    _scan_group_page's process_post closure so a second source (Yad2) can feed
    the same pipeline without a second hand-copied version of this logic —
    see CLAUDE.md on keeping _evaluate_post_data/_build_row call sites in sync.
    """
    _safe_print(f"[{label}] Analyzing post (URL: {post_url})...")

    try:
        data = analyze_post_with_llm(text)
    except (GmapsQuotaHalted, HeadlessCheckpointAbort):
        raise

    with local_lock:
        stats["llm_parsed"] += 1
    if not data:
        _safe_print(f"    [{label}] Skipped: LLM failed to parse or returned no data.")
        storage.record_post(post_url, group_url, text, storage.VERDICT_PARSE_FAILED,
                             analysis=_analysis_from_fields({}, post_date, storage.VERDICT_PARSE_FAILED))
        return

    verdict, fields = _evaluate_post_data(data, text, group_url)
    if verdict == storage.VERDICT_REJECTED_ROOMS:
        _safe_print(f"    [{label}] Skipped: Room count is not suitable ({fields['rooms_val']}).")
        storage.record_post(post_url, group_url, text, verdict, data, analysis=_analysis_from_fields(fields, post_date, verdict))
        return
    if verdict == storage.VERDICT_REJECTED_PRICE:
        _safe_print(f"    [{label}] Skipped: Price is not suitable ({int(fields['price_val']):,} ₪).")
        storage.record_post(post_url, group_url, text, verdict, data, analysis=_analysis_from_fields(fields, post_date, verdict))
        return
    if verdict == storage.VERDICT_PRICE_UNKNOWN:
        _safe_print(f"    [{label}] Skipped: no price stated in post (contact seller directly).")
        storage.record_post(post_url, group_url, text, verdict, data, analysis=_analysis_from_fields(fields, post_date, verdict))
        return
    if verdict == storage.VERDICT_DUPLICATE_LISTING:
        _safe_print(f"    [{label}] Skipped: repost of an already-added listing ({fields['address']}, {fields['rooms_val']} rooms, {int(fields['price_val']):,} ₪).")
        storage.record_post(post_url, group_url, text, verdict, data, analysis=_analysis_from_fields(fields, post_date, verdict))
        return

    new_row = _build_row(post_url, post_date, fields)

    if fields.get("distance_source") == "excluded_south":
        _safe_print(f"    [{label}] Skipped: location south of Tel Aviv HaHagana station.")
        storage.record_post(post_url, group_url, text, storage.VERDICT_REJECTED_LOCATION, data, analysis=_analysis_from_fields(fields, post_date, storage.VERDICT_REJECTED_LOCATION))
        return

    dist_meters = fields.get("distance_meters")
    # Only reject on distance if a real distance was actually computed against
    # Google Maps — dist_meters is a placeholder (999999/inf) for an uncertain
    # address/quota/error, not a real distance, and shouldn't disqualify a
    # post as if it were far away (see CLAUDE.md, verified 2026-07-18).
    if fields.get("distance_source") in _DISTANCE_FILTERABLE_SOURCES and dist_meters is not None and (dist_meters / 1000.0) > MAX_WALKING_DISTANCE_KM:
        _safe_print(f"    [{label}] Skipped: Distance too far ({fields.get('distance_text')} > {MAX_WALKING_DISTANCE_KM}km).")
        storage.record_post(post_url, group_url, text, storage.VERDICT_REJECTED_DISTANCE, data, analysis=_analysis_from_fields(fields, post_date, storage.VERDICT_REJECTED_DISTANCE))
        return

    with _sheet_lock:
        seen_now = post_url in seen_urls or post_url in pending_seen_urls
        if not seen_now:
            pending_rows.append(new_row)
            pending_seen_urls.add(post_url)
            pending_records.append((post_url, group_url, text, data, _analysis_from_fields(fields, post_date)))
            if pending_telegram is not None:
                pending_telegram.append((post_url, dict(fields), new_row[-1]))
            with local_lock:
                stats["added"] += 1
            price_display = f"{int(fields['price_val']):,} ₪" if fields['price_val'] else "מחיר לא צוין"
            prefix = "SUCCESS" if live else "DRY RUN"
            verb = "queued" if live else "would queue (pass --live to commit)"
            _safe_print(f"    {prefix}: [{label}] Apartment {verb}: {fields['rooms_val']} rooms | {price_display} | {new_row[3]} | Address: {fields['address']}")
        else:
            _safe_print(f"    [{label}] Skipped before queueing: duplicate URL.")

def _replay_text_prefilters(text: str) -> tuple[str, dict] | None:
    """
    Content-based filter checks (not URL/date/live cache) for --replay — mirrors
    _scan_group_page (where the conditions also live for the live group_label
    prints), but here without a browser. If you change NEGATIVE_KEYWORDS/
    EXCLUDED_LOCATIONS/etc., update both places.
    Returns (verdict, reject_reason) if it should stop, otherwise None to continue to the LLM.
    """
    excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text]
    if excluded_found:
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"excluded_location:{excluded_found[0]}"}

    roommate_match = re.search(ROOMMATE_KEYWORDS, text)
    if roommate_match and not _ROOMMATE_COUPLE_EXCEPTION_RE.search(text):
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"roommate_keyword:{roommate_match.group(0)}"}

    neg_match = re.search(NEGATIVE_KEYWORDS, text)
    if neg_match:
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"negative_keyword:{neg_match.group(0)}"}

    sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
    if sale_price_match:
        return storage.VERDICT_PREFILTERED, {"reject_reason": f"for_sale:{sale_price_match.group(0).strip()}"}

    if not re.search(ROOMS_PRE_FILTER_REGEX, text):
        return storage.VERDICT_PREFILTERED, {"reject_reason": "room_count_mismatch"}

    return None
