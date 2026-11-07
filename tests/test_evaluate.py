"""
Regression tests for core.evaluate._evaluate_post_data's price/rooms
"second chance" logic — see CLAUDE.md's LLM Details section and
test_posts/post_6_old_price_distractor.txt for the incident these guard
against. None of these reach the dedup-key check (a real bot_data.db
read): either they return early on a rejected price, or no address is
given so _listing_dedupe_key returns None.
"""
import threading
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


def test_price_prefilter_rejects_clearly_out_of_range_rent():
    assert evaluate._price_prefilter_reject("להשכרה 3 חדרים, 9,000 ₪ לחודש") == 9000


def test_price_prefilter_keeps_in_range_rent():
    assert evaluate._price_prefilter_reject("להשכרה 3 חדרים, 6,300 ₪ לחודש") is None


def test_price_prefilter_keeps_post_with_in_range_price_beside_old_price():
    assert evaluate._price_prefilter_reject("להשכרה 3 חדרים. היה 6,500 ₪ עכשיו 7,200 ₪") is None


def test_price_prefilter_ignores_arnona_sized_amount():
    assert evaluate._price_prefilter_reject("להשכרה 3 חדרים, ארנונה 1,200 ₪ לחודשיים") is None


def test_price_prefilter_ignores_post_without_price():
    assert evaluate._price_prefilter_reject("להשכרה 3 חדרים ברמת גן") is None


def test_price_prefilter_keeps_stepped_price_with_in_range_figure_unmarked():
    assert evaluate._price_prefilter_reject("שכר דירה 5200 עד 1/3/27 ו5500 לאחר מכן") is None


def test_content_prefilter_rejects_excluded_location():
    verdict, analysis, _ = evaluate._content_prefilter("להשכרה 3 חדרים בחולון, 6,000 ₪")

    assert (verdict, analysis["reject_reason"]) == (storage.VERDICT_PREFILTERED, "excluded_location:חולון")


def test_content_prefilter_rejects_roommate_post():
    verdict, _, _ = evaluate._content_prefilter("מחפשת שותפה לדירת 3 חדרים")

    assert verdict == storage.VERDICT_PREFILTERED


def test_content_prefilter_allows_roommate_mention_alongside_couple():
    assert evaluate._content_prefilter("דירת 3 חדרים ברמת גן, מתאימה לזוג או ל-2 שותפים, 6,000 ₪") is None


def test_content_prefilter_rejects_sale_price():
    _, analysis, _ = evaluate._content_prefilter("למכירה דירת 3 חדרים, מחיר: 3,395,000 ₪")

    assert analysis["reject_reason"] == "for_sale:3,395,000"


def test_content_prefilter_rejects_non_matching_room_count():
    _, analysis, _ = evaluate._content_prefilter("להשכרה 5 חדרים ברמת גן, 6,000 ₪")

    assert analysis["reject_reason"] == "room_count_mismatch"


def test_content_prefilter_marks_out_of_range_price_as_rejected_price():
    verdict, _, _ = evaluate._content_prefilter("להשכרה 3 חדרים ברמת גן, 9,000 ₪ לחודש")

    assert verdict == storage.VERDICT_REJECTED_PRICE


def test_content_prefilter_includes_post_url_in_room_count_message():
    _, _, message = evaluate._content_prefilter("להשכרה 5 חדרים ברמת גן", "https://example.test/p/1")

    assert "https://example.test/p/1" in message


def test_content_prefilter_passes_clean_post():
    assert evaluate._content_prefilter("להשכרה 3 חדרים ברמת גן, 6,000 ₪ לחודש") is None


def _queue_listing(monkeypatch, post_url, pending_rows, recorded):
    """Runs process_candidate_listing for one fake post of the same apartment; only the LLM, the
    evaluation result, the row builder and the DB write (network/DB boundaries) are faked."""
    fields = {"rooms_val": 3.5, "price_val": 6000.0, "address": "קרל נטר 9, רמת גן"}
    monkeypatch.setattr(evaluate, "analyze_post_with_llm", lambda text: {"rooms": 3.5})
    monkeypatch.setattr(evaluate, "_evaluate_post_data", lambda data, text, group_url="": (storage.VERDICT_ADDED, dict(fields)))
    monkeypatch.setattr(evaluate, "_build_row", lambda url, date, f: [url, "", "", "", "", "", "", "", "", "", "", "", "", "", "", 70])
    monkeypatch.setattr(evaluate.storage, "record_post", lambda url, group, text, verdict, *args, **kwargs: recorded.append((url, verdict)))
    evaluate.process_candidate_listing(post_url, "text", "1h", "group", "label", True, set(), pending_rows, set(), [],
                                       {"llm_parsed": 0, "added": 0}, threading.Lock())


