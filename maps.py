"""
=== Distance / geocoding ===

get_walking_distance() is the public entry point: walking distance to
DESTINATION_ADDRESS via Google Distance Matrix, with address-confidence
gating, monthly-quota tracking, a free straight-line fallback over the quota,
and a per-address cache. _format_address_display() reshapes an LLM-extracted
address into the sheet's display convention, resolving an ambiguous
no-city address against the scanning group's candidate cities when needed
(which can itself spend geocoding calls — not pure, so it lives here rather
than in core/normalize.py).
"""
import re
import threading

import googlemaps

import env
import storage
import distance_fallback
from config import (
    DESTINATION_ADDRESS, GMAPS_MONTHLY_CAP, GMAPS_ON_CAP, GMAPS_TARGET_CITIES,
    GMAPS_VALIDATE_ADDRESSES, GMAPS_DISTANCE_ONLY_CONFIDENT_ADDRESS, EXCLUDE_SOUTH_OF_LAT,
)
from core.util import _safe_print, _with_retries
from core.errors import GmapsQuotaHalted
from core.normalize import (
    _classify_address_confidence, _CITY_ONLY_ADDRESSES, _extract_city_and_core,
    _format_core_with_city, _NEIGHBORHOOD_DISPLAY_RE, _CORNER_DISPLAY_RE,
    _CORNER_SLASH_RE, _STREET_HINT_RE, _TRAILING_NUMBER_RE, _HEBREW_RE,
)

_gmaps_client = None

_gmaps_client_lock = threading.Lock()

def get_gmaps_client():
    global _gmaps_client
    if _gmaps_client is None:
        with _gmaps_client_lock:
            if _gmaps_client is None:
                _gmaps_client = googlemaps.Client(key=env.get_gmaps_api_key())
    return _gmaps_client

# googlemaps sends GMAPS_API_KEY as a URL query param (unlike Gemini, which uses
# a header) — on a network-level failure (not a clean API error response), the
# underlying requests/urllib3 exception's str() embeds the full request URL,
# key included. Strip it before any exception text reaches a print/log.
_API_KEY_QUERY_RE = re.compile(r'([?&]key=)[^&\s]+')

def _redact_api_key(text: str) -> str:
    return _API_KEY_QUERY_RE.sub(r'\1REDACTED', text)

def _gmaps_result_matches_city(result: dict, city: str) -> bool:
    formatted = result.get("formatted_address", "")
    result_city = _gmaps_result_city(result)
    return city in formatted or city == result_city or city in result_city

def _resolve_ambiguous_city(street_core: str, candidate_cities: list[str]) -> str | None:
    """
    Resolves which of a group's candidate cities a street actually belongs to.
    1 candidate -> trusted directly, no Maps call. >=2 -> geocode each to see
    which ones the street exists in; if more than one does, the walking-
    distance-closer one to DESTINATION_ADDRESS wins. Never guesses: returns
    None (caller falls back to the address unchanged) when nothing resolves
    confidently or Maps is unavailable/over quota.
    """
    if not candidate_cities:
        return None
    if len(candidate_cities) == 1:
        return candidate_cities[0]

    if not GMAPS_VALIDATE_ADDRESSES:
        return None

    hits = []  # (city, formatted_address)
    for city in candidate_cities:
        over_cap, _ = _handle_gmaps_cap_if_needed()
        if over_cap:
            break
        query = f"{street_core}, {city}, ישראל"
        try:
            storage.increment_gmaps_usage()
            results = _with_retries(lambda: get_gmaps_client().geocode(query, language="he", region="il"))
        except Exception as e:
            _safe_print(f"\n    [Google Geocoding API Error]: {_redact_api_key(str(e))}")
            continue
        if not results:
            continue
        best = results[0]
        if _gmaps_has_street_precision(best) and _gmaps_result_matches_city(best, city):
            hits.append((city, best.get("formatted_address", "")))

    if not hits:
        return None
    if len(hits) == 1:
        return hits[0][0]

    best_city, best_meters = None, float("inf")
    for city, formatted in hits:
        over_cap, _ = _handle_gmaps_cap_if_needed()
        if over_cap:
            break
        try:
            storage.increment_gmaps_usage()
            result = _with_retries(lambda: get_gmaps_client().distance_matrix(
                origins=formatted, destinations=DESTINATION_ADDRESS, mode="walking", language="he", region="il",
            ))
            element = result["rows"][0]["elements"][0]
            if element.get("status") == "OK" and element["distance"]["value"] < best_meters:
                best_meters = element["distance"]["value"]
                best_city = city
        except Exception as e:
            _safe_print(f"\n    [Google Distance Matrix API Error]: {_redact_api_key(str(e))}")
            continue

    return best_city or hits[0][0]

