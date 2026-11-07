"""
Tests for --replay: by default it reuses each post's stored LLM parse (no LLM
calls), and an already-added post must not be reported as a duplicate of its
own stored record. Storage is a real temp DB; the LLM is the only fake.
"""
import json

import pytest

import apartment_bot
import storage
from core.dedupe import _known_added_listing_keys, _listing_dedupe_key

_URL = "https://www.facebook.com/groups/1/posts/1/"
_TEXT = "להשכרה דירת 3 חדרים ברחוב ביאליק 14 רמת גן, 6,000 ש\"ח לחודש, כניסה מיידית"
_PARSE = {"rooms": 3, "price": 6000, "address": "ביאליק 14, רמת גן", "entry_date": "מיידי"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


def _store(url, verdict, parsed=_PARSE, text=_TEXT):
    storage.record_post(url, "https://www.facebook.com/groups/1", text, verdict, parsed,
                        analysis={"address": parsed and parsed["address"], "rooms_val": 3.0, "price_val": 6000.0})


def test_replay_reuses_the_stored_parse_without_calling_the_llm(monkeypatch):
    _store(_URL, storage.VERDICT_ADDED)
    monkeypatch.setattr(apartment_bot, "analyze_post_with_llm", lambda text: pytest.fail("replay must not call the LLM"))

    assert apartment_bot.replay_all_posts() == []


def test_replay_with_llm_re_parses_the_post(monkeypatch):
    _store(_URL, storage.VERDICT_ADDED)
    calls = []
    monkeypatch.setattr(apartment_bot, "analyze_post_with_llm", lambda text: calls.append(text) or dict(_PARSE, price=9000))

    assert apartment_bot.replay_all_posts(use_llm=True) == [(_URL, storage.VERDICT_ADDED, storage.VERDICT_REJECTED_PRICE)]


def test_post_never_parsed_is_not_judged_without_the_llm(monkeypatch):
    _store(_URL, storage.VERDICT_PRICE_UNKNOWN, parsed=None)
    monkeypatch.setattr(apartment_bot, "analyze_post_with_llm", lambda text: pytest.fail("replay must not call the LLM"))

    assert apartment_bot.replay_all_posts() == []


def test_known_listing_keys_can_leave_out_a_posts_own_record():
    _store(_URL, storage.VERDICT_ADDED)

    assert _listing_dedupe_key("ביאליק 14, רמת גן", 3.0, 6000.0) not in _known_added_listing_keys(exclude_url=_URL)


def test_a_different_post_of_the_same_listing_is_still_a_duplicate():
    _store(_URL, storage.VERDICT_ADDED)

    assert _listing_dedupe_key("ביאליק 14, רמת גן", 3.0, 6000.0) in _known_added_listing_keys(
        exclude_url="https://www.facebook.com/groups/2/posts/2/")
