"""
Tests for fb_scraper's pure decision logic (no browser). The scroll early-stop rule is the
piece whose regressions silently turn a 3-scroll scan into an 80-scroll crawl every run.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import GROUP_YIELD_MIN_HISTORY, GROUP_YIELD_MIN_SCROLLS, GROUP_YIELD_POOR_RATE, GROUP_YIELD_RICH_RATE
from core import storage
from core.errors import LoginRequired
from sources import fb_scraper
from sources.fb_scraper import (
    _DEAD_LINKS_CHECKED_KEY, _ScanProgress, _load_scan_pace, _save_scan_pace, _dead_link_check_due, _ensure_logged_in, _estimate_seconds_left,
    _blocked_membership, _new_totals, _progress_line, _read_post_text,
    _scan_group_page, _scan_groups_sequentially, my_groups_from_links, _scope_group_urls, _tally, _update_old_streak, _yield_scaled_scroll_cap,
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


def test_no_time_estimate_before_the_first_group_finishes():
    assert _estimate_seconds_left(done=0, total=54, done_elapsed_s=0, elapsed_s=200) is None


def test_time_left_keeps_counting_down_while_no_group_finishes():
    # 2 of 10 groups done after 120s projects a 600s scan; a minute later there's a minute less left.
    earlier = _estimate_seconds_left(done=2, total=10, done_elapsed_s=120, elapsed_s=180)
    later = _estimate_seconds_left(done=2, total=10, done_elapsed_s=120, elapsed_s=240)

    assert earlier - later == 60


def test_progress_line_shows_the_bar_counts_and_time_left():
    line = _progress_line(done=12, total=24, added=3, elapsed_s=300, left_s=300)

    assert line == "[############------------] 12/24 groups | 3 added | 5 min elapsed | ~5 min left"


def test_a_crashed_group_still_moves_the_progress_bar(monkeypatch, db):
    def crash(*args):
        raise RuntimeError("page crashed")
    monkeypatch.setattr(fb_scraper.time, "sleep", lambda s: None)
    totals = _new_totals(1)
    progress = _ScanProgress(1, totals)

    _scan_groups_sequentially(None, ["g1"], None, set(), True, False, totals, scan_group=crash, progress=progress)
    progress.stop()

    assert progress.done == 1


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


def test_dead_link_check_runs_when_it_never_ran_before(db):
    assert _dead_link_check_due(datetime(2026, 11, 12, 9, 0)) is True


def test_dead_link_check_is_skipped_within_the_interval(db):
    storage.set_bot_state(_DEAD_LINKS_CHECKED_KEY, "2026-11-12T08:00:00")

    assert _dead_link_check_due(datetime(2026, 11, 12, 9, 0)) is False


def test_dead_link_check_runs_again_once_the_interval_passed(db):
    storage.set_bot_state(_DEAD_LINKS_CHECKED_KEY, "2026-11-11T08:00:00")

    assert _dead_link_check_due(datetime(2026, 11, 12, 9, 0)) is True


class _FakeGroupPage:
    def __init__(self, button, article_count):
        self.button = button
        self.article_count = article_count

    def get_by_role(self, role, name):
        return _FakeLoginForm(is_shown=bool(name.match(self.button)))

    def locator(self, selector):
        return self

    def count(self):
        return self.article_count


def test_a_private_group_not_joined_is_detected_before_scrolling():
    assert _blocked_membership(_FakeGroupPage("Join group", article_count=0)) == "not_member"


def test_a_group_with_a_pending_join_request_is_detected_before_scrolling():
    assert _blocked_membership(_FakeGroupPage("Cancel request", article_count=0)) == "pending"


def test_a_public_group_showing_posts_to_non_members_is_still_scanned():
    assert _blocked_membership(_FakeGroupPage("Join group", article_count=3)) is None


def test_last_runs_pace_gives_an_estimate_before_the_first_group_finishes():
    assert _estimate_seconds_left(done=0, total=54, done_elapsed_s=0, elapsed_s=30, prior_s_per_group=10) == 510


def test_halfway_the_estimate_blends_the_observed_and_last_runs_pace_equally():
    # 20s/group observed, 10s/group last run -> 15s/group over 10 groups, 100s already spent.
    assert _estimate_seconds_left(done=5, total=10, done_elapsed_s=100, elapsed_s=100, prior_s_per_group=10) == 50


def test_a_complete_scan_saves_its_pace_for_the_next_run(db):
    _save_scan_pace(SimpleNamespace(done=54, total=54, done_elapsed_s=540), _new_totals(54))

    assert _load_scan_pace() == 10


def test_a_scan_cut_short_by_a_checkpoint_does_not_save_its_pace(db):
    totals = _new_totals(54)
    totals["checkpoint_skipped"] = 40

    _save_scan_pace(SimpleNamespace(done=54, total=54, done_elapsed_s=60), totals)

    assert _load_scan_pace() is None


def test_a_yad2_only_run_scans_no_facebook_group():
    assert _scope_group_urls(["https://www.facebook.com/groups/1"], "yad2", None) == []


def test_a_one_group_run_accepts_any_link_shape_of_a_listed_group(db):
    active = ["https://www.facebook.com/groups/123"]

    assert _scope_group_urls(active, "fb", "https://m.facebook.com/groups/123/?ref=share") == active


def test_a_one_group_run_refuses_a_group_not_in_the_groups_tab(db):
    with pytest.raises(ValueError):
        _scope_group_urls(["https://www.facebook.com/groups/123"], "fb", "https://www.facebook.com/groups/999")


class _FakeGuestPage:
    """A logged-out view of a group: Facebook's login form on top. Records every click."""
    url = "https://www.facebook.com/groups/123"

    def __init__(self):
        self.clicks = []

    def goto(self, url, wait_until, timeout):
        pass

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        page = self

        class _Element:
            first = property(lambda element: element)

            def is_visible(self):
                return 'name="email"' in selector

            def click(self, timeout=None):
                page.clicks.append(selector)

        return _Element()


