"""
Tests for fb_scraper's pure decision logic (no browser). The scroll early-stop rule is the
piece whose regressions silently turn a 3-scroll scan into an 80-scroll crawl every run.
"""
from datetime import datetime, timedelta

from config import GROUP_YIELD_MIN_HISTORY, GROUP_YIELD_MIN_SCROLLS, GROUP_YIELD_POOR_RATE, GROUP_YIELD_RICH_RATE
from fb_scraper import _update_old_streak, _yield_scaled_scroll_cap

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
