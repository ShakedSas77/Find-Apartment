"""
Tests for fb_scraper's pure decision logic (no browser). The scroll early-stop rule is the
piece whose regressions silently turn a 3-scroll scan into an 80-scroll crawl every run.
"""
from datetime import datetime, timedelta

from config import GROUP_YIELD_MIN_HISTORY, GROUP_YIELD_MIN_SCROLLS, GROUP_YIELD_POOR_RATE, GROUP_YIELD_RICH_RATE
from fb_scraper import _read_post_text, _update_old_streak, _yield_scaled_scroll_cap

_CUTOFF = datetime(2026, 10, 1, 12, 0)
_AFTER_CUTOFF = _CUTOFF + timedelta(days=1)
_BEFORE_CUTOFF = _CUTOFF - timedelta(days=1)


def test_known_post_extends_the_streak():
    assert _update_old_streak(2, is_known=True, post_dt=_AFTER_CUTOFF, cutoff=_CUTOFF) == 3


def test_known_undated_post_extends_the_streak():
    assert _update_old_streak(2, is_known=True, post_dt=None, cutoff=_CUTOFF) == 3


def test_unknown_post_older_than_cutoff_extends_the_streak():
    assert _update_old_streak(2, is_known=False, post_dt=_BEFORE_CUTOFF, cutoff=_CUTOFF) == 3


def test_fresh_unknown_post_resets_the_streak():
    assert _update_old_streak(4, is_known=False, post_dt=_AFTER_CUTOFF, cutoff=_CUTOFF) == 0


def test_unknown_undated_post_leaves_the_streak_unchanged():
    assert _update_old_streak(4, is_known=False, post_dt=None, cutoff=_CUTOFF) == 4


def test_low_yield_group_gets_the_minimum_scroll_cap():
    assert _yield_scaled_scroll_cap(added=1, total=1000, full_cap=30) == GROUP_YIELD_MIN_SCROLLS


def test_high_yield_group_keeps_the_full_scroll_cap():
    assert _yield_scaled_scroll_cap(added=20, total=1000, full_cap=30) == 30


def test_mid_yield_group_scales_linearly_between_the_bounds():
    midpoint_rate = (GROUP_YIELD_POOR_RATE + GROUP_YIELD_RICH_RATE) / 2

    assert _yield_scaled_scroll_cap(added=round(midpoint_rate * 10000), total=10000, full_cap=30) == 19


def test_group_with_little_history_keeps_the_full_scroll_cap():
    assert _yield_scaled_scroll_cap(added=0, total=GROUP_YIELD_MIN_HISTORY - 1, full_cap=30) == 30


class _FakeElement:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _FakeLocator:
    def __init__(self, elements):
        self.elements = elements

    def all(self):
        return self.elements


class _FakeArticle:
    """An article whose marked containers (selector -> texts) and whole-article text are given."""

    def __init__(self, whole_text, containers=None):
        self.whole_text = whole_text
        self.containers = containers or {}

    def locator(self, selector):
        return _FakeLocator([_FakeElement(t) for t in self.containers.get(selector, [])])

    def inner_text(self):
        return self.whole_text


_BODY = "להשכרה 3 חדרים ברמת גן, 6,000 ש\"ח"
_WHOLE = "Orly Schick\n7 hours ago\n· Follow\n" + _BODY + "\n12\nNoy Azulay\n·\n1h\nמחיר?\nReply"


def test_post_text_is_read_from_the_story_message_container():
    article = _FakeArticle(_WHOLE, {'div[data-ad-rendering-role="story_message"]': [_BODY]})

    assert _read_post_text(article) == _BODY


def test_shared_post_joins_the_sharers_note_and_the_original():
    article = _FakeArticle(_WHOLE, {'div[data-ad-rendering-role="story_message"]': ["שימו לב לדירה הזאת", _BODY]})

    assert _read_post_text(article) == "שימו לב לדירה הזאת\n" + _BODY


def test_falls_back_to_the_whole_article_when_no_container_is_marked():
    assert _read_post_text(_FakeArticle(_WHOLE)) == _WHOLE


def test_falls_back_when_the_marked_container_is_too_short_to_be_the_post():
    article = _FakeArticle(_WHOLE, {'div[data-ad-rendering-role="story_message"]': ["😀"]})

    assert _read_post_text(article) == _WHOLE


def test_post_from_the_cutoffs_own_day_is_not_treated_as_old():
    same_day_midnight = _CUTOFF.replace(hour=0, minute=0)

    assert _update_old_streak(2, is_known=False, post_dt=same_day_midnight, cutoff=_CUTOFF) == 0
