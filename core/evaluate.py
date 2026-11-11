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
from datetime import date, datetime

from core import storage
from core import scoring
from config import (
    MIN_PRICE, MAX_PRICE, MIN_ROOMS, MAX_ROOMS, INCLUDE_PRICE_UNKNOWN,
    MAX_WALKING_DISTANCE_KM, EXCLUDED_LOCATIONS, ROOMMATE_KEYWORDS,
    NEGATIVE_KEYWORDS, ROOMS_PRE_FILTER_REGEX, SCORE_ENTRY_DATE_MAX_DAYS_AHEAD,
    ARNONA_MAX_BIMONTHLY, PRICE_PREFILTER_MIN_RENT, SHEET_HEADERS, TELEGRAM_ENABLED, EXCLUDED_STREETS_RAMAT_GAN_ONLY,
)
from core.util import _safe_print, _sheet_lock, timed
from core.errors import GmapsQuotaHalted, HeadlessCheckpointAbort
from core.llm import analyze_post_with_llm, last_model_used
from core.normalize import (
    BIDI_RE, _extract_price_candidates, _normalize_bimonthly_fee, _warn_if_fee_implausible,
    _strip_foreign_letters, _reject_hallucinated_address, _is_price_in_text, _parse_floor, _detect_agent,
    _classify_parking, _normalize_entry_date, resolve_entry_date, map_bool, _sheet_safe_cell,
    _ROOMMATE_COUPLE_EXCEPTION_RE, _first_mobile_phone, _PHONE_RE, _SEEKING_A_TENANT_RE, _ENGLISH_ROOMS_RE, _STUDIO_AMENITY_RE, _CITY_DISPLAY_RES,
    _SHARED_WORD_PREFIX, _ROOMMATE_FRIENDLY_RE, _reads_as_an_offer,
)
from core.dedupe import _listing_dedupe_key, _known_added_listing_keys, _maybe_enrich_duplicate_address
from integrations.maps import _format_address_display, get_walking_distance
from integrations.sheets import _append_rows_batch
from integrations import telegram_notifier

_SCORE_COL_INDEX = SHEET_HEADERS.index("ציון התאמה")

# distance_source values trustworthy enough to gate the > MAX_WALKING_DISTANCE_KM
# rejection on. "straight_line_estimate" (distance_fallback.py, used only once
# GMAPS_MONTHLY_CAP is hit) is a real, if rougher, distance figure — same
# treatment as a Google-sourced one. Anything else ("skipped"/"quota"/"cache"
# with no real number) must NOT be gated on, or a listing with no real distance
# data would look like it passed the filter.
_DISTANCE_FILTERABLE_SOURCES = {"google_maps", "straight_line_estimate"}

# Dedupe keys of listings queued during this process's run (guarded by _sheet_lock) — see process_candidate_listing.
_run_queued_listing_keys: set[tuple] = set()

def _price_prefilter_reject(text: str) -> int | None:
    """
    Returns the first stated rent if every rent-sized price in the text is outside
    [MIN_PRICE, MAX_PRICE], so the post can skip the LLM. One in-range candidate
    (e.g. an "old price" next to the real one) keeps it going to the LLM, and so does
    any bare in-range number anywhere in the text (a stepped price like "5200 until
    March, 5500 after" only has a marker next to the first figure).
    """
    candidates = [p for p in _extract_price_candidates(text) if p >= PRICE_PREFILTER_MIN_RENT]
    if not candidates:
        return None
    flat_text = re.sub(r'(?<=[0-9])[.,\s](?=[0-9]{3}(?![0-9]))', '', BIDI_RE.sub('', text))
    bare_numbers = (int(n) for n in re.findall(r'(?<![0-9])[0-9]{4,5}(?![0-9])', flat_text))
    if any(MIN_PRICE <= n <= MAX_PRICE for n in bare_numbers):
        return None
    return candidates[0]

