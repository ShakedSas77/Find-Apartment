"""
Tests for the alert-email source's extraction step: email -> readable text, and
the read-only mailbox access. Only the IMAP server (network) is faked.
"""
from email.message import EmailMessage

import pytest

from core.evaluate import _content_prefilter
from sources import mail_source


def _email(plain: str | None = None, html_body: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "alerts@yad2.co.il"
    msg["Subject"] = "דירות חדשות"
    if plain is not None:
        msg.set_content(plain)
    if html_body is not None:
        if plain is None:
            msg.set_content(html_body, subtype="html")
        else:
            msg.add_alternative(html_body, subtype="html")
    return msg


class _FakeImap:
    """Records how the mailbox was opened and serves one stored message."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.select_args = None
        self.fetch_spec = None

    def login(self, user, password):
        pass

    def select(self, folder, readonly=False):
        self.select_args = (folder, readonly)
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b"1"]

    def fetch(self, msg_id, spec):
        self.fetch_spec = spec
        return "OK", [(b"1 (BODY[] {n}", self.raw), b")"]

    def logout(self):
        pass


@pytest.fixture
def fake_imap(monkeypatch):
    server = _FakeImap(_email(plain="3 חדרים בביאליק").as_bytes())
    monkeypatch.setattr(mail_source.imaplib, "IMAP4_SSL", lambda host: server)
    monkeypatch.setattr(mail_source.env, "get_alerts_imap_credentials", lambda: ("me@gmail.com", "app-pass", "apartment-alerts"))
    return server


def test_plain_text_part_is_preferred_over_html():
    msg = _email(plain="טקסט רגיל", html_body="<p>טקסט HTML</p>")

    assert mail_source.message_text(msg) == "טקסט רגיל"


def test_html_only_email_is_converted_to_text_with_line_breaks():
    msg = _email(html_body="<div>3 חדרים</div><div>6,000 ₪</div>")

    assert mail_source.message_text(msg) == "3 חדרים\n6,000 ₪"


def test_numeric_html_entities_become_hebrew():
    msg = _email(html_body="<p>&#1491;&#1497;&#1512;&#1492;</p>")

    assert mail_source.message_text(msg) == "דירה"


def test_style_blocks_are_dropped():
    msg = _email(html_body="<style>.a{color:red}</style><p>דירה</p>")

    assert mail_source.message_text(msg) == "דירה"


def test_mailbox_is_opened_read_only(fake_imap):
    mail_source.fetch_recent_messages(days=2)

    assert fake_imap.select_args == ('"apartment-alerts"', True)


def test_messages_are_fetched_without_marking_them_read(fake_imap):
    mail_source.fetch_recent_messages(days=2)

    assert fake_imap.fetch_spec == "(BODY.PEEK[])"


def test_missing_credentials_raise_before_connecting(monkeypatch):
    monkeypatch.setattr(mail_source.env, "get_alerts_imap_credentials", lambda: (None, None, "INBOX"))
    monkeypatch.setattr(mail_source.imaplib, "IMAP4_SSL", lambda host: pytest.fail("must not connect"))

    with pytest.raises(RuntimeError, match="missing"):
        mail_source.fetch_recent_messages(days=2)


def test_dump_writes_one_text_file_per_email(fake_imap, tmp_path):
    mail_source.dump_alert_emails(days=2, out_dir=str(tmp_path))

    assert "3 חדרים בביאליק" in (tmp_path / "alert_001.txt").read_text(encoding="utf-8")


# A trimmed, made-up Yad2 daily digest in the real layout (2026-11-08): the HTML is the only part,
# each listing's link is wrapped in the click-tracking redirect, and the city is only in the title.
def _yad2_listing(item_id: str, price: str, street: str, details: str) -> str:
    tracked = f"https://x.awstrack.me/L0/https:%2F%2Fwww.yad2.co.il%2Frealestate%2Fitem%2F{item_id}%3FisFromMyAlerts=true/1/abc"
    return (f'<div><a href="{tracked}"><img></a></div><div>ירד ב-</div><div>\u200f{price} \u200f₪</div>'
            f"<div>{street}</div><div>{details}</div><div><a href=\"{tracked}\">לפרטים נוספים</a></div>")


def _yad2_digest(*listings: str) -> EmailMessage:
    body = ("<div>מצאנו 2 מודעות חדשות שמתאימות לחיפוש שלך:</div><div>דירות להשכרה בגבעתיים</div>"
            "<div>מחיר מינימלי:</div><div>5500</div><div>|</div>"
            + "".join(listings) + "<div><a href=\"https://x.awstrack.me/my-alerts\">לכל המודעות</a></div>")
    return _email(html_body=body)


_DIGEST = _yad2_digest(
    _yad2_listing("go1c2c33", "6,500", 'כ"ג 11', "חברת חשמל | דירה | 3.5 חדרים | 90 מ״ר"),
    _yad2_listing("r9hn1gua", "6,300", "כצנלסון 71", "בורוכוב | דירה | 3 חדרים | 65 מ״ר"),
)


def test_digest_listing_gets_its_own_item_link_out_of_the_tracking_redirect():
    assert mail_source.parse_yad2_digest(_DIGEST)[1]["url"] == "https://www.yad2.co.il/realestate/item/r9hn1gua"


def test_digest_listing_price_is_read():
    assert mail_source.parse_yad2_digest(_DIGEST)[0]["price"] == 6500


def test_digest_listing_rooms_are_read():
    assert mail_source.parse_yad2_digest(_DIGEST)[0]["rooms"] == 3.5


def test_digest_listing_street_is_the_line_above_the_details():
    assert mail_source.parse_yad2_digest(_DIGEST)[0]["street"] == 'כ"ג 11'


def test_digest_city_comes_from_the_search_title():
    assert mail_source.parse_yad2_digest(_DIGEST)[1]["city"] == "גבעתיים"


def test_digest_whose_links_dont_pair_up_with_its_listings_is_skipped():
    listing = _yad2_listing("go1c2c33", "6,500", 'כ"ג 11', "חברת חשמל | דירה | 3.5 חדרים | 90 מ״ר")
    unlinked = listing.replace("go1c2c33", "")

    assert mail_source.parse_yad2_digest(_yad2_digest(listing, unlinked)) == []


def test_digest_listing_text_passes_the_same_pre_filters_as_a_post():
    listing = mail_source.parse_yad2_digest(_DIGEST)[1]

    assert _content_prefilter(mail_source._listing_text(listing)) is None
