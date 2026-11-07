"""
Tests for --stats' suspicious-rejects audit: a pre-filter reject whose text still states a target room
count and an in-range price is surfaced, grouped by rule; a reject that doesn't fit isn't.
"""
import pytest

import apartment_bot
import storage

_FITS = 'מחפשים דייר לדירת 3 חדרים ברמת גן, 6,000 ש"ח לחודש'
_DOES_NOT_FIT = 'מחפשים דירת 3 חדרים עד 9,000 ש"ח'


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


def _reject(url, text, reason="negative_keyword:מחפש"):
    storage.record_post(url, "https://www.facebook.com/groups/1", text, storage.VERDICT_PREFILTERED,
                        analysis={"reject_reason": reason})


def test_a_reject_that_fits_rooms_and_price_is_listed_under_its_rule(capsys):
    _reject("https://www.facebook.com/groups/1/posts/1/", _FITS)

    apartment_bot._print_suspicious_rejects()

    assert "1  negative_keyword:מחפש" in capsys.readouterr().out


def test_a_reject_outside_the_price_range_is_not_listed(capsys):
    _reject("https://www.facebook.com/groups/1/posts/2/", _DOES_NOT_FIT)

    apartment_bot._print_suspicious_rejects()

    assert capsys.readouterr().out == ""
