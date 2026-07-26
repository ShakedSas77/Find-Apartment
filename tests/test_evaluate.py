"""
Regression tests for core.evaluate._evaluate_post_data's price/rooms
"second chance" logic — see CLAUDE.md's LLM Details section and
test_posts/post_6_old_price_distractor.txt for the incident these guard
against. None of these reach the dedup-key check (a real bot_data.db
read): either they return early on a rejected price, or no address is
given so _listing_dedupe_key returns None.
"""
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
