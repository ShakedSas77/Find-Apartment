"""
Regression tests for core.evaluate._evaluate_post_data's price/rooms
"second chance" logic — see CLAUDE.md's LLM Details section and
test_posts/post_6_old_price_distractor.txt for the incident these guard
against. None of these reach the dedup-key check (a real bot_data.db
read): either they return early on a rejected price, or no address is
given so _listing_dedupe_key returns None.
"""
from datetime import date, timedelta

import storage
import core.evaluate as evaluate


def test_llm_price_not_overridden_by_nearby_old_price_distractor():
    # 7200 is a real LLM-extracted value, just outside MIN_PRICE-MAX_PRICE (5500-6700)
    # — the regression this guards against is the nearby "6,500" distractor silently
    # replacing it, which would flip this to VERDICT_ADDED with the wrong price.
    text = 'להשכרה 3 חדרים, רחוב אחד העם, רמת גן.\nהיה 6,500 עכשיו 7,200 ש"ח בעקבות עליית הארנונה.'
    data = {"rooms": 3, "price": 7200, "address": "רחוב אחד העם, רמת גן"}

    verdict, fields = evaluate._evaluate_post_data(data, text)

    assert fields["price_val"] == 7200.0
    assert verdict == storage.VERDICT_REJECTED_PRICE


def test_second_chance_fires_when_llm_price_is_null():
    text = "להשכרה 3 חדרים ברמת גן, 6,300 ₪ לחודש."
    data = {"rooms": 3, "price": None}

    verdict, fields = evaluate._evaluate_post_data(data, text)

    assert fields["price_val"] == 6300.0


def test_absurd_llm_price_is_replaced_ungated():
    text = "להשכרה 3 חדרים ברמת גן, 6,300 ₪ לחודש."
    data = {"rooms": 3, "price": 150}

    verdict, fields = evaluate._evaluate_post_data(data, text)

    assert fields["price_val"] == 6300.0


def test_no_price_anywhere_is_price_unknown_verdict():
    text = "להשכרה 3 חדרים ברמת גן, לפרטים נא לפנות בהודעה פרטית."
    data = {"rooms": 3, "price": None}

    verdict, fields = evaluate._evaluate_post_data(data, text)

    assert verdict == storage.VERDICT_PRICE_UNKNOWN


def test_far_out_entry_date_is_rejected():
    # ~90 days out, past SCORE_ENTRY_DATE_MAX_DAYS_AHEAD (60) — a confidently
    # resolved date this far ahead should never reach the sheet.
    future = date.today() + timedelta(days=90)
    text = "להשכרה 3 חדרים ברמת גן, 6,300 ₪ לחודש."
    data = {"rooms": 3, "price": 6300, "entry_date": future.strftime("%d.%m")}

    verdict, fields = evaluate._evaluate_post_data(data, text)

    assert verdict == storage.VERDICT_REJECTED_ENTRY_DATE


def test_unparseable_entry_date_is_not_rejected():
    # No resolvable date at all — never rejected on this basis, only scores 0.
    text = "להשכרה 3 חדרים ברמת גן, 6,300 ₪ לחודש."
    data = {"rooms": 3, "price": 6300, "entry_date": "מועד לא סופי עדיין"}

    verdict, fields = evaluate._evaluate_post_data(data, text)

    assert verdict != storage.VERDICT_REJECTED_ENTRY_DATE


def test_rooms_pre_filter_accepts_two_and_a_half_rooms():
    import re
    from config import ROOMS_PRE_FILTER_REGEX
    assert re.search(ROOMS_PRE_FILTER_REGEX, "להשכרה דירת 2.5 חדרים ברמת גן")


def test_rooms_pre_filter_rejects_twelve_and_a_half_rooms():
    import re
    from config import ROOMS_PRE_FILTER_REGEX
    assert not re.search(ROOMS_PRE_FILTER_REGEX, "12.5 חדרים")
