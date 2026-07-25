"""
=== Fit score ===

A single 0-100 number summarizing how well an added listing matches what we
want, so the sheet's "ציון התאמה" column can be scanned at a glance instead of
reading every cell. Purely informational — doesn't affect filtering, dedupe,
or sheet sort order.
"""

from config import (
    MIN_PRICE, MAX_PRICE, MAX_WALKING_DISTANCE_KM,
    SCORE_WEIGHT_PRICE, SCORE_WEIGHT_DISTANCE, SCORE_WEIGHT_AMENITIES,
    AGENT_FEE_AMORTIZE_MONTHS,
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_fit_score(fields: dict) -> int:
    price_val = fields.get("price_val") or 0
    if price_val and fields.get("is_agent"):
        # One-time broker's fee (~one month's rent) amortized over a year of
        # lease, folded into the scoring price only — the displayed price is
        # untouched. Makes an agent listing compete on effective monthly cost.
        effective_price = price_val + price_val / AGENT_FEE_AMORTIZE_MONTHS
    else:
        effective_price = price_val

    if effective_price and MAX_PRICE > MIN_PRICE:
        price_score = SCORE_WEIGHT_PRICE * _clamp01(
            1 - (effective_price - MIN_PRICE) / (MAX_PRICE - MIN_PRICE)
        )
    else:
        price_score = 0.0

    dist_meters = fields.get("distance_meters")
    if isinstance(dist_meters, (int, float)) and dist_meters not in (float("inf"),) and MAX_WALKING_DISTANCE_KM > 0:
        dist_km = dist_meters / 1000.0
        distance_score = SCORE_WEIGHT_DISTANCE * _clamp01(1 - dist_km / MAX_WALKING_DISTANCE_KM)
    else:
        distance_score = 0.0

    amenity_flags = [
        fields.get("elevator") is True,
        bool(fields.get("parking")) and fields.get("parking") != "לא",
        fields.get("shelter") is True,
    ]
    amenities_score = SCORE_WEIGHT_AMENITIES * (sum(amenity_flags) / len(amenity_flags))

    return round(price_score + distance_score + amenities_score)
