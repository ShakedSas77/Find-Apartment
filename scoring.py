"""
=== Fit score ===

A single 0-100 number summarizing how well an added listing matches what we
want, so the sheet's "ציון התאמה" column can be scanned at a glance instead of
reading every cell. Purely informational — doesn't affect filtering, dedupe,
or sheet sort order.

Six independently-weighted factors (must sum to 100 — see config.py's
SCORE_WEIGHT_* constants): price, location, entry date, floor/elevator +
shelter, rooms, parking.
"""
import math
import re
from datetime import date

from config import (
    MIN_PRICE, MAX_PRICE, MAX_WALKING_DISTANCE_KM, MIN_ROOMS, MAX_ROOMS,
    DESTINATION_LAT, DESTINATION_LON,
    SCORE_WEIGHT_PRICE, SCORE_WEIGHT_LOCATION, SCORE_WEIGHT_ENTRY_DATE,
    SCORE_WEIGHT_AMENITIES, SCORE_WEIGHT_ROOMS, SCORE_WEIGHT_PARKING,
    AGENT_FEE_AMORTIZE_MONTHS, SCORE_PRICE_SOFT_CEILING,
    SCORE_ENTRY_DATE_TARGET_MONTH, SCORE_ENTRY_DATE_TARGET_DAY,
    SCORE_ENTRY_DATE_SIGMA_DAYS_BEFORE, SCORE_ENTRY_DATE_SIGMA_DAYS_AFTER,
    SCORE_LOCATION_NORTH_BONUS_PER_KM, SCORE_LOCATION_SOUTH_PENALTY_PER_KM,
    SCORE_LOCATION_EAST_PENALTY_PER_KM, SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT,
    SCORE_LOCATION_GIVATAIM_BONUS,
    SCORE_NO_ELEVATOR_FLOOR_THRESHOLD, SCORE_NO_ELEVATOR_PENALTY_PER_FLOOR,
    SCORE_SHELTER_BONUS, SCORE_ROOMS_MAX_PENALTY_FRACTION,
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _price_score(effective_price: float) -> float:
    """
    Flat-ish (mild decay) up to SCORE_PRICE_SOFT_CEILING, then decays rapidly
    — reaches 0 at soft_ceiling + 2*(MAX_PRICE - soft_ceiling), so it's
    already quite low by MAX_PRICE itself, not just beyond it.
    """
    if not effective_price:
        return 0.0
    if effective_price <= MIN_PRICE:
        return SCORE_WEIGHT_PRICE
    if effective_price <= SCORE_PRICE_SOFT_CEILING:
        span = SCORE_PRICE_SOFT_CEILING - MIN_PRICE
        fraction = (effective_price - MIN_PRICE) / span if span > 0 else 0.0
        return SCORE_WEIGHT_PRICE * (1 - 0.1 * _clamp01(fraction))
    steep_span = 2 * (MAX_PRICE - SCORE_PRICE_SOFT_CEILING)
    if steep_span <= 0:
        return 0.0
    steep_fraction = _clamp01((effective_price - SCORE_PRICE_SOFT_CEILING) / steep_span)
    return SCORE_WEIGHT_PRICE * 0.9 * (1 - steep_fraction) ** 2


def _direction_adjustment(lat: float | None, lon: float | None) -> float:
    """
    North of DESTINATION_LAT/LON is a bonus; south or east is a penalty; west
    is neutral. Degrees converted to km via a flat local approximation (fine
    at city scale) — 1 lat degree ~111km, 1 lon degree ~111km*cos(latitude).
    Clamped to +/- SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT.
    """
    if lat is None or lon is None:
        return 0.0
    lat_km = (lat - DESTINATION_LAT) * 111.0
    lon_km = (lon - DESTINATION_LON) * 111.0 * math.cos(math.radians(DESTINATION_LAT))

    north_bonus = max(0.0, lat_km) * SCORE_LOCATION_NORTH_BONUS_PER_KM
    south_penalty = max(0.0, -lat_km) * SCORE_LOCATION_SOUTH_PENALTY_PER_KM
    east_penalty = max(0.0, lon_km) * SCORE_LOCATION_EAST_PENALTY_PER_KM

    adjustment = north_bonus - south_penalty - east_penalty
    return max(-SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT, min(SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT, adjustment))


def _city_bonus(address: str | None) -> float:
    """Givataim preferred over Ramat Gan — a flat bonus off the address text itself."""
    if address and "גבעתיים" in address:
        return SCORE_LOCATION_GIVATAIM_BONUS
    return 0.0


def _location_score(fields: dict) -> float:
    dist_meters = fields.get("distance_meters")
    if isinstance(dist_meters, (int, float)) and dist_meters != float("inf") and MAX_WALKING_DISTANCE_KM > 0:
        dist_km = dist_meters / 1000.0
        base = SCORE_WEIGHT_LOCATION * _clamp01(1 - dist_km / MAX_WALKING_DISTANCE_KM)
    else:
        base = 0.0

    adjustment = _direction_adjustment(fields.get("lat"), fields.get("lon")) + _city_bonus(fields.get("address"))
    adjustment = max(-SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT, min(SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT, adjustment))

    return max(0.0, min(SCORE_WEIGHT_LOCATION, base + adjustment))


_ENTRY_DATE_DDMM_RE = re.compile(r'^(\d{1,2})/(\d{1,2})$')

_HEBREW_MONTH_NAMES = {
    'ינואר': 1, 'פברואר': 2, 'מרץ': 3, 'אפריל': 4, 'מאי': 5, 'יוני': 6,
    'יולי': 7, 'אוגוסט': 8, 'ספטמבר': 9, 'אוקטובר': 10, 'נובמבר': 11, 'דצמבר': 12,
}


def _entry_date_curve(days_signed: int) -> float:
    """
    Continuous decay, not stepped tiers: 1.0 exactly on target, ~0.61 at
    one sigma away, ~0.14 at 2*sigma, tapering smoothly from there rather
    than jumping between fixed bands. This is what makes e.g. Sep 1 (further
    away) score lower than Sep 20 (closer), even though both are "September".

    Asymmetric: days_signed < 0 (before target) decays on
    SCORE_ENTRY_DATE_SIGMA_DAYS_BEFORE, days_signed > 0 (after target) decays
    on the more forgiving SCORE_ENTRY_DATE_SIGMA_DAYS_AFTER.
    """
    sigma = SCORE_ENTRY_DATE_SIGMA_DAYS_AFTER if days_signed > 0 else SCORE_ENTRY_DATE_SIGMA_DAYS_BEFORE
    return math.exp(-(days_signed ** 2) / (2 * sigma ** 2))


def _score_month_day(month: int, day: int) -> float:
    """
    Fraction (0..1) of SCORE_WEIGHT_ENTRY_DATE for a specific month/day, via a
    fixed placeholder year — fine for a target near October, but would need
    wraparound handling if SCORE_ENTRY_DATE_TARGET_MONTH ever moved close to
    December/January.
    """
    target = date(2001, SCORE_ENTRY_DATE_TARGET_MONTH, SCORE_ENTRY_DATE_TARGET_DAY)
    try:
        candidate = date(2001, month, day)
    except ValueError:
        return 0.0
    return _entry_date_curve((candidate - target).days)


def _entry_date_score(entry_date: str | None, today: date | None = None) -> float:
    """
    "מיידי" (immediate) is scored against today's *real* distance from the
    target date (not the year-agnostic placeholder used for parsed dates) —
    it's a real "right now", so it should decay the same continuous curve
    everything else does. A missing/unparseable value is the only hard 0:
    there's genuinely no date to measure a distance from. `today` is
    injectable so tests don't depend on the wall clock.
    """
    if not entry_date:
        return 0.0

    if entry_date == "מיידי":
        today = today or date.today()
        target_this_year = date(today.year, SCORE_ENTRY_DATE_TARGET_MONTH, SCORE_ENTRY_DATE_TARGET_DAY)
        return SCORE_WEIGHT_ENTRY_DATE * _entry_date_curve((today - target_this_year).days)

    match = _ENTRY_DATE_DDMM_RE.match(entry_date.strip())
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        return SCORE_WEIGHT_ENTRY_DATE * _score_month_day(month, day)

    for name, month in _HEBREW_MONTH_NAMES.items():
        if name in entry_date:
            # No exact day in free text ("ספטמבר") — the 15th is a reasonable
            # mid-month stand-in for the curve.
            return SCORE_WEIGHT_ENTRY_DATE * _score_month_day(month, day=15)

    return 0.0


def _amenities_score(fields: dict) -> float:
    """Floor/elevator condition is the primary driver; shelter is a minor bonus on top."""
    floor = fields.get("floor")
    has_elevator = fields.get("elevator") is True

    floor_elevator_budget = SCORE_WEIGHT_AMENITIES - SCORE_SHELTER_BONUS
    penalty = 0.0
    if not has_elevator and isinstance(floor, int) and floor > SCORE_NO_ELEVATOR_FLOOR_THRESHOLD:
        penalty = (floor - SCORE_NO_ELEVATOR_FLOOR_THRESHOLD) * SCORE_NO_ELEVATOR_PENALTY_PER_FLOOR
    floor_elevator_score = max(0.0, floor_elevator_budget - penalty)

    shelter_score = SCORE_SHELTER_BONUS if fields.get("shelter") is True else 0.0

    return min(SCORE_WEIGHT_AMENITIES, floor_elevator_score + shelter_score)


def _rooms_score(rooms_val) -> float:
    """3.0 rooms is full marks; scales down toward MAX_ROOMS by up to SCORE_ROOMS_MAX_PENALTY_FRACTION."""
    if not isinstance(rooms_val, (int, float)):
        return 0.0
    span = MAX_ROOMS - MIN_ROOMS
    fraction_above_min = _clamp01((rooms_val - MIN_ROOMS) / span) if span > 0 else 0.0
    return SCORE_WEIGHT_ROOMS * (1 - SCORE_ROOMS_MAX_PENALTY_FRACTION * fraction_above_min)


def _parking_score(parking: str | None) -> float:
    """Only private parking is granted points — street parking and no parking both score 0."""
    return SCORE_WEIGHT_PARKING if parking == "פרטית" else 0.0


def compute_fit_score(fields: dict) -> int:
    price_val = fields.get("price_val") or 0
    if price_val and fields.get("is_agent"):
        # One-time broker's fee (~one month's rent) amortized over a year of
        # lease, folded into the scoring price only — the displayed price is
        # untouched. Makes an agent listing compete on effective monthly cost.
        effective_price = price_val + price_val / AGENT_FEE_AMORTIZE_MONTHS
    else:
        effective_price = price_val

    total = (
        _price_score(effective_price)
        + _location_score(fields)
        + _entry_date_score(fields.get("entry_date"))
        + _amenities_score(fields)
        + _rooms_score(fields.get("rooms_val"))
        + _parking_score(fields.get("parking"))
    )
    return round(total)
