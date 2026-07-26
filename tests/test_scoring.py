"""
Unit tests for the fit-score factors in scoring.py. Each factor is tested via
its private helper directly (plain dicts/values in, a number out — no DB/
network/LLM involved), plus a couple of compute_fit_score integration checks.
"""
from config import MAX_PRICE, SCORE_WEIGHT_ENTRY_DATE, SCORE_WEIGHT_PRICE
import scoring


# --- _price_score ---

def test_price_at_or_below_min_price_is_full_score():
    assert scoring._price_score(5500) == SCORE_WEIGHT_PRICE


def test_price_at_soft_ceiling_is_only_mildly_reduced():
    score = scoring._price_score(6500)
    assert 0.85 * SCORE_WEIGHT_PRICE <= score < SCORE_WEIGHT_PRICE


def test_price_decays_much_faster_past_soft_ceiling_than_before_it():
    score_below = scoring._price_score(6000)  # 500 above MIN_PRICE, still under soft ceiling
    score_above = scoring._price_score(6700)  # 200 above soft ceiling, at MAX_PRICE
    drop_below = SCORE_WEIGHT_PRICE - score_below
    drop_above = score_below - score_above
    assert drop_above > drop_below


def test_zero_price_scores_zero():
    assert scoring._price_score(0) == 0.0


# --- _entry_date_score ---

def test_entry_date_exact_target_is_full_score():
    assert scoring._entry_date_score("04/10") == SCORE_WEIGHT_ENTRY_DATE


def test_entry_date_rest_of_target_month_scores_below_full_but_positive():
    score = scoring._entry_date_score("20/10")
    assert 0 < score < SCORE_WEIGHT_ENTRY_DATE


def test_entry_date_closer_to_target_scores_higher():
    near_score = scoring._entry_date_score("20/10")
    far_score = scoring._entry_date_score("15/09")
    assert far_score < near_score


def test_entry_date_after_target_decays_slower_than_before():
    # Oct 20 (16 days after target) vs Sep 23 (11 days before target) — the
    # "after" side is more forgiving, so the further-away date still wins.
    after_score = scoring._entry_date_score("20/10")
    before_score = scoring._entry_date_score("23/09")
    assert after_score > before_score


def test_entry_date_far_outside_window_scores_near_zero():
    assert scoring._entry_date_score("15/05") < 0.01 * SCORE_WEIGHT_ENTRY_DATE


def test_entry_date_immediate_uses_todays_distance_from_target():
    from datetime import date
    on_target = scoring._entry_date_score("מיידי", today=date(2026, 10, 4))
    far_from_target = scoring._entry_date_score("מיידי", today=date(2026, 1, 1))
    assert on_target == SCORE_WEIGHT_ENTRY_DATE
    assert far_from_target < 0.01 * SCORE_WEIGHT_ENTRY_DATE


def test_entry_date_missing_scores_zero():
    assert scoring._entry_date_score("") == 0.0
    assert scoring._entry_date_score(None) == 0.0


def test_entry_date_month_name_only_scores_positive_but_below_full():
    score = scoring._entry_date_score("ספטמבר")
    assert 0 < score < SCORE_WEIGHT_ENTRY_DATE


# --- _direction_adjustment ---

def test_direction_north_of_destination_is_a_bonus():
    from config import DESTINATION_LAT, DESTINATION_LON
    adjustment = scoring._direction_adjustment(DESTINATION_LAT + 0.01, DESTINATION_LON)
    assert adjustment > 0


def test_direction_south_of_destination_is_a_penalty():
    from config import DESTINATION_LAT, DESTINATION_LON
    adjustment = scoring._direction_adjustment(DESTINATION_LAT - 0.01, DESTINATION_LON)
    assert adjustment < 0


def test_direction_east_of_destination_is_a_penalty():
    from config import DESTINATION_LAT, DESTINATION_LON
    adjustment = scoring._direction_adjustment(DESTINATION_LAT, DESTINATION_LON + 0.01)
    assert adjustment < 0


def test_direction_missing_coordinates_is_neutral():
    assert scoring._direction_adjustment(None, None) == 0.0


# --- _city_bonus ---

def test_city_bonus_for_givataim():
    assert scoring._city_bonus("הירדן 5, גבעתיים") > 0


def test_no_city_bonus_for_ramat_gan():
    assert scoring._city_bonus("הירדן 5, רמת גן") == 0.0


# --- _amenities_score ---

def test_high_floor_with_elevator_has_no_penalty():
    full = scoring._amenities_score({"floor": 8, "elevator": True, "shelter": False})
    low_floor_no_elevator = scoring._amenities_score({"floor": 1, "elevator": False, "shelter": False})
    assert full == low_floor_no_elevator


def test_high_floor_without_elevator_is_penalized():
    ok = scoring._amenities_score({"floor": 2, "elevator": False, "shelter": False})
    penalized = scoring._amenities_score({"floor": 6, "elevator": False, "shelter": False})
    assert penalized < ok


def test_shelter_adds_a_bonus():
    without = scoring._amenities_score({"floor": 1, "elevator": True, "shelter": False})
    with_shelter = scoring._amenities_score({"floor": 1, "elevator": True, "shelter": True})
    assert with_shelter > without


# --- _rooms_score ---

def test_three_rooms_is_full_score():
    from config import SCORE_WEIGHT_ROOMS
    assert scoring._rooms_score(3.0) == SCORE_WEIGHT_ROOMS


def test_three_and_a_half_rooms_scores_a_bit_lower():
    from config import SCORE_WEIGHT_ROOMS
    score = scoring._rooms_score(3.5)
    assert 0 < score < SCORE_WEIGHT_ROOMS


# --- _parking_score ---

def test_private_parking_is_granted():
    from config import SCORE_WEIGHT_PARKING
    assert scoring._parking_score("פרטית") == SCORE_WEIGHT_PARKING


def test_street_parking_gets_no_bonus():
    assert scoring._parking_score("ברחוב") == 0.0


def test_no_parking_gets_no_bonus():
    assert scoring._parking_score("") == 0.0


# --- compute_fit_score integration ---

def test_agent_fee_amortization_lowers_score_vs_same_price_private_listing():
    base_fields = {
        "price_val": MAX_PRICE, "rooms_val": 3.0, "floor": 1, "elevator": True,
        "shelter": False, "parking": "", "entry_date": "", "distance_meters": None, "address": "",
    }
    private_score = scoring.compute_fit_score({**base_fields, "is_agent": False})
    agent_score = scoring.compute_fit_score({**base_fields, "is_agent": True})
    assert agent_score < private_score
