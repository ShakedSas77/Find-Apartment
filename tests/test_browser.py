"""
Unit tests for browser.py's pure helpers.
"""
from sources.browser import _normalize_user_agent

_HEADLESS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "HeadlessChrome/149.0.7827.55 Safari/537.36")
_HEADFUL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/149.0.0.0 Safari/537.36")


def test_headless_ua_is_normalized_to_the_reduced_headful_form():
    assert _normalize_user_agent(_HEADLESS_UA) == _HEADFUL_UA


def test_already_reduced_headful_ua_is_left_unchanged():
    assert _normalize_user_agent(_HEADFUL_UA) == _HEADFUL_UA


def test_ua_without_chrome_token_is_left_unchanged():
    assert _normalize_user_agent("curl/8.0") == "curl/8.0"