def _format_address_display(address: str, group_url: str | None = None) -> str:
    """
    "רחוב המעלות 12 בגבעתיים" -> "המעלות 12, גבעתיים"
    "רחוב הירדן רמת גן" -> "הירדן, רמת גן" (no number)
    "שכונת מרום נווה ברמת גן" -> "מרום נווה, רמת גן" (neighborhood, not a street)
    "רחוב אלימלך על פינת הרצל ברמת גן" -> "אלימלך פינת הרצל, רמת גן" (corner)
    "כצנלסון/ויצמן בגבעתיים" -> "כצנלסון פינת ויצמן, גבעתיים" (slash shorthand)
    When no city is stated at all, falls back to resolving one from the
    scanning group's own candidate cities (see _resolve_ambiguous_city),
    keyed off the primary street (group1 of a corner/slash pair, since that's
    what geocoding needs) — neighborhood phrasing alone stays out of scope
    (too ambiguous to geocode as a single street query). Falls back to the
    address unchanged whenever nothing resolves confidently — never guesses
    a city.
    """
    if not address:
        return address

    found = _extract_city_and_core(address)
    if found:
        core, city = found
        return _format_core_with_city(core, city) or address

    if group_url and not _NEIGHBORHOOD_DISPLAY_RE.match(address.strip()):
        candidates = storage.get_group_city_hint(group_url)
        if candidates:
            corner_m = _CORNER_DISPLAY_RE.match(address) or _CORNER_SLASH_RE.match(address)
            if corner_m:
                street_only = _STREET_HINT_RE.sub('', corner_m.group(1)).strip()
                core_for_format = address
            else:
                street_core = _STREET_HINT_RE.sub('', address, count=1).strip()
                m = _TRAILING_NUMBER_RE.match(street_core)
                street_only = m.group(1).strip() if m else street_core
                core_for_format = street_core
            if street_only and _HEBREW_RE.search(street_only):
                city = _resolve_ambiguous_city(street_only, candidates)
                if city:
                    return _format_core_with_city(core_for_format, city) or address

    return address

_gmaps_cap_lock = threading.Lock()

_gmaps_cap_notice_printed = False

def _handle_gmaps_cap_if_needed() -> tuple[bool, str]:
    global _gmaps_cap_notice_printed
    if storage.get_gmaps_usage() < GMAPS_MONTHLY_CAP:
        return False, ""

    with _gmaps_cap_lock:
        already_notified = _gmaps_cap_notice_printed
        _gmaps_cap_notice_printed = True
    if not already_notified:
        _safe_print(f"\n    WARNING: Google Maps monthly cap reached ({GMAPS_MONTHLY_CAP} calls). "
                    f"GMAPS_ON_CAP='{GMAPS_ON_CAP}' in config.py.")
    if GMAPS_ON_CAP == "halt":
        raise GmapsQuotaHalted()
    return True, "מכסה חודשית הסתיימה"

def _gmaps_component_long_names(result: dict, component_type: str) -> list[str]:
    names = []
    for component in result.get("address_components", []):
        if component_type in component.get("types", []):
            names.append(component.get("long_name", ""))
    return names

def _gmaps_result_city(result: dict) -> str:
    for component_type in ("locality", "administrative_area_level_2", "administrative_area_level_1"):
        names = _gmaps_component_long_names(result, component_type)
        if names:
            return names[0]
    return ""

