"""
=== Free straight-line distance fallback ===

Used whenever Google is off (GMAPS_ENABLED = False, the default) or GMAPS_MONTHLY_CAP
is hit — at that point Google's own geocoding is also unavailable (get_walking_distance() and _validate_address_with_geocoding()
share the same monthly counter), so this needs a genuinely free coordinate
source: Nominatim (OpenStreetMap), no API key. The result is a calibrated
haversine estimate, not a real walking route — a rough correction for
"streets aren't straight," not a precise one. See config.py for the
calibration factor and destination coordinates.
"""

import math
import re
import threading
import time

import requests

from config import (
    DESTINATION_LAT, DESTINATION_LON, DISTANCE_ESTIMATE_LOCATION_ERROR_KM, DISTANCE_ESTIMATE_ROUTE_ERROR,
    NOMINATIM_USER_AGENT, STRAIGHT_LINE_CALIBRATION_FACTOR,
)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_MIN_INTERVAL_SECONDS = 1.0  # Nominatim usage policy: max ~1 request/second

_TARGET_CITIES = ("תל אביב", "רמת גן", "גבעתיים")

# Nominatim's place_rank says what a query matched: 28+ a house or building, 26-27 a street,
# 18-25 a neighbourhood or suburb; below that a whole city, too vague to measure from.
_HOUSE_RANK, _STREET_RANK, _AREA_RANK = 28, 26, 18
# distance_source per match precision; DISTANCE_ESTIMATE_LOCATION_ERROR_KM is keyed by these.
ESTIMATE_SOURCES = ("straight_line_estimate", "street_estimate", "area_estimate")

_rate_lock = threading.Lock()
_last_call = 0.0


def _throttle():
    global _last_call
    with _rate_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def nominatim_geocode(address: str) -> tuple[float, float, int] | None:
    """Free geocode via OpenStreetMap Nominatim. Returns (lat, lon, place_rank) or None
    if the address wasn't found or the request failed."""
    if not address or len(address.strip()) < 3:
        return None
    query = address if "ישראל" in address else f"{address}, ישראל"
    _throttle()
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=8,
        )
        results = resp.json()
    except Exception:
        return None
    if not results:
        return None
    try:
        return float(results[0]["lat"]), float(results[0]["lon"]), int(results[0].get("place_rank", 0))
    except (KeyError, ValueError, TypeError):
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _candidate_queries(address: str) -> list[str]:
    """Without Google's city-anchored canonical address, a bare street ("רחוב שינקין")
    geocodes to nothing, so try it under each target city unless one is already named."""
    street = address.strip()
    for prefix in ("רחוב ", "שכונת "):
        if street.startswith(prefix):
            street = street[len(prefix):]
    if any(city in street for city in _TARGET_CITIES):
        return [street]
    return [f"{street}, {city}" for city in _TARGET_CITIES]


def estimate_straight_line_distance(address: str) -> tuple[str, int, float, float, str] | None:
    """
    (distance_text, distance_meters, lat, lon, source) via free geocoding + calibrated haversine, or
    None if the address couldn't be geocoded below city level. source (ESTIMATE_SOURCES) says how
    precisely the address was found — a house, a street, or only a neighbourhood — which sets the
    estimate's error (estimate_error_km).
    """
    best = None
    for query in _candidate_queries(address):
        match = next(filter(None, (nominatim_geocode(v) for v in spelling_variants(query))), None)
        if not match or match[2] < _AREA_RANK:
            continue
        lat, lon, rank = match
        km = haversine_km(lat, lon, DESTINATION_LAT, DESTINATION_LON) * STRAIGHT_LINE_CALIBRATION_FACTOR
        # Nearest wins when the same street name exists in several cities: errs toward
        # keeping the listing, which the sheet reader can then sanity-check.
        if best is None or km < best[0]:
            best = (km, lat, lon, rank)
    if best is None:
        return None
    km, lat, lon, rank = best
    source = ESTIMATE_SOURCES[0] if rank >= _HOUSE_RANK else ESTIMATE_SOURCES[1] if rank >= _STREET_RANK else ESTIMATE_SOURCES[2]
    return f"{km:.1f}", int(km * 1000), lat, lon, source


def estimate_error_km(source: str, km: float | None) -> float | None:
    """
    How far off an estimated distance can be (9 in 10 real routes fell within it): the route-shape error,
    proportional to the distance, plus where in the street or neighbourhood the flat actually is.
    None for a distance that isn't an estimate (Google's own walking route, or none at all).
    """
    if source not in ESTIMATE_SOURCES or km is None:
        return None
    return round(km * DISTANCE_ESTIMATE_ROUTE_ERROR + DISTANCE_ESTIMATE_LOCATION_ERROR_KM[source], 1)


def distance_accuracy_label(source: str, error_km: float | None) -> str:
    """The sheet's דיוק מרחק cell: "Google" for a real walking route, "הערכה ±0.4" for an estimate."""
    if source == "google_maps":
        return "Google"
    return f"הערכה ±{error_km:g}" if error_km is not None else ""


def error_km_from_label(label: str) -> float | None:
    """distance_accuracy_label() read back, for alerts rebuilt from a sheet row."""
    match = _ERROR_LABEL_RE.search(label or "")
    return float(match.group(1)) if match else None


_ERROR_LABEL_RE = re.compile(r"±(\d+(?:\.\d+)?)")
_STREET_PREFIX_RE = re.compile(r"^(?:רחוב|רח['׳]?|שד['׳]|שדרות)\s+")
# A 3-4 letter Hebrew word with no quote mark yet: an acronym written without its gershayim (רמבם, אצל).
_ACRONYM_CANDIDATE_RE = re.compile(r"(?<![֐-׿\"'])([֐-׿]{2,3})([֐-׿])(?![֐-׿\"'])")
_HOUSE_NUMBER_RE = re.compile(r"\s*\b\d{1,3}[א-ת]?\b")


def spelling_variants(query: str) -> list[str]:
    """
    The query, then spellings OSM is likelier to have, tried only if it misses (seen 2026-11-07:
    "רמבם 3, גבעתיים" misses, OSM has רמב"ם). In order: quote marks normalized + street prefix
    dropped; gershayim inserted into the first 3-4 letter word; house number dropped — street-level
    precision only, a last resort.
    """
    street, sep, rest = query.partition(",")
    cleaned = _STREET_PREFIX_RE.sub("", street.replace("״", '"').replace("׳", "'").strip())
    variants = [query, cleaned + sep + rest]
    if '"' not in cleaned:
        variants.append(_ACRONYM_CANDIDATE_RE.sub(r'\1"\2', cleaned, count=1) + sep + rest)
    without_number = _HOUSE_NUMBER_RE.sub("", cleaned).strip()
    if without_number and without_number != cleaned:
        variants.append(without_number + sep + rest)
    return list(dict.fromkeys(v.strip() for v in variants))
