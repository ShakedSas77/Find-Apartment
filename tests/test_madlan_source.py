"""
Tests for the Madlan listing source: mapping an API bulletin into the pipeline's
ApartmentData shape, and running listings through the real pipeline with no LLM
call. Only the network (Madlan HTTP, distance lookup) is faked; storage is a
real temp DB.
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.evaluate as evaluate
import madlan_source
import storage

_RECENT = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
_OLD = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat().replace("+00:00", "Z")


def _bulletin(**overrides):
    item = {
        "id": "abc123", "price": 6000, "beds": 3, "area": 75, "floor": 2, "firstTimeSeen": _RECENT,
        "rentalBrokerFee": False, "address": "ביאליק 14",
        "addressDetails": {"city": "רמת גן", "streetName": "ביאליק", "streetNumber": "14", "neighbourhood": "מרכז העיר"},
    }
    item.update(overrides)
    return item


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()
    monkeypatch.setattr(evaluate, "_run_queued_listing_keys", set())
    monkeypatch.setattr(evaluate, "get_walking_distance",
                        lambda address: ("1.2", 1200, "high", "", "straight_line_estimate", None, None))
    monkeypatch.setattr(evaluate, "analyze_post_with_llm",
                        lambda text: pytest.fail("Madlan listings must not reach the LLM"))
    monkeypatch.setattr(madlan_source, "MADLAN_LOCATION_DOC_IDS", ["רמת-גן-ישראל"])


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_address_is_built_from_street_number_and_city():
    assert madlan_source.to_apartment_data(_bulletin())["address"] == "ביאליק 14, רמת גן"


def test_broker_fee_marks_the_listing_as_agent():
    assert madlan_source.to_apartment_data(_bulletin(rentalBrokerFee=True))["is_agent"] is True


def test_missing_broker_fee_leaves_agent_unknown():
    assert madlan_source.to_apartment_data(_bulletin(rentalBrokerFee=None))["is_agent"] is None


def test_listing_text_states_no_agent_so_text_detection_agrees():
    item = _bulletin(rentalBrokerFee=False)

    assert "ללא תיווך" in madlan_source.listing_text(item, madlan_source.to_apartment_data(item))


def test_listing_seen_long_ago_is_not_recent():
    assert madlan_source._is_recent(_OLD) is False


def test_listing_without_a_date_counts_as_recent():
    assert madlan_source._is_recent(None) is True


def test_post_date_uses_the_sheets_day_month_format():
    assert madlan_source._post_date("2026-11-05T09:00:00Z") == "05/11"


def test_fetch_sends_the_visitor_token_as_a_bearer_header(monkeypatch):
    sent = {}
    monkeypatch.setattr(madlan_source.env, "get_madlan_token", lambda: "tok123")
    monkeypatch.setattr(madlan_source.requests, "post",
                        lambda url, json, headers, timeout: sent.update(headers) or
                        _FakeResponse({"data": {"searchPoiV2": {"poi": []}}}))

    madlan_source.fetch_madlan_listings("רמת-גן-ישראל")

    assert sent["authorization"] == "Bearer tok123"


def test_fetch_raises_on_graphql_errors(monkeypatch):
    monkeypatch.setattr(madlan_source.requests, "post",
                        lambda *args, **kwargs: _FakeResponse({"errors": [{"message": "unauthorized"}]}))

    with pytest.raises(RuntimeError):
        madlan_source.fetch_madlan_listings("רמת-גן-ישראל")


def test_matching_listing_is_queued_without_an_llm_call(monkeypatch):
    monkeypatch.setattr(madlan_source, "fetch_madlan_listings", lambda doc_id: [_bulletin()])

    stats = madlan_source.run_madlan(sheet=None, seen_urls=set(), live=False)

    assert stats["added"] == 1


def test_out_of_range_price_is_recorded_as_rejected(monkeypatch):
    monkeypatch.setattr(madlan_source, "fetch_madlan_listings", lambda doc_id: [_bulletin(price=9500)])

    madlan_source.run_madlan(sheet=None, seen_urls=set(), live=False)

    assert storage.should_skip("https://www.madlan.co.il/listings/abc123") is True


def test_old_listing_is_skipped_before_evaluation(monkeypatch):
    monkeypatch.setattr(madlan_source, "fetch_madlan_listings", lambda doc_id: [_bulletin(firstTimeSeen=_OLD)])

    stats = madlan_source.run_madlan(sheet=None, seen_urls=set(), live=False)

    assert stats["prefiltered"] == 1


def test_api_failure_skips_the_location_without_raising(monkeypatch):
    def fail(doc_id):
        raise RuntimeError("401")
    monkeypatch.setattr(madlan_source, "fetch_madlan_listings", fail)

    stats = madlan_source.run_madlan(sheet=None, seen_urls=set(), live=False)

    assert stats["posts_seen"] == 0
