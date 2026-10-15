"""
Tests for fb_scraper's pure decision logic (no browser). The scroll early-stop rule is the
piece whose regressions silently turn a 3-scroll scan into an 80-scroll crawl every run.
"""
from datetime import datetime, timedelta

from fb_scraper import _update_old_streak

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
