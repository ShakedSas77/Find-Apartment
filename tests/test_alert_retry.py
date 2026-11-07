"""
Tests for re-sending Telegram alerts that failed after the listing was already
written to the sheet and recorded as added — before this, such an alert was
lost for good, since an added post is never rescanned.
"""
from datetime import datetime, timedelta

import pytest

import fb_scraper
import storage
from config import SHEET_HEADERS

_URL = "https://www.facebook.com/groups/1/posts/1/"


@pytest.fixture(autouse=True)
def empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


@pytest.fixture
def sent(monkeypatch):
    """Captures alerts at the Telegram boundary instead of sending them."""
    calls = []
    monkeypatch.setattr(fb_scraper.telegram_notifier, "send_listing_alert",
                        lambda url, fields, score: calls.append((url, fields, score)) or "1")
    return calls


class FakeSheet:
    def __init__(self, data):
        self.data = data

    def get_all_values(self):
        return self.data


def _add(url, hours_ago=0):
    storage.record_post(url, "https://www.facebook.com/groups/1", "text", storage.VERDICT_ADDED)
    stamp = (datetime.now() - timedelta(hours=hours_ago)).isoformat()
    with storage._connect() as conn:
        conn.execute("UPDATE posts SET last_processed = ? WHERE url = ?", (stamp, url))


def _row(url, price="6000", score="72"):
    row = [""] * len(SHEET_HEADERS)
    row[0], row[1], row[2] = url, price, "3"
    row[SHEET_HEADERS.index("ציון התאמה")] = score
    return row


def test_added_listing_without_message_id_is_owed():
    _add(_URL)

    assert storage.get_owed_alert_urls(48) == [_URL]


def test_listing_with_a_sent_message_is_not_owed():
    _add(_URL)
    storage.set_telegram_message(_URL, "chat", "42")

    assert storage.get_owed_alert_urls(48) == []


def test_listing_older_than_the_window_is_not_owed():
    _add(_URL, hours_ago=72)

    assert storage.get_owed_alert_urls(48) == []


def test_owed_alert_is_resent_with_the_sheet_rows_values(sent):
    _add(_URL)

    fb_scraper._resend_owed_alerts(FakeSheet([SHEET_HEADERS, _row(_URL, price="6100", score="72")]))

    assert [(url, fields["price_val"], score) for url, fields, score in sent] == [(_URL, 6100.0, 72)]


def test_owed_listing_missing_from_the_sheet_is_not_sent(sent):
    _add(_URL)

    fb_scraper._resend_owed_alerts(FakeSheet([SHEET_HEADERS]))

    assert sent == []
