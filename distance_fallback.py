"""
=== Free straight-line distance fallback ===

Used only when GMAPS_MONTHLY_CAP is hit — at that point Google's own geocoding
is also unavailable (get_walking_distance() and _validate_address_with_geocoding()
share the same monthly counter), so this needs a genuinely free coordinate
source: Nominatim (OpenStreetMap), no API key. The result is a calibrated
haversine estimate, not a real walking route — a rough correction for
"streets aren't straight," not a precise one. See config.py for the
calibration factor and destination coordinates.
"""

import math
import threading
import time

import requests

from config import (
    DESTINATION_LAT, DESTINATION_LON,
    NOMINATIM_USER_AGENT, STRAIGHT_LINE_CALIBRATION_FACTOR,
)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_MIN_INTERVAL_SECONDS = 1.0  # Nominatim usage policy: max ~1 request/second

_rate_lock = threading.Lock()
_last_call = 0.0


def _throttle():
    global _last_call
    with _rate_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def nominatim_geocode(address: str) -> tuple[float, float] | None:
    """Free geocode via OpenStreetMap Nominatim. Returns (lat, lon) or None
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
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, ValueError, TypeError):
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def estimate_straight_line_distance(address: str) -> tuple[str, int] | None:
    """(distance_text, distance_meters) via free geocoding + calibrated
    haversine, or None if the address couldn't be geocoded."""
    coords = nominatim_geocode(address)
    if not coords:
        return None
    lat, lon = coords
    km = haversine_km(lat, lon, DESTINATION_LAT, DESTINATION_LON) * STRAIGHT_LINE_CALIBRATION_FACTOR
    meters = int(km * 1000)
    return f"{km:.1f}", meters
