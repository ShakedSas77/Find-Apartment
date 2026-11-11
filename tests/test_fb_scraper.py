"""
Tests for fb_scraper's pure decision logic (no browser). The scroll early-stop rule is the
piece whose regressions silently turn a 3-scroll scan into an 80-scroll crawl every run.
"""
from datetime import datetime, timedelta

import pytest

from config import GROUP_YIELD_MIN_HISTORY, GROUP_YIELD_MIN_SCROLLS, GROUP_YIELD_POOR_RATE, GROUP_YIELD_RICH_RATE
from core.errors import LoginRequired
from sources import fb_scraper
from sources.fb_scraper import (
    _ensure_logged_in, _new_totals, _read_post_text, _scan_groups_sequentially, _tally, _update_old_streak,
    _yield_scaled_scroll_cap,
)

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


def test_a_shared_marketplace_cards_caption_is_appended_to_the_post_text():
    # The card's photos are links too, with no text; only the caption link carries the price.
    caption = "₪9,250\n · תל אביב - יפו\nלהשכרה דירה 3 חדרים קומה 45"
    article = _FakeArticle(_WHOLE, {
        'div[data-ad-rendering-role="story_message"]': [_BODY],
        'a[href*="/commerce/listing/"]': ["", "", caption],
    })

    assert _read_post_text(article) == _BODY + "\n" + caption


def test_post_from_the_cutoffs_own_day_is_not_treated_as_old():
    same_day_midnight = _CUTOFF.replace(hour=0, minute=0)

    assert _update_old_streak(2, is_known=False, post_dt=same_day_midnight, cutoff=_CUTOFF) == 0


def _group_stats(added=0, checkpoint=False):
    return {"added": added, "posts_seen": 10, "prefiltered": 7, "llm_parsed": 3, "checkpoint_hit": checkpoint}


def _totals_after_two_groups():
    totals = _new_totals(2)
    _tally(totals, _group_stats(added=2))
    _tally(totals, _group_stats(added=1))
    return totals


def test_tally_counts_each_group_as_scanned():
    assert _totals_after_two_groups()["groups_scanned"] == 2


def test_tally_sums_the_added_listings():
    assert _totals_after_two_groups()["total_added"] == 3


def test_tally_sums_the_posts_seen():
    assert _totals_after_two_groups()["posts_seen"] == 20


def _scan_four_groups_with_a_checkpoint_on_the_second(monkeypatch):
    outcomes = iter([_group_stats(), _group_stats(checkpoint=True), _group_stats()])
    monkeypatch.setattr(fb_scraper.time, "sleep", lambda s: None)
    totals = _new_totals(4)
    _scan_groups_sequentially(None, ["g1", "g2", "g3", "g4"], None, set(), True, False, totals,
                              scan_group=lambda *args: next(outcomes))
    return totals


def test_sequential_checkpoint_stops_the_scan(monkeypatch):
    assert _scan_four_groups_with_a_checkpoint_on_the_second(monkeypatch)["groups_scanned"] == 2


def test_sequential_checkpoint_counts_the_remaining_groups_as_skipped(monkeypatch):
    assert _scan_four_groups_with_a_checkpoint_on_the_second(monkeypatch)["checkpoint_skipped"] == 3


def test_a_crashed_group_is_not_counted_as_scanned(monkeypatch):
    def crash(*args):
        raise RuntimeError("page crashed")
    monkeypatch.setattr(fb_scraper.time, "sleep", lambda s: None)
    totals = _new_totals(1)

    _scan_groups_sequentially(None, ["g1"], None, set(), True, False, totals, scan_group=crash)

    assert totals["groups_scanned"] == 0


class _FakeLoginForm:
    def __init__(self, is_shown):
        self.is_shown = is_shown

    @property
    def first(self):
        return self

    def is_visible(self):
        return self.is_shown


class _FakeLoginPage:
    def __init__(self, is_logged_in):
        self.is_logged_in = is_logged_in

    def goto(self, url, wait_until):
        pass

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        return _FakeLoginForm(is_shown=not self.is_logged_in)


def test_headless_run_with_a_lost_session_raises_login_required():
    with pytest.raises(LoginRequired):
        _ensure_logged_in(_FakeLoginPage(is_logged_in=False), headless=True)


def test_headless_run_with_a_live_session_continues():
    assert _ensure_logged_in(_FakeLoginPage(is_logged_in=True), headless=True) is None