def _evaluate_post_data(data: dict, text: str, group_url: str = "", post_url: str | None = None) -> tuple[str, dict]:
    """
    Runs the threshold checks (rooms/price, including the regex "second chance")
    and all field normalization. Shared between _scan_group and --reparse-rejected
    so the logic stays identical. Returns (verdict, fields) — fields holds what's
    needed to build a sheet row when verdict is storage.VERDICT_ADDED. post_url
    keeps an already-added post from matching its own record as a duplicate.
    """
    rooms = data.get("rooms")
    price = data.get("price")
    if price is not None and not _is_price_in_text(price, text):
        # A price the post never states is the model's invention, not a reading; treated as missing.
        price = None

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
        possible_prices = _extract_price_candidates(text)
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
    if arnona and arnona > ARNONA_MAX_BIMONTHLY:
        # A value this high is an extraction bug (e.g. a price or phone fragment), not a real bill —
        # blank it so it neither lands in the sheet nor inflates the total monthly cost.
        _safe_print(f"    WARNING: Arnona {arnona} exceeds {ARNONA_MAX_BIMONTHLY} bi-monthly - treating as missing.")
        arnona = ""

    address = _strip_foreign_letters(data.get("address") or "")
    address = _reject_hallucinated_address(address, text)
    address = _format_address_display(address, group_url)

    dup_key = _listing_dedupe_key(address, rooms_val, price_val)
    if dup_key and dup_key in _known_added_listing_keys(exclude_url=post_url):
        _maybe_enrich_duplicate_address(dup_key, address)
        return storage.VERDICT_DUPLICATE_LISTING, {"rooms_val": rooms_val, "price_val": price_val, "address": address}

    floor = _parse_floor(data.get("floor") or "")
    is_agent = _detect_agent(text, data.get("is_agent"))
    parking = _classify_parking(data.get("parking") or "")
    entry_date = _normalize_entry_date(data.get("entry_date") or "")

    # Reject only a confidently-resolved far-out date — an unparseable/missing
    # one (resolve_entry_date returns None) is never rejected on this basis,
    # same caution rule as weak addresses elsewhere in this pipeline.
    resolved_entry_date = resolve_entry_date(entry_date)
    if resolved_entry_date and (resolved_entry_date - date.today()).days > SCORE_ENTRY_DATE_MAX_DAYS_AHEAD:
        return storage.VERDICT_REJECTED_ENTRY_DATE, {
            "rooms_val": rooms_val, "price_val": price_val, "address": address, "entry_date": entry_date,
        }

    fields = {
        "rooms_val": rooms_val, "price_val": price_val, "arnona": arnona, "vaad": vaad,
        "address": address, "floor": floor, "is_agent": is_agent, "parking": parking,
        "entry_date": entry_date, "elevator": data.get("elevator"),
        "shelter": data.get("shelter"),
        "in_focus_area": scoring.mentions_focus_neighborhood(text, address),
    }
    return storage.VERDICT_ADDED, fields

def _build_row(post_url: str, fb_post_date: str, fields: dict) -> list:
    with timed("maps"):
        dist_text, dist_meters, address_confidence, address_warning, distance_source, lat, lon = get_walking_distance(fields["address"])
    fields["distance_text"] = dist_text
    fields["distance_meters"] = dist_meters
    fields["address_confidence"] = address_confidence
    fields["address_warning"] = address_warning
    fields["distance_source"] = distance_source
    fields["lat"] = lat
    fields["lon"] = lon

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
        scoring.compute_total_monthly_cost(fields),
    ]
    return [_sheet_safe_cell(v) for v in row]

def _analysis_from_fields(fields: dict, post_date: str, reject_reason: str = "", model_used: str | None = None) -> dict:
    # model_used defaults to this thread's last LLM call: every caller records a post on the same
    # thread right after analyzing it.
    return {
        "price_val": fields.get("price_val"),
        "rooms_val": fields.get("rooms_val"),
        "address": fields.get("address"),
        "address_confidence": fields.get("address_confidence"),
        "distance_text": fields.get("distance_text"),
        "distance_meters": fields.get("distance_meters"),
        "post_date": post_date,
        "reject_reason": reject_reason,
        "model_used": model_used or last_model_used(),
    }

