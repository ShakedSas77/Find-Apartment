"""
Regression test for the bot-token leak: requests/urllib3 exceptions embed the full
request URL, and Telegram carries the token in the URL path.
"""
from urllib.parse import unquote_plus

import requests

import config
import scoring
import telegram_notifier

_FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abc-DEF"


def test_redact_bot_token_strips_token_from_urllib3_style_message():
    message = ("HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
               f"/bot{_FAKE_TOKEN}/sendMessage (Caused by ConnectTimeoutError)")

    assert _FAKE_TOKEN not in telegram_notifier._redact_bot_token(message)


def test_redact_bot_token_keeps_method_and_host_for_debugging():
    message = f"url: /bot{_FAKE_TOKEN}/sendMessage"

    assert telegram_notifier._redact_bot_token(message) == "url: /botREDACTED/sendMessage"


def test_redact_bot_token_leaves_unrelated_text_unchanged():
    assert telegram_notifier._redact_bot_token("connection refused") == "connection refused"


def test_call_failure_does_not_print_the_token(monkeypatch, capsys):
    monkeypatch.setattr(telegram_notifier, "TELEGRAM_BOT_TOKEN", _FAKE_TOKEN)

    def raise_with_url(url, **_kwargs):
        raise requests.exceptions.ConnectTimeout(f"Max retries exceeded with url: {url.split('.org')[1]}")

    monkeypatch.setattr(telegram_notifier.requests, "post", raise_with_url)

    telegram_notifier._call("sendMessage", {})

    assert _FAKE_TOKEN not in capsys.readouterr().out


_FULL_FIELDS = {
    "rooms_val": 3.0, "price_val": 6300, "distance_text": "1.4", "floor": 0, "elevator": True, "parking": "פרטית",
    "shelter": True, "arnona": "450", "vaad": "120", "entry_date": "מיידי", "address": "רחוב הרב קוק 5, גבעתיים",
    "is_agent": False,
}
_SPARSE_FIELDS = {"rooms_val": 2.5, "price_val": 5800, "distance_text": "מרחק לא חושב", "floor": None, "elevator": None,
                  "parking": "", "shelter": None, "arnona": "", "vaad": "", "entry_date": None, "address": ""}


def test_listing_message_omits_placeholders_for_unknown_fields():
    assert "?" not in telegram_notifier._format_listing_message(_SPARSE_FIELDS, 48)


def test_listing_message_escapes_html_in_scraped_address():
    message = telegram_notifier._format_listing_message(dict(_FULL_FIELDS, address="<script>x</script> & co"), 70)

    assert "&lt;script&gt;x&lt;/script&gt; &amp; co" in message


def test_listing_message_shows_total_monthly_cost():
    expected = f"{scoring.compute_total_monthly_cost(_FULL_FIELDS):,} ₪"

    assert expected in telegram_notifier._format_listing_message(_FULL_FIELDS, 70)


def test_listing_message_no_longer_asks_for_a_rating():
    assert "דרג" not in telegram_notifier._format_listing_message(_FULL_FIELDS, 70)


def test_listing_message_shows_ground_floor_as_karka():
    assert "קומה: קרקע" in telegram_notifier._format_listing_message(_FULL_FIELDS, 70)


def test_listing_message_skips_a_distance_placeholder_that_is_not_a_number():
    assert "ק\"מ" not in telegram_notifier._format_listing_message(_SPARSE_FIELDS, 48)


def test_score_tier_is_green_at_the_good_threshold():
    assert telegram_notifier._score_tier(config.TELEGRAM_SCORE_GOOD) == "🟢"


def test_score_tier_is_yellow_at_the_ok_threshold():
    assert telegram_notifier._score_tier(config.TELEGRAM_SCORE_OK) == "🟡"


def test_score_tier_is_red_below_the_ok_threshold():
    assert telegram_notifier._score_tier(config.TELEGRAM_SCORE_OK - 1) == "🔴"


def test_new_listing_keyboard_is_only_the_open_post_link():
    keyboard = telegram_notifier.build_link_keyboard("123", "456")

    assert keyboard == {"inline_keyboard": [[{"text": "🔗 פתח פוסט", "url": "https://www.facebook.com/groups/123/posts/456/"}]]}


def test_listing_keyboard_has_a_link_button_to_the_canonical_post_url():
    keyboard = telegram_notifier.build_listing_keyboard(0, 0, "123", "456")

    assert keyboard["inline_keyboard"][1] == [{"text": "🔗 פתח פוסט", "url": "https://www.facebook.com/groups/123/posts/456/"}]


def test_listing_keyboard_keeps_both_vote_buttons_in_the_first_row():
    keyboard = telegram_notifier.build_listing_keyboard(2, 1, "123", "456")

    assert [button["callback_data"] for button in keyboard["inline_keyboard"][0]] == ["up:123:456", "down:123:456"]