@pytest.fixture
def guest_mode(monkeypatch, db):
    monkeypatch.setattr(fb_scraper, "FACEBOOK_LOGIN", False)
    monkeypatch.setattr(fb_scraper, "GRAPHQL_CAPTURE_ENABLED", False)


def test_guest_mode_marks_a_group_behind_the_login_wall_as_needing_a_login(guest_mode):
    _scan_group_page(_FakeGuestPage(), "https://www.facebook.com/groups/123", "g", None, set(), headless=True)

    assert storage.get_group_overview()["https://www.facebook.com/groups/123"]["status"] == "needs_login"


def test_guest_mode_never_closes_the_login_dialog(guest_mode):
    page = _FakeGuestPage()

    _scan_group_page(page, "https://www.facebook.com/groups/123", "g", None, set(), headless=True)

    assert page.clicks == []


def test_my_groups_keeps_each_group_once_with_its_name():
    links = [["https://www.facebook.com/groups/123/", "דירות להשכרה בגבעתיים\n12 new posts"],
             ["https://www.facebook.com/groups/123/?ref=bookmarks", ""]]

    assert my_groups_from_links(links) == [{"url": "https://www.facebook.com/groups/123",
                                            "name": "דירות להשכרה בגבעתיים", "looks_like_rentals": True}]


def test_my_groups_skips_facebooks_own_group_pages():
    links = [["https://www.facebook.com/groups/feed/", "Your feed"], ["https://www.facebook.com/groups/discover/", "Discover"]]

    assert my_groups_from_links(links) == []


def test_my_groups_lists_rental_groups_first():
    links = [["https://www.facebook.com/groups/1/", "Hiking Israel"], ["https://www.facebook.com/groups/2/", "דירות רמת גן"]]

    assert [g["name"] for g in my_groups_from_links(links)] == ["דירות רמת גן", "Hiking Israel"]