def process_candidate_listing(post_url: str, text: str, post_date: str, group_url: str, label: str, live: bool,
                               seen_urls: set, pending_rows: list, pending_seen_urls: set,
                               pending_records: list, stats: dict, local_lock: threading.Lock,
                               pending_telegram: list | None = None, parsed: dict | None = None) -> None:
    """
    Runs one scraped listing (any source) through LLM parse -> _evaluate_post_data
    -> _build_row -> queue-for-sheet-write. Pulled out of _scan_group_page's
    process_post closure so another source can feed the same pipeline without a
    second hand-copied version of this logic — see CLAUDE.md on keeping
    _evaluate_post_data/_build_row call sites in sync. A source that already
    has the fields (Yad2's alert emails) passes them as `parsed` and skips the LLM.
    """
    _safe_print(f"[{label}] Analyzing post (URL: {post_url})...")

    if parsed is not None:
        data = parsed
    else:
        try:
            with timed("llm"):
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
    if verdict == storage.VERDICT_REJECTED_ENTRY_DATE:
        _safe_print(f"    [{label}] Skipped: entry date too far out ({fields.get('entry_date')}).")
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

    # _known_added_listing_keys() only knows listings already *recorded* as added, which happens at
    # the group's batch flush — so a repost sitting in the same in-flight window (same group batch,
    # or another group scanning in parallel) would slip through and be sent to Telegram twice.
    # Claiming the key here, under the same lock that queues the row, closes that window.
    listing_key = _listing_dedupe_key(fields["address"], fields["rooms_val"], fields["price_val"])
    with _sheet_lock:
        seen_now = post_url in seen_urls or post_url in pending_seen_urls
        repost_in_flight = not seen_now and listing_key is not None and listing_key in _run_queued_listing_keys
        if repost_in_flight:
            pass
        elif not seen_now:
            if listing_key is not None:
                _run_queued_listing_keys.add(listing_key)
            pending_rows.append(new_row)
            pending_seen_urls.add(post_url)
            pending_records.append((post_url, group_url, text, data, _analysis_from_fields(fields, post_date)))
            if pending_telegram is not None:
                pending_telegram.append((post_url, dict(fields, contact_phone=_first_mobile_phone(text)),
                                         new_row[_SCORE_COL_INDEX]))
            with local_lock:
                stats["added"] += 1
            price_display = f"{int(fields['price_val']):,} ₪" if fields['price_val'] else "מחיר לא צוין"
            prefix = "SUCCESS" if live else "DRY RUN"
            verb = "queued" if live else "would queue (rerun without --dry-run to commit)"
            _safe_print(f"    {prefix}: [{label}] Apartment {verb}: {fields['rooms_val']} rooms | {price_display} | {new_row[3]} | Address: {fields['address']}")
        else:
            _safe_print(f"    [{label}] Skipped before queueing: duplicate URL.")
    if repost_in_flight:
        _safe_print(f"    [{label}] Skipped: repost of a listing queued earlier this run ({fields['address']}, {fields['rooms_val']} rooms, {int(fields['price_val']):,} ₪).")
        storage.record_post(post_url, group_url, text, storage.VERDICT_DUPLICATE_LISTING, data,
                             analysis=_analysis_from_fields(fields, post_date, storage.VERDICT_DUPLICATE_LISTING))

def flush_pending_listings(sheet, seen_urls: set, pending_rows: list, pending_seen_urls: set, pending_records: list,
                           pending_telegram: list, stats: dict, live: bool, label: str) -> None:
    """
    Commits one source batch queued by process_candidate_listing: appends the rows to the sheet,
    records each as added, then sends its Telegram alert. Kept separate from the scan loop so any
    listing source commits in the same order — sheet first, alerts last. A dry run only reports what
    it would write.
    """
    if not pending_rows:
        return
    if not live:
        _safe_print(f"    [{label}] DRY RUN: {len(pending_rows)} row(s) would be written to Google Sheets (skipped — rerun without --dry-run to commit).")
        return
    with _sheet_lock:
        try:
            with timed("sheets"):
                _append_rows_batch(sheet, pending_rows)
            seen_urls.update(pending_seen_urls)
            for record_url, record_group_url, record_text, record_data, record_analysis in pending_records:
                storage.record_post(record_url, record_group_url, record_text, storage.VERDICT_ADDED, record_data, analysis=record_analysis)
            _safe_print(f"    [{label}] Batch wrote {len(pending_rows)} row(s) to Google Sheets.")
            if TELEGRAM_ENABLED:
                for tg_url, tg_fields, tg_score in pending_telegram:
                    try:
                        telegram_notifier.send_listing_alert(tg_url, tg_fields, tg_score)
                    except Exception as e:
                        _safe_print(f"    WARNING: [{label}] Telegram alert failed for {tg_url}: {e}")
        except Exception as e:
            _safe_print(f"    ERROR: [{label}] batch writing to sheet: {e}")
            stats["added"] -= len(pending_rows)

