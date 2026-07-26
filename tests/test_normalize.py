"""
Unit tests for the pure text-normalization/dedupe helpers.
"""
from datetime import datetime

from core.normalize import (
    _detect_agent,
    _infer_post_date,
    _normalize_bimonthly_fee,
    _parse_floor,
)
from apartment_bot import (
    _listing_dedupe_key,
    _phone_price_rooms_fingerprint,
    _text_dedup_hash,
)


# --- _normalize_bimonthly_fee ---

def test_normalize_bimonthly_fee_doubles_monthly_amount():
    assert _normalize_bimonthly_fee("500 לחודש") == 1000


def test_normalize_bimonthly_fee_keeps_bimonthly_amount_unchanged():
    assert _normalize_bimonthly_fee("800 לחודשיים") == 800


def test_normalize_bimonthly_fee_strips_thousands_separator():
    assert _normalize_bimonthly_fee("1,200") == 1200


def test_normalize_bimonthly_fee_included_in_rent_returns_zero():
    assert _normalize_bimonthly_fee("כלול") == 0


def test_normalize_bimonthly_fee_empty_input_returns_empty_string():
    assert _normalize_bimonthly_fee("") == ""


# --- _parse_floor ---

def test_parse_floor_ground_floor_is_zero():
    assert _parse_floor("קרקע") == 0


def test_parse_floor_extracts_floor_out_of_total():
    assert _parse_floor("3 מתוך 5") == 3


def test_parse_floor_hebrew_ordinal_word():
    assert _parse_floor("שלישית") == 3


def test_parse_floor_explicit_digit_wins_over_ground_floor_mention():
    assert _parse_floor("1 מעל הקרקע") == 1


def test_parse_floor_empty_input_returns_empty_string():
    assert _parse_floor("") == ""


# --- _infer_post_date ---

def test_infer_post_date_rolls_back_to_previous_year_when_date_is_in_future():
    now = datetime(2026, 1, 15)
    assert _infer_post_date("01/09", now=now) == datetime(2025, 9, 1)


def test_infer_post_date_keeps_current_year_when_date_is_today():
    now = datetime(2026, 1, 15)
    assert _infer_post_date("15/01", now=now) == datetime(2026, 1, 15)


def test_infer_post_date_invalid_calendar_date_returns_none():
    assert _infer_post_date("31/02", now=datetime(2026, 1, 15)) is None


def test_infer_post_date_unparseable_string_returns_none():
    assert _infer_post_date("garbage", now=datetime(2026, 1, 15)) is None


# --- _phone_price_rooms_fingerprint / _text_dedup_hash (crosspost dedup) ---

_TEXT_A = "דירה להשכרה 3 חדרים, 6500 ₪ לחודש. לפרטים התקשרו 050-1234567"
_TEXT_B = "להשכרה דירת 3 חד' מדהימה! מחיר 6500 ₪. פנו אלינו: 0501234567"
_TEXT_C = "להשכרה דירת 3 חד' מדהימה! מחיר 7500 ₪. פנו אלינו: 0501234567"


def test_text_dedup_hash_matches_across_differently_worded_crossposts():
    assert _text_dedup_hash(_TEXT_A) == _text_dedup_hash(_TEXT_B)


def test_phone_price_rooms_fingerprint_differs_when_price_differs():
    assert _phone_price_rooms_fingerprint(_TEXT_B) != _phone_price_rooms_fingerprint(_TEXT_C)


def test_phone_price_rooms_fingerprint_empty_without_room_count():
    text = "לפרטים 050-1234567 מחיר 6500 ₪"
    assert _phone_price_rooms_fingerprint(text) == ""


def test_text_dedup_hash_empty_for_short_text_with_no_phone():
    assert _text_dedup_hash("דירה יפה להשכרה") == ""


# --- _listing_dedupe_key ---

def test_listing_dedupe_key_normalizes_street_prefix_and_city_hyphenation():
    key_with_prefix = _listing_dedupe_key("רחוב הרצל 5, רמת גן", 3, 6500)
    key_without_prefix = _listing_dedupe_key("הרצל 5 רמת-גן", 3.0, 6500.0)
    assert key_with_prefix == key_without_prefix


def test_listing_dedupe_key_none_for_unstated_address():
    assert _listing_dedupe_key("לא צוין", 3, 6500) is None


def test_listing_dedupe_key_none_for_address_too_short():
    assert _listing_dedupe_key("אבג", 3, 6500) is None


# --- _detect_agent ---

def test_detect_agent_negation_defers_to_llm_value():
    assert _detect_agent("דירה ללא תיווך", False) is False


def test_detect_agent_explicit_agency_signal_overrides_to_true():
    assert _detect_agent('תיווך גל נדל"ן', None) is True


def test_detect_agent_no_signal_defers_to_llm_value():
    assert _detect_agent("דירה יפה", None) is None