def test_send_listing_alert_uses_html_without_a_link_preview(monkeypatch):
    sent = {}
    monkeypatch.setattr(telegram_notifier, "_configured", lambda: True)
    monkeypatch.setattr(telegram_notifier, "_call", lambda method, payload: sent.update(payload) or {"message_id": 1})
    monkeypatch.setattr(telegram_notifier.storage, "set_telegram_message", lambda *args: None)

    telegram_notifier.send_listing_alert("https://www.facebook.com/groups/123/posts/456/", _FULL_FIELDS, 70)

    assert (sent["parse_mode"], sent["disable_web_page_preview"]) == ("HTML", True)


def test_send_listing_alert_attaches_no_vote_buttons(monkeypatch):
    sent = {}
    monkeypatch.setattr(telegram_notifier, "_configured", lambda: True)
    monkeypatch.setattr(telegram_notifier, "_call", lambda method, payload: sent.update(payload) or {"message_id": 1})
    monkeypatch.setattr(telegram_notifier.storage, "set_telegram_message", lambda *args: None)

    telegram_notifier.send_listing_alert("https://www.facebook.com/groups/123/posts/456/", _FULL_FIELDS, 70)

    assert all("callback_data" not in button for row in sent["reply_markup"]["inline_keyboard"] for button in row)


def test_maps_link_is_built_for_a_known_street():
    url = telegram_notifier._maps_url({"address": "ריינס 22, גבעתיים"})

    assert url.startswith("https://www.google.com/maps/search/?api=1&query=")


def test_maps_link_query_carries_the_address_and_country():
    from urllib.parse import parse_qs, urlparse

    url = telegram_notifier._maps_url({"address": "ריינס 22, גבעתיים"})

    assert parse_qs(urlparse(url).query)["query"] == ["ריינס 22, גבעתיים, ישראל"]


def test_maps_link_is_hidden_for_a_city_only_address():
    assert telegram_notifier._maps_url({"address": "גבעתיים"}) is None


def test_maps_link_is_hidden_for_a_neighborhood_only_address():
    assert telegram_notifier._maps_url({"address": "בשכונת גבעת רמב״ם המבוקשת"}) is None


def test_maps_link_is_hidden_when_the_address_is_missing():
    assert telegram_notifier._maps_url({"address": ""}) is None


def test_maps_link_respects_a_downgraded_stored_confidence():
    assert telegram_notifier._maps_url({"address": "ריינס 22, גבעתיים", "address_confidence": "low"}) is None


def test_link_keyboard_adds_the_maps_button_next_to_the_post_button():
    keyboard = telegram_notifier.build_link_keyboard("123", "456", "https://www.google.com/maps/search/?api=1&query=x")

    assert [button["text"] for button in keyboard["inline_keyboard"][0]] == ["🔗 פתח פוסט", "🗺 פתח במפות"]


def test_link_keyboard_without_a_maps_url_has_only_the_post_button():
    keyboard = telegram_notifier.build_link_keyboard("123", "456")

    assert [button["text"] for button in keyboard["inline_keyboard"][0]] == ["🔗 פתח פוסט"]


def test_send_listing_alert_includes_the_maps_button_for_a_known_street(monkeypatch):
    sent = {}
    monkeypatch.setattr(telegram_notifier, "_configured", lambda: True)
    monkeypatch.setattr(telegram_notifier, "_call", lambda method, payload: sent.update(payload) or {"message_id": 1})
    monkeypatch.setattr(telegram_notifier.storage, "set_telegram_message", lambda *args: None)

    telegram_notifier.send_listing_alert("https://www.facebook.com/groups/123/posts/456/", _FULL_FIELDS, 70)

    assert "🗺 פתח במפות" in [button["text"] for button in sent["reply_markup"]["inline_keyboard"][0]]



def test_whatsapp_link_targets_the_international_form_of_the_mobile_number():
    url = telegram_notifier._whatsapp_url({"contact_phone": "0541234567", "address": "ביאליק 14"})

    assert url.startswith("https://wa.me/972541234567?text=")


def test_whatsapp_link_prefills_an_encoded_hebrew_opener_naming_the_address():
    url = telegram_notifier._whatsapp_url({"contact_phone": "0541234567", "address": "ביאליק 14"})

    assert unquote_plus(url.split("text=", 1)[1]) == "היי, ראיתי את המודעה על הדירה בביאליק 14. היא עדיין רלוונטית?"


def test_whatsapp_link_is_absent_without_a_phone():
    assert telegram_notifier._whatsapp_url({"address": "ביאליק 14"}) is None


def test_send_listing_alert_includes_the_whatsapp_button_when_the_post_had_a_mobile(monkeypatch):
    sent = {}
    monkeypatch.setattr(telegram_notifier, "_configured", lambda: True)
    monkeypatch.setattr(telegram_notifier, "_call", lambda method, payload: sent.update(payload) or {"message_id": 1})
    monkeypatch.setattr(telegram_notifier.storage, "set_telegram_message", lambda *args: None)

    telegram_notifier.send_listing_alert("https://www.facebook.com/groups/123/posts/456/",
                                         dict(_FULL_FIELDS, contact_phone="0541234567"), 70)

    assert "💬 וואטסאפ" in [button["text"] for button in sent["reply_markup"]["inline_keyboard"][0]]