def _content_prefilter(text: str, post_url: str = "") -> tuple[str, dict, str] | None:
    """
    Content-based filter checks (nothing URL/date/DB-dependent) shared by the live scan
    (_scan_group_page) and --replay. Returns (verdict, analysis, message) for the first
    check that rejects the post, or None to continue to the LLM. `message` is the
    scan's console reason; `post_url` only enriches the room-count message.
    """
    excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text
                      and not (loc in EXCLUDED_STREETS_RAMAT_GAN_ONLY and _names_only_other_target_city(text))]
    if excluded_found:
        return (storage.VERDICT_PREFILTERED, {"reject_reason": f"excluded_location:{excluded_found[0]}"},
                f"Contains excluded location '{excluded_found[0]}'.")

    # Roommate/shared-room posts, unless explicitly also couple-friendly
    roommate_match = next((m for m in re.finditer(ROOMMATE_KEYWORDS, text)
                           if text[m.start() - 1:m.start()] != _SHARED_WORD_PREFIX
                           and not _ROOMMATE_FRIENDLY_RE.search(text[max(0, m.start() - 40):m.start()])), None)
    if roommate_match and not _ROOMMATE_COUPLE_EXCEPTION_RE.search(text):
        return (storage.VERDICT_PREFILTERED, {"reject_reason": f"roommate_keyword:{roommate_match.group(0)}"},
                f"Contains negative keyword '{roommate_match.group(0)}'.")

    # Other obvious non-relevant posts (sublets, studios, commercial, seekers)
    neg_match = next((m for m in re.finditer(NEGATIVE_KEYWORDS, text)
                      if not (m.group(0).startswith("מחפש")
                              and (_SEEKING_A_TENANT_RE.match(text, m.start()) or _reads_as_an_offer(text)))
                      and not (m.group(0) == "סטודיו" and _STUDIO_AMENITY_RE.match(text, m.start()))), None)
    if neg_match:
        return (storage.VERDICT_PREFILTERED, {"reject_reason": f"negative_keyword:{neg_match.group(0)}"},
                f"Contains negative keyword '{neg_match.group(0)}'.")

    # Sales instead of rentals. Many agent posts omit the word "for sale" and just write
    # "Price: 3,395,000 ₪" — a 7+ digit price (or "X million") is an unambiguous sale signal.
    # The separator requirement ([.,]\d{3}) blocks a false match on a phone number (050-1234567).
    sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
    if sale_price_match:
        return (storage.VERDICT_PREFILTERED, {"reject_reason": f"for_sale:{sale_price_match.group(0).strip()}"},
                f"Apartment for sale (price {sale_price_match.group(0).strip()}).")

    if not re.search(ROOMS_PRE_FILTER_REGEX, text) and not _ENGLISH_ROOMS_RE.search(text):
        clean_snip = text[:100].replace('\n', ' ')
        detail = f"\n      URL: {post_url}\n      Text: {clean_snip}..." if post_url else ""
        actual_rooms_match = re.search(r'([1-9](?:\.5)?)\s*חד', text)
        if actual_rooms_match:
            message = f"Post is for {actual_rooms_match.group(1)} rooms (not matching target {MIN_ROOMS}-{MAX_ROOMS})."
        else:
            message = "No mention of matching room count."
        return storage.VERDICT_PREFILTERED, {"reject_reason": "room_count_mismatch"}, message + detail

    # Recorded as rejected_price (not prefiltered) so --reparse-rejected re-checks it if MIN_PRICE/MAX_PRICE change.
    rejected_price = _price_prefilter_reject(text)
    if rejected_price is not None:
        return (storage.VERDICT_REJECTED_PRICE, {"price_val": rejected_price, "reject_reason": f"price_prefilter:{rejected_price}"},
                f"price {rejected_price:,} out of range.")

    # Recorded as price_unknown (what the LLM would conclude anyway), so --reparse-rejected picks it up
    # if INCLUDE_PRICE_UNKNOWN is turned on.
    if not INCLUDE_PRICE_UNKNOWN and not _has_price_amount(text):
        return (storage.VERDICT_PRICE_UNKNOWN, {"reject_reason": "no_price_amount"},
                "No price-like amount in the post (skipping the LLM).")

    return None

def _names_only_other_target_city(text: str) -> bool:
    # A Ramat Gan-only street name (EXCLUDED_STREETS_RAMAT_GAN_ONLY) in a post that names only
    # Givataim or Tel Aviv is a same-named street elsewhere ("רחוב הנגבה בגבעתיים").
    cities = {name for pattern, name in _CITY_DISPLAY_RES if pattern.search(text)}
    return "רמת גן" not in cities and bool(cities & {"גבעתיים", "תל אביב"})

# A rent-sized amount: 4-5 digits, optionally with a thousands separator ("5,900", "שכ״ד-5900"), or a
# price in words ("6 אלף", "6.2 אלף", "ששת אלפים", "6K"). Measured 2026-11-07 on 378 LLM-parsed posts:
# a post with none of these (after removing phone numbers) never produced a price — skipping it saves
# ~19% of LLM calls and would have changed no added/duplicate/price-rejected verdict.
_PRICE_AMOUNT_RE = re.compile(r"(?<!\d)\d{1,2}[,.]?\d{3}(?!\d)|\d+(?:[.,]\d+)?\s*אלף|אלפים|\d\s*K\b", re.IGNORECASE)

def _has_price_amount(text: str) -> bool:
    return bool(_PRICE_AMOUNT_RE.search(_PHONE_RE.sub(" ", text)))
