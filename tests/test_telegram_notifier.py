"""
Regression test for the bot-token leak: requests/urllib3 exceptions embed the full
request URL, and Telegram carries the token in the URL path.
"""
import requests

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