def _gmaps_has_street_precision(result: dict) -> bool:
    types = set(result.get("types", []))
    # "intersection" (a corner query, e.g. "X פינת Y") geocodes to an exact
    # lat/lng just like a street_address does — at least as precise for
    # walking-distance purposes, not the vague "general area" result this
    # check exists to filter out. Confirmed via a live geocode of a real
    # corner address: Google returns type=intersection with a precise point.
    if "street_address" in types or "premise" in types or "intersection" in types:
        return True
    component_types = {
        t
        for component in result.get("address_components", [])
        for t in component.get("types", [])
    }
    return "route" in component_types

def _gmaps_city_allowed(result: dict) -> bool:
    formatted = result.get("formatted_address", "")
    city = _gmaps_result_city(result)
    return any(target in formatted or target in city for target in GMAPS_TARGET_CITIES)

def _validate_address_with_geocoding(address: str, confidence: str) -> tuple[str, str, str, str, str, float | None, float | None]:
    """
    Returns canonical_address, city, updated_confidence, warning, geocode_status, lat, lon.
    lat/lon are only ever populated on the "ok" success path (a precise, city-
    matched result) — every other path either never geocoded or got a
    result too weak to trust for a geographic check, so callers needing them
    (e.g. the south-of-HaHagana exclusion, or the fit-score direction signal)
    should treat None as "can't tell, don't score/reject on this basis."
    """
    if not GMAPS_VALIDATE_ADDRESSES:
        return address, "", confidence, "", "disabled", None, None

    if confidence in {"missing", "low"}:
        return "", "", confidence, "כתובת חלשה - לא נשלחה לגיאוקודינג", "skipped_low_confidence", None, None

    over_cap, placeholder = _handle_gmaps_cap_if_needed()
    if over_cap:
        return address, "", confidence, placeholder, "quota", None, None

    query = address
    if not any(city in query for city in ["רמת גן", "גבעתיים", "תל אביב", "רמת-גן", "ר\"ג"]):
        query = f"{query}, רמת גן, גבעתיים, ישראל"

    try:
        storage.increment_gmaps_usage()
        results = _with_retries(lambda: get_gmaps_client().geocode(query, language="he", region="il"))
    except Exception as e:
        _safe_print(f"\n    [Google Geocoding API Error]: {_redact_api_key(str(e))}")
        return address, "", confidence, "שגיאת אימות כתובת", "geocode_error", None, None

    if not results:
        return "", "", "low", "Google לא מצא את הכתובת", "not_found", None, None

    best = results[0]
    if not _gmaps_city_allowed(best):
        return "", _gmaps_result_city(best), "low", "הכתובת לא אומתה בעיר יעד", "wrong_city", None, None

    if not _gmaps_has_street_precision(best):
        return "", _gmaps_result_city(best), "low", "Google החזיר תוצאה כללית בלבד", "not_street_precision", None, None

    location = best.get("geometry", {}).get("location", {})
    lat = location.get("lat")
    lon = location.get("lng")
    return best.get("formatted_address", address), _gmaps_result_city(best), "high", "", "ok", lat, lon

