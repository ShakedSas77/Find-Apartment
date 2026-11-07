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
    SCORE_ENTRY_DATE_SIGMA_DAYS_AFTER,
    SCORE_LOCATION_NORTH_BONUS_PER_KM, SCORE_LOCATION_SOUTH_PENALTY_PER_KM,
    SCORE_LOCATION_EAST_PENALTY_PER_KM, SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT,
    SCORE_LOCATION_GIVATAIM_BONUS, FOCUS_NEIGHBORHOODS, SCORE_FOCUS_NEIGHBORHOOD_BONUS,
    SCORE_NO_ELEVATOR_FLOOR_THRESHOLD, SCORE_NO_ELEVATOR_PENALTY_PER_FLOOR,
    SCORE_SHELTER_BONUS, SCORE_ROOMS_MAX_PENALTY_FRACTION,
    DEFAULT_ARNONA_MONTHLY_BY_CITY, TOTAL_COST_DEFAULT_CITY, DEFAULT_VAAD_MONTHLY,
    TOTAL_COST_AGENT_FEE_AMORTIZE_MONTHS,
)
from core.normalize import resolve_entry_date


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


def _entry_date_curve(days_signed: int) -> float:
    """
    On or before target (days_signed <= 0): full marks — moving in ASAP is
    always best, never penalized for being early. After target: continuous
    Gaussian decay, not stepped tiers — ~0.61 at one sigma away, ~0.14 at
    2*sigma — so e.g. Nov 20 (further) scores lower than Nov 1 (closer), even
    though both are "November". Sigma is tuned so December-or-later is
    already very low.
    """
    if days_signed <= 0:
        return 1.0
    return math.exp(-(days_signed ** 2) / (2 * SCORE_ENTRY_DATE_SIGMA_DAYS_AFTER ** 2))


def _entry_date_score(entry_date: str | None, today: date | None = None) -> float:
    """
    Resolves entry_date to a concrete date via core.normalize.resolve_entry_date
    ("מיידי" or "DD/MM" — see there for how free text becomes one of those)
    and scores it by real distance from today, so "ASAP" always means the
    same thing regardless of the wall-clock date. A missing/unparseable
    entry_date is the only case that scores a hard 0, since there's genuinely
    no date to measure. `today` is injectable so tests don't depend on the
    wall clock.
    """
    if not entry_date:
        return 0.0
    today = today or date.today()
    resolved = resolve_entry_date(entry_date, today=today)
    if resolved is None:
        return 0.0
    return SCORE_WEIGHT_ENTRY_DATE * _entry_date_curve((resolved - today).days)


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
    """MIN_ROOMS is full marks; scales down toward MAX_ROOMS by up to SCORE_ROOMS_MAX_PENALTY_FRACTION."""
    if not isinstance(rooms_val, (int, float)):
        return 0.0
    span = MAX_ROOMS - MIN_ROOMS
    fraction_above_min = _clamp01((rooms_val - MIN_ROOMS) / span) if span > 0 else 0.0
    return SCORE_WEIGHT_ROOMS * (1 - SCORE_ROOMS_MAX_PENALTY_FRACTION * fraction_above_min)


def _parking_score(parking: str | None) -> float:
    """Only private parking is granted points — street parking and no parking both score 0."""
    return SCORE_WEIGHT_PARKING if parking == "פרטית" else 0.0


def mentions_focus_neighborhood(*texts: str | None) -> bool:
    return any(name in text for text in texts if text for name in FOCUS_NEIGHBORHOODS)


def _focus_bonus(fields: dict) -> float:
    # in_focus_area is set from the full post text at evaluation time; a sheet row
    # rebuilt by --recompute-scores only has the address cell to go on.
    if fields.get("in_focus_area") or mentions_focus_neighborhood(fields.get("address")):
        return SCORE_FOCUS_NEIGHBORHOOD_BONUS
    return 0.0


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
        + _focus_bonus(fields)
    )
    return min(100, round(total))


def _monthly_fee(bimonthly_raw, default_monthly: float) -> float:
    """Sheet arnona/vaad are bi-monthly; blank/unparseable falls back to the monthly default."""
    digits = re.sub(r"[^\d]", "", str(bimonthly_raw if bimonthly_raw is not None else ""))
    return int(digits) / 2 if digits else default_monthly


def compute_total_monthly_cost(fields: dict) -> int | str:
    """Rent + monthly arnona + monthly vaad + broker fee amortized. "" when the rent is unknown."""
    price_val = fields.get("price_val") or 0
    if not price_val:
        return ""
    address = fields.get("address") or ""
    city = next((c for c in DEFAULT_ARNONA_MONTHLY_BY_CITY if c in address), TOTAL_COST_DEFAULT_CITY)
    arnona = _monthly_fee(fields.get("arnona"), DEFAULT_ARNONA_MONTHLY_BY_CITY[city])
    vaad = _monthly_fee(fields.get("vaad"), DEFAULT_VAAD_MONTHLY)
    agent_fee = price_val / TOTAL_COST_AGENT_FEE_AMORTIZE_MONTHS if fields.get("is_agent") else 0
    return round(price_val + arnona + vaad + agent_fee)
