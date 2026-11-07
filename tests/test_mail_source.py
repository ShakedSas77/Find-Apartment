"""
Tests for the alert-email source's extraction step: email -> readable text, and
the read-only mailbox access. Only the IMAP server (network) is faked.
"""
from email.message import EmailMessage

import pytest

import mail_source


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