def test_repost_of_a_listing_queued_earlier_in_the_run_is_not_queued_again(monkeypatch):
    evaluate._run_queued_listing_keys.clear()
    pending_rows, recorded = [], []

    _queue_listing(monkeypatch, "https://www.facebook.com/groups/1/posts/1/", pending_rows, recorded)
    _queue_listing(monkeypatch, "https://www.facebook.com/groups/1/posts/2/", pending_rows, recorded)

    assert len(pending_rows) == 1


def test_in_run_repost_is_recorded_as_a_duplicate_listing(monkeypatch):
    evaluate._run_queued_listing_keys.clear()
    pending_rows, recorded = [], []

    _queue_listing(monkeypatch, "https://www.facebook.com/groups/1/posts/1/", pending_rows, recorded)
    _queue_listing(monkeypatch, "https://www.facebook.com/groups/1/posts/2/", pending_rows, recorded)

    assert recorded == [("https://www.facebook.com/groups/1/posts/2/", storage.VERDICT_DUPLICATE_LISTING)]


def test_content_prefilter_rejects_the_hatikva_neighborhood():
    _, analysis, _ = evaluate._content_prefilter("להשכרה 3 חדרים בשכונת התקווה, תל אביב, 6,000 ₪")

    assert analysis["reject_reason"] == "excluded_location:התקווה"


def test_post_with_no_price_amount_skips_the_llm_as_price_unknown():
    verdict, _, _ = evaluate._content_prefilter("להשכרה דירת 3 חדרים ברמת גן, מחיר בפרטי 050-5265642")

    assert verdict == storage.VERDICT_PRICE_UNKNOWN


def test_price_after_a_hyphen_counts_as_an_amount():
    assert evaluate._has_price_amount("שכ״ד-5900 ש״ח חודש בחודשו")


def test_price_in_words_counts_as_an_amount():
    assert evaluate._has_price_amount("דירת 3 חדרים, 6.2 אלף לחודש")


def test_a_phone_number_alone_is_not_an_amount():
    assert not evaluate._has_price_amount("פרטים אצל דני 054-1234567")


def test_landlord_looking_for_a_tenant_is_not_rejected_as_a_seeker():
    assert evaluate._content_prefilter("מחפשים דייר לדירת 3 חדרים ברמת גן, 6,000 ש\"ח") is None


def test_leaving_tenant_looking_for_a_replacement_is_not_rejected_as_a_seeker():
    assert evaluate._content_prefilter("עוזבת את הדירה ומחפשת מחליפ/ה, 3 חדרים בגבעתיים, 5,900 ש\"ח") is None


def test_someone_looking_for_an_apartment_is_still_rejected():
    _, analysis, _ = evaluate._content_prefilter("זוג מחפשים דירת 3 חדרים ברמת גן עד 6,000 ש\"ח")

    assert analysis["reject_reason"].startswith("negative_keyword:מחפש")


def test_english_room_count_passes_the_room_filter():
    assert evaluate._content_prefilter("3 room apartment for rent in Ramat Gan, 6,200 NIS") is None


def test_english_bedrooms_do_not_count_as_rooms():
    _, analysis, _ = evaluate._content_prefilter("3 bedrooms apartment for rent in Ramat Gan, 6,200 NIS")

    assert analysis["reject_reason"] == "room_count_mismatch"


def test_studio_as_a_building_amenity_does_not_reject_the_listing():
    assert evaluate._content_prefilter("להשכרה 3 חדרים ברמת גן, 6,200 ש\"ח. בבניין: בריכה, חדר כושר, סטודיו פילאטיס") is None


def test_a_studio_apartment_is_still_rejected():
    _, analysis, _ = evaluate._content_prefilter("להשכרה דירת סטודיו מקסימה ברמת גן, 3 חדרים בבניין, 4,500 ש\"ח")

    assert analysis["reject_reason"] == "negative_keyword:סטודיו"


def test_a_same_named_street_in_givatayim_is_not_excluded():
    assert evaluate._content_prefilter("מתפנה דירת 3 חדרים ברחוב הנגבה בגבעתיים, 6,000 ש\"ח") is None


def test_the_excluded_ramat_gan_street_is_still_excluded():
    _, analysis, _ = evaluate._content_prefilter("דירת 3 חדרים ברחוב עוזיאל ברמת גן, 6,000 ש\"ח")

    assert analysis["reject_reason"] == "excluded_location:עוזיאל"


def test_an_excluded_area_is_excluded_whatever_city_is_named():
    _, analysis, _ = evaluate._content_prefilter("דירת 3 חדרים בבני ברק על גבול גבעתיים, 6,000 ש\"ח")

    assert analysis["reject_reason"] == "excluded_location:בני ברק"