def get_walking_distance(address: str):
    """
    Returns distance_text, distance_meters, confidence, warning, source, lat, lon.
    lat/lon are the listing's own geocoded coordinates (not the destination's)
    — used by scoring.py for the north/south/east directional adjustment. Only
    populated when a geocode actually ran and succeeded; None otherwise (see
    _validate_address_with_geocoding's docstring for which paths that covers).
    """
    confidence, warning = _classify_address_confidence(address)

    if not address or len(address) < 3:
        return "", 999999, "missing", "כתובת חסרה", "skipped", None, None

    cached = storage.get_address_cache(address)
    if cached:
        # Must return the ORIGINAL source ("google_maps"/"skipped"/"quota"),
        # not a hardcoded "cache" — callers gate the > MAX_WALKING_DISTANCE_KM
        # rejection on distance_source == "google_maps", so a hardcoded value
        # here silently let any cached far-away address (e.g. previously
        # geocoded by an earlier, differently-rejected post reusing the same
        # address text) bypass the distance filter entirely.
        return (
            cached.get("distance_text") or "",
            cached.get("distance_meters") or 999999,
            cached.get("confidence") or confidence,
            cached.get("warning") or "",
            cached.get("distance_source") or "cache",
            cached.get("lat"),
            cached.get("lon"),
        )

    if address.strip() in _CITY_ONLY_ADDRESSES:
        storage.save_address_cache(address, "", "", "low", "כתובת ברמת עיר בלבד", "", 999999, "skipped", "city_only", None, None)
        return "", 999999, "low", "כתובת ברמת עיר בלבד", "skipped", None, None

    canonical_address, city, confidence, geocode_warning, geocode_status, lat, lon = _validate_address_with_geocoding(address, confidence)
    warning = geocode_warning or warning

    if GMAPS_DISTANCE_ONLY_CONFIDENT_ADDRESS and confidence not in {"high", "medium"}:
        storage.save_address_cache(address, canonical_address, city, confidence, warning, "", 999999, "skipped", geocode_status, lat, lon)
        return "", 999999, confidence, warning, "skipped", lat, lon

    if EXCLUDE_SOUTH_OF_LAT is not None and lat is not None and lat < EXCLUDE_SOUTH_OF_LAT:
        south_warning = "מיקום מדרום לתחנת הרכבת ההגנה - מחוץ לטווח"
        storage.save_address_cache(address, canonical_address, city, confidence, south_warning, "", 999999, "excluded_south", geocode_status, lat, lon)
        return "", 999999, confidence, south_warning, "excluded_south", lat, lon

    over_cap, placeholder = _handle_gmaps_cap_if_needed()
    if over_cap:
        fallback = distance_fallback.estimate_straight_line_distance(canonical_address or address)
        if fallback:
            fallback_text, fallback_meters = fallback
            fallback_warning = "הערכה קווית (מכסת Google הסתיימה)"
            storage.save_address_cache(address, canonical_address, city, confidence, fallback_warning, fallback_text, fallback_meters, "straight_line_estimate", geocode_status, lat, lon)
            return fallback_text, fallback_meters, confidence, fallback_warning, "straight_line_estimate", lat, lon
        storage.save_address_cache(address, canonical_address, city, confidence, warning, placeholder, 999999, "quota", geocode_status, lat, lon)
        return placeholder, 999999, confidence, warning, "quota", lat, lon

    # Safety-net addition for Google Maps: anchoring the search area
    origin = canonical_address or address
    if not any(city_name in origin for city_name in ["רמת גן", "גבעתיים", "תל אביב", "רמת-גן", "ר\"ג"]):
        origin = f"{origin}, רמת גן, גבעתיים, ישראל"

    try:
        storage.increment_gmaps_usage()  # counted before the call — origins×destinations is always 1×1 here
        result = _with_retries(lambda: get_gmaps_client().distance_matrix(
            origins=origin,
            destinations=DESTINATION_ADDRESS,
            mode="walking",
            language="he",
            region="il",
        ))
        element = result["rows"][0]["elements"][0]
        if element["status"] == "OK":
            dist_meters = element["distance"]["value"]
            dist_text = f"{dist_meters / 1000:.1f}"
            storage.save_address_cache(address, canonical_address, city, confidence, warning, dist_text, dist_meters, "google_maps", geocode_status, lat, lon)
            return dist_text, dist_meters, confidence, warning, "google_maps", lat, lon

        storage.save_address_cache(address, canonical_address, city, confidence, "Distance Matrix לא החזיר מסלול תקין", "", 999999, "google_maps_failed", geocode_status, lat, lon)
        return "", float('inf'), confidence, "Distance Matrix לא החזיר מסלול תקין", "google_maps_failed", lat, lon
    except Exception as e:
        _safe_print(f"\n    [Google Maps API Error]: {_redact_api_key(str(e))}")
        storage.save_address_cache(address, canonical_address, city, confidence, "שגיאת Distance Matrix", "", 999999, "google_maps_error", geocode_status, lat, lon)
        return "", float('inf'), confidence, "שגיאת Distance Matrix", "google_maps_error", lat, lon
