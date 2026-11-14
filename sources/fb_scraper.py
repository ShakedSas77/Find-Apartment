"""
=== Facebook group scraper ===

run_scraper() is the public entry point: logs in (manual on first run,
persisted via chrome_profile/), scans the Groups tab's active groups (parallel or sequential
per MAX_CONCURRENT_GROUPS), and feeds every candidate post through
core.evaluate.process_candidate_listing. Checkpoint/CAPTCHA handling, dead-
link pruning, article selectors, and the "See more" expansion logic are all
FB-DOM-specific and live here rather than in a shared module.
"""
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

from core import storage
from integrations import telegram_notifier
from config import (
    SCROLL_COUNT, FIRST_SCAN_SCROLL_COUNT, SCROLL_DELAY_MS, SCROLL_STEP_MIN_PX, SCROLL_STEP_MAX_PX,
    MIN_SCROLLS_BEFORE_EARLY_STOP, GROUP_YIELD_SCALING, GROUP_YIELD_MIN_HISTORY, GROUP_YIELD_POOR_RATE,
    GROUP_YIELD_RICH_RATE, GROUP_YIELD_MIN_SCROLLS,
    CONSECUTIVE_OLD_POSTS_TO_STOP, LOGIN_MAX_ATTEMPTS,
    MAX_CONCURRENT_GROUPS, GMAPS_MONTHLY_CAP, MAX_POST_AGE_DAYS,
    PRUNE_DEAD_LINKS_ENABLED, DEAD_LINK_CHECK_INTERVAL_HOURS, TELEGRAM_ENABLED, SHEET_HEADERS, BROWSER_LOCALE, ALERT_RETRY_MAX_AGE_HOURS,
    GRAPHQL_CAPTURE_ENABLED, ALERTS_SCAN_ENABLED, YAD2_TAB_NAME, PROGRESS_TICK_S,
    SEE_MORE_SETTLE_POLL_MS, SEE_MORE_SETTLE_MAX_MS, SEE_MORE_RETRY_DELAY_MS,
)
from sources.browser import _CHROME_LAUNCH_ARGS, _apply_stealth, _normalize_user_agent, _is_visible, _dismiss_popups
from core.util import _safe_print, _checkpoint_lock, _resume_event, _sheet_lock, timed, get_phase_seconds
from core.errors import GmapsQuotaHalted, HeadlessCheckpointAbort, LoginRequired
from core.normalize import (
    BIDI_RE, _strip_comment_section, relative_to_date, _infer_post_date,
    _extract_candidate_cities, _is_recognized_fb_date_text,
)
from core.dedupe import _text_dedup_hash
from core.evaluate import process_candidate_listing, _content_prefilter, flush_pending_listings
from backup_db import backup_if_stale
from integrations.groups_sync import canonical_group_url, sync_groups, TAB_NAME as GROUPS_TAB_NAME
from sources.graphql_capture import GraphqlPostCapture, compare_with_dom
from sources.mail_source import scan_alert_emails
from core import run_log
from integrations.sheets import (
    setup_google_sheet, dedupe_and_sort_sheet, open_listings_tab, _rewrite_sheet_data_rows,
    _fields_from_sheet_row, _SCORE_COL_INDEX,
)

_text_dedup_lock = threading.Lock()

_run_text_hashes: dict[str, str] = {}  # text_hash -> claiming url, this run only (fresh per process)

_headless_checkpoint_hit = False  # set once any group hits a checkpoint in --headless mode; other groups then skip fast

_DEAD_POST_RE = re.compile(r"isn.t available right now", re.IGNORECASE)
_DEAD_LINK_PROGRESS_EVERY = 10
_DEAD_LINKS_CHECKED_KEY = "dead_links_checked_at"
_PROGRESS_BAR_WIDTH = 24

def _is_post_removed(page, url: str, headless: bool, label: str) -> bool:
    """
    Navigates to a post URL and checks for Facebook's generic "This content
    isn't available right now" placeholder — shown when a post was deleted,
    or its visibility was changed to exclude this account. Confirms with one
    reload before concluding it's really gone, so a transient load hiccup
    can't wipe a real listing.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    page.wait_for_timeout(2000)
    _handle_checkpoint_if_present(page, url, label, headless)
    if not _is_visible(page.get_by_text(_DEAD_POST_RE)):
        return False

    # A failed reload is inconclusive, not proof the post is gone — treat it as
    # alive so a transient network error can't delete a real listing's row.
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception:
        return False
    return _is_visible(page.get_by_text(_DEAD_POST_RE))

def _dead_link_check_due(now: datetime) -> bool:
    last = storage.get_bot_state(_DEAD_LINKS_CHECKED_KEY)
    if last and now - datetime.fromisoformat(last) < timedelta(hours=DEAD_LINK_CHECK_INTERVAL_HOURS):
        hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600
        print(f"Dead-link check: skipped — last ran {hours:.1f}h ago (every {DEAD_LINK_CHECK_INTERVAL_HOURS}h).")
        return False
    return True

def _prune_dead_links(page, sheet, headless: bool, live: bool) -> int:
    """
    Beginning-of-run pass: visits every URL already in the sheet and removes
    rows whose post Facebook now shows as unavailable (deleted, or visibility
    changed) — such a link is a dead lead that would otherwise sit in the
    sheet until MAX_POST_AGE_DAYS ages it out on its own. live=False checks
    and reports what would be removed without writing, same dry-run contract
    as the rest of the pipeline. Local DB rows are untouched — same reasoning
    as stale-row pruning in dedupe_and_sort_sheet: the sheet is just the
    "current" view, should_skip() only ever reads verdict/attempts.
    """
    data = sheet.get_all_values()
    if len(data) <= 1:
        return 0
    rows = data[1:]
    urls = [(i, row[0]) for i, row in enumerate(rows) if row and row[0]]
    # Each visit takes ~8s with the pause, so a 60-listing sheet is ~8 minutes with nothing else printed.
    print(f"Dead-link check: visiting {len(urls)} listing(s) already in the sheet "
          f"(~{max(1, round(len(urls) * 8.5 / 60))} min)...")

    dead_indices = []
    for checked, (i, url) in enumerate(urls, 1):
        if _is_post_removed(page, url, headless, "Dead-link check"):
            dead_indices.append(i)
            _safe_print(f"    Dead link found (post no longer available): {url}")
        if checked % _DEAD_LINK_PROGRESS_EVERY == 0 and checked < len(urls):
            print(f"    Dead-link check: {checked}/{len(urls)} checked, {len(dead_indices)} dead so far.")
        time.sleep(random.uniform(2, 5))

    if not dead_indices:
        print("Dead-link check: 0 removed listing(s) found.")
        return 0

    if not live:
        print(f"DRY RUN: would remove {len(dead_indices)} dead listing(s) from the sheet. Rerun without --dry-run to commit.")
        return len(dead_indices)

    keep_rows = [row for i, row in enumerate(rows) if i not in dead_indices]
    total_cols = max(len(SHEET_HEADERS), max((len(r) for r in data), default=0))
    _rewrite_sheet_data_rows(sheet, len(rows), keep_rows, total_cols)
    print(f"Removed {len(dead_indices)} dead listing(s) from the sheet (post no longer available).")
    return len(dead_indices)

# FB serves the same group post under multiple URL shapes — /posts/<id> and
# /permalink/<id> are interchangeable, and www/m/web hosts all resolve to the
# same post. Without canonicalization each shape looks like a new post to the
# dedupe checks (DB primary key + sheet seen_urls) and costs a duplicate LLM call.
_CANONICAL_GROUP_POST_RE = re.compile(
    r'https://(?:www|m|web)\.facebook\.com/groups/([^/?#]+)/(?:posts|permalink)/(\d+)'
)

def _canonical_post_url(url: str) -> str:
    m = _CANONICAL_GROUP_POST_RE.match(url)
    if m:
        # Trailing slash kept — matches the shape already stored in the DB/sheet
        return f"https://www.facebook.com/groups/{m.group(1)}/posts/{m.group(2)}/"
    return url  # unknown shape: pass through unchanged, never break dedupe on odd URLs

# 7-selector fallback chain — FB DOM changes without warning, last resort:
# aria-labelledby+aria-describedby holds even when role="article" is absent
_ARTICLE_SELECTORS = [
    'div[role="article"]',
    'div[aria-labelledby][aria-describedby]',
    'div[aria-posinset]',
    'div[data-ad-preview="message"]',
    'div.x1yztbdb',
    'div[data-pagelet^="GroupFeed"] > div > div',
    'div[role="feed"] > div > div',
]

def _find_articles(page) -> list:
    for sel in _ARTICLE_SELECTORS:
        elements = page.locator(sel).all()
        if len(elements) > 0:
            return elements
    return []

_SEE_MORE_CLICK_JS = """el => {
    const patterns = ['See more', 'קרא עוד', 'ראה עוד'];
    for (const btn of el.querySelectorAll('div[role="button"], span, a')) {
        if (patterns.includes(btn.textContent.trim())) { btn.click(); return true; }
    }
    return false;
}"""

# The post body's own container(s) — FB marks the author's message, so reading it skips the author
# header, "Follow"/timestamp chrome, reaction counts and the comment thread that article.inner_text()
# drags along (and that _strip_comment_section then has to cut back out by pattern). A shared post can
# carry two (the sharer's note + the original), so all are joined. Selectors from MasuRii/FBScrapeIdeas.
_POST_BODY_SELECTORS = (
    'div[data-ad-rendering-role="story_message"]',
    'div[data-ad-preview="message"]',
    'div[data-ad-comet-preview="message"]',
)
_MIN_POST_BODY_CHARS = 20
# A post that shares a Marketplace listing shows the price only on the attached card, outside the
# body container (seen 2026-11-11: rent ₪9,250 on the card, none in the text, and the LLM filled
# in an example price instead). The card is the link to the listing; its text is price · city · title.
_LISTING_CARD_SELECTORS = (
    'a[href*="/commerce/listing/"]',
    'a[href*="/marketplace/item/"]',
)

def _read_post_text(article) -> str:
    for selector in _POST_BODY_SELECTORS:
        try:
            parts = [el.inner_text().strip() for el in article.locator(selector).all()]
        except Exception:
            continue
        body = "\n".join(part for part in parts if part)
        if len(body) > _MIN_POST_BODY_CHARS:
            return "\n".join([body] + _read_listing_cards(article))
    return article.inner_text().strip()

def _read_listing_cards(article) -> list[str]:
    cards = []
    for selector in _LISTING_CARD_SELECTORS:
        try:
            texts = [el.inner_text().strip() for el in article.locator(selector).all()]
        except Exception:
            continue
        # Each photo in the card is its own link with no text; only the caption link carries any.
        cards.extend(t for t in texts if t and t not in cards)
    return cards

def _wait_for_text_growth(article, before_len: int) -> str:
    # Polls instead of always sleeping a fixed time after a "See more" click; local DOM re-reads
    # only, no extra requests to Facebook.
    text = ""
    elapsed = 0
    while elapsed < SEE_MORE_SETTLE_MAX_MS:
        try:
            article.page.wait_for_timeout(SEE_MORE_SETTLE_POLL_MS)
        except Exception:
            break
        elapsed += SEE_MORE_SETTLE_POLL_MS
        try:
            text = _read_post_text(article)
        except Exception:
            break
        if len(text) > before_len:
            return text
    return text

def extract_post_info(article) -> tuple[str, str]:
    """
    Finds the post's own permalink + timestamp among the article's links. On a
    "shared post" layout, a nested/preview link inside the embedded content can
    also have an href matching /posts//permalink//marketplace-item — but its
    visible text is the preview's own content (sometimes a raw URL), not a
    timestamp. Prefer the first candidate whose text actually looks like a real
    FB date; only fall back to the first href match if none do (unchanged
    behavior for the common non-shared-post case, where there's just one).
    """
    try:
        links = article.locator('a[role="link"]').all()
        first_candidate = None
        for link in links:
            href = link.get_attribute("href") or ""
            if any(seg in href for seg in ("/posts/", "/permalink/", "/marketplace/item/")):
                if "comment_id" in href:
                    continue
                clean = href.split("?")[0]
                if clean.startswith("/"):
                    clean = "https://www.facebook.com" + clean
                clean = _canonical_post_url(clean)

                link_text = ""
                try:
                    link_text = link.inner_text().strip()
                except Exception:
                    pass

                if link_text and _is_recognized_fb_date_text(link_text):
                    return clean, relative_to_date(link_text)

                if first_candidate is None:
                    first_candidate = (clean, relative_to_date(link_text) if link_text else "")

        if first_candidate:
            return first_candidate
    except Exception:
        pass
    return "Link not extracted", ""

def _harvest_new_articles(page, harvested: dict, effective_cutoff: datetime) -> list:
    """
    Facebook unmounts posts that scroll far out of view, so reading the feed once
    after the scroll loop only sees the last few screens. Called after every scroll
    instead: records each not-yet-seen post (keyed by URL) while it is still in the
    DOM. "See more" is expanded here, in place, since the element is gone later.
    Posts already older than the cutoff, or already handled by a previous run
    (storage.should_skip), skip the expansion — they get pre-filtered anyway.
    Returns the newly harvested (url, post_dt, is_known) triples in feed order.
    """
    new_posts = []
    for article in _find_articles(page):
        try:
            post_url, fb_post_date = extract_post_info(article)
            if post_url == "Link not extracted" or post_url in harvested:
                continue
            post_dt = _infer_post_date(fb_post_date) if fb_post_date else None
            is_known = storage.should_skip(post_url)
            text = _read_post_text(article)
            if not is_known and (post_dt is None or not _is_before_cutoff(post_dt, effective_cutoff)):
                for attempt in range(2):
                    try:
                        if article.evaluate(_SEE_MORE_CLICK_JS):
                            text = _wait_for_text_growth(article, len(text)) or text
                        break
                    except Exception:
                        if attempt == 0:
                            page.wait_for_timeout(SEE_MORE_RETRY_DELAY_MS)
        except Exception:
            continue
        if len(text) <= 20:
            continue
        harvested[post_url] = {"text": text, "date": fb_post_date, "is_known": is_known}
        new_posts.append((post_url, post_dt, is_known))
    return new_posts

# FB's "Join group" button (English UI is assumed elsewhere too; Hebrew fallback in case the locale changes).
_JOIN_GROUP_RE = re.compile(r"^\s*(?:\+\s*)?(?:Join group|Join Group|הצטרפות לקבוצה|הצטרף לקבוצה)\s*$")
# Shown instead of Join once a request to join is waiting for an admin's approval.
_JOIN_PENDING_RE = re.compile(r"^\s*(?:Cancel request|ביטול בקשה|בטל בקשה)\s*$")

def _blocked_membership(page) -> str | None:
    # "not_member" / "pending" (join requested) when the group shows that button and no posts at all,
    # else None. Posts on the page mean it's readable — a public group shows them to non-members.
    try:
        if page.locator('div[role="article"], div[aria-posinset]').count():
            return None
    except Exception:
        return None
    if _is_visible(page.get_by_role("button", name=_JOIN_GROUP_RE)):
        return "not_member"
    if _is_visible(page.get_by_role("button", name=_JOIN_PENDING_RE)):
        return "pending"
    return None

def _ensure_sorted_by_new_posts(page, group_label: str):
    """
    Group feeds default to "Most relevant" sort (falls back to "Recent
    activity" — comment activity, not post date — when nothing stands out),
    either of which can surface bumped/commented-on posts out of chronological
    order. Switching to "New posts" makes the feed strictly newest-first,
    which is what makes it safe to stop scrolling once old posts start
    appearing (see the scroll loop in _scan_group_page). Verified against live
    FB DOM 2026-07-25: the sort control is a role="button" whose visible text
    is the current selection ("Most relevant"/"Recent activity"/"New posts"),
    and the opened menu's options are role="menuitemradio", not "menuitem".
    Best-effort: any failure just leaves the default sort, and
    CONSECUTIVE_OLD_POSTS_TO_STOP's tolerance covers that case too.
    """
    try:
        sort_button = page.locator(
            '[role="button"]:has-text("Most relevant"), '
            '[role="button"]:has-text("Recent activity"), '
            '[role="button"]:has-text("New posts")'
        ).first
        if not sort_button.is_visible(timeout=1500):
            return
        if "New posts" in sort_button.inner_text():
            return
        sort_button.click(timeout=1500)
        page.locator('[role="menuitemradio"]:has-text("New posts")').first.click(timeout=1500)
        page.wait_for_timeout(1500)
        _safe_print(f"[{group_label}] Switched sort to New posts.")
    except Exception:
        pass

def _handle_checkpoint_if_present(page, target_url: str, group_label: str, headless: bool):
    """
    If Facebook asks for login/2FA/CAPTCHA — stops every group, not just the
    current one. In headless mode there's no visible window to solve it in
    manually, so a screenshot is saved, every other group is flagged to give up
    instead of each one hitting the same wall itself, and an exception is
    raised that signals _scan_group to stop this group cleanly.
    """
    global _headless_checkpoint_hit
    while (_is_visible(page.locator('input[name="email"]')) or
           _is_visible(page.locator('input[name="pass"]')) or
           "checkpoint" in page.url or
           _is_visible(page.locator('iframe[title*="recaptcha"]')) or
           _is_visible(page.get_by_text("I'm not a robot"))):
        if headless:
            safe_label = group_label.replace(' ', '_').replace('/', '-')
            screenshot_path = f"checkpoint_{safe_label}.png"
            try:
                page.screenshot(path=screenshot_path)
            except Exception:
                pass
            with _checkpoint_lock:
                already_flagged = _headless_checkpoint_hit
                _headless_checkpoint_hit = True
            if not already_flagged:
                _safe_print(f"\n[{group_label}] Facebook is asking for a password, 2FA, or CAPTCHA security check. "
                            f"Cannot solve this in --headless mode. Screenshot saved to {screenshot_path}. "
                            f"Rerun with --headful to resolve it manually.")
            raise HeadlessCheckpointAbort()

        is_leader = False
        with _checkpoint_lock:
            if _resume_event.is_set():
                _resume_event.clear()
                is_leader = True
        if is_leader:
            try:
                page.bring_to_front()
            except Exception:
                pass
            _safe_print(f"\n[{group_label}] Facebook is asking for a password, 2FA, or CAPTCHA security check. Pausing ALL groups.")
            input("  -> Please complete it in the browser tab that was brought to front, then press ENTER here to resume all groups... ")
            _resume_event.set()
        else:
            _resume_event.wait()
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception:
            pass

def _is_before_cutoff(post_dt: datetime, cutoff: datetime) -> bool:
    # Post dates are day-only (DD/MM -> midnight) while the cutoff is usually the exact time of the
    # group's last scan, so a datetime compare made every post from the cutoff's own day look older
    # (posted 11:00, last scan 09:00 -> "00:00 < 09:00"). Found 2026-11-07: 1,648 of 2,322 "older
    # than the scan cutoff" rejects were posted the same day they were seen.
    return post_dt.date() < cutoff.date()

def _update_old_streak(current: int, is_known: bool, post_dt: datetime | None, cutoff: datetime) -> int:
    """
    Next value of the consecutive-old-posts streak that triggers the scroll early-stop.
    A post a previous run already handled counts like an old one: a group with no saved
    baseline can't reach the MAX_POST_AGE_DAYS cutoff in max_scrolls, so without this it
    re-crawls the same range every run and never catches up. A fresh post resets the
    streak; an undated, unknown one (comment/unparseable) is inconclusive and leaves it.
    """
    if is_known or (post_dt is not None and _is_before_cutoff(post_dt, cutoff)):
        return current + 1
    if post_dt is not None:
        return 0
    return current

def _yield_scaled_scroll_cap(added: int, total: int, full_cap: int) -> int:
    if total < GROUP_YIELD_MIN_HISTORY:
        return full_cap
    rate = added / total
    if rate >= GROUP_YIELD_RICH_RATE:
        return full_cap
    if rate <= GROUP_YIELD_POOR_RATE:
        return GROUP_YIELD_MIN_SCROLLS
    fraction = (rate - GROUP_YIELD_POOR_RATE) / (GROUP_YIELD_RICH_RATE - GROUP_YIELD_POOR_RATE)
    return round(GROUP_YIELD_MIN_SCROLLS + fraction * (full_cap - GROUP_YIELD_MIN_SCROLLS))

def _scroll_and_harvest(page, group_label: str, effective_cutoff: datetime, max_scrolls: int) -> tuple[dict, bool]:
    """
    Scrolls the group feed, harvesting posts after every scroll (Facebook unmounts
    posts that leave the viewport), until the early-stop streak is reached or
    max_scrolls runs out. Returns (harvested posts by url, caught_up) — caught_up is
    True only when the early-stop fired, i.e. the scan provably reached content an
    earlier run already covered.
    """
    harvested = {}
    consecutive_old = 0
    scrolls_done = 0
    caught_up = False
    _safe_print(f"[{group_label}] Scrolling (up to {max_scrolls} times, stops early once past {effective_cutoff:%d/%m %H:%M})...")
    for i in range(max_scrolls):
        scrolls_done = i + 1
        # Jitter the scroll distance too, not just the delay — a perfectly fixed pace and size reads as more bot-like
        # Occasional cursor drift: a human's mouse doesn't sit frozen while reading a feed
        if random.random() < 0.25:
            try:
                page.mouse.move(random.randint(200, 1100), random.randint(200, 800), steps=random.randint(5, 15))
            except Exception:
                pass
        page.mouse.wheel(0, random.randint(SCROLL_STEP_MIN_PX, SCROLL_STEP_MAX_PX))
        jittered_delay = max(500, SCROLL_DELAY_MS + random.randint(-400, 400))
        page.wait_for_timeout(jittered_delay)

        try:
            new_posts = _harvest_new_articles(page, harvested, effective_cutoff)
        except Exception:
            new_posts = []

        for _, post_dt, is_known in new_posts:
            consecutive_old = _update_old_streak(consecutive_old, is_known, post_dt, effective_cutoff)

        if scrolls_done >= MIN_SCROLLS_BEFORE_EARLY_STOP and consecutive_old >= CONSECUTIVE_OLD_POSTS_TO_STOP:
            caught_up = True
            if scrolls_done < max_scrolls:
                _safe_print(f"[{group_label}] Stopping early after {scrolls_done} scroll(s) — {consecutive_old} consecutive old/already-processed post(s) found.")
            break
    _safe_print(f"[{group_label}] Done scrolling ({scrolls_done} scroll(s)).")
    if not caught_up:
        _safe_print(f"[{group_label}] Did not catch up to the last scan within {max_scrolls} scrolls — "
                     f"last_scan_time not advanced, will retry this range next run.")
    return harvested, caught_up

def _collect_valid_posts(harvested: dict, target_url: str, group_label: str, seen_urls, effective_cutoff: datetime, stats: dict) -> list:
    """
    Runs every harvested post through the cheap, no-LLM gates (cached verdict, already in the
    sheet, older than the scan cutoff, content pre-filters, text-dedup) and records a verdict
    for each one it rejects. Returns the posts worth an LLM call.
    """
    valid_posts = []
    # The three "already handled" gates fire for most harvested posts on every run; one count line
    # per group instead of a line per post keeps the log readable (they used to bury the real errors).
    skipped = {"already processed": 0, "already in sheet": 0, "older than cutoff": 0}
    for post_url, item in harvested.items():
        fb_post_date = item["date"]
        text = BIDI_RE.sub('', item["text"])
        text = _strip_comment_section(text)

        if item["is_known"]:
            skipped["already processed"] += 1
            continue

        with _sheet_lock:
            already_seen = post_url in seen_urls
        if already_seen:
            skipped["already in sheet"] += 1
            continue

        # Same effective_cutoff the scroll loop used to decide when to stop — a post
        # this old either predates MAX_POST_AGE_DAYS or predates the group's last scan
        # (already handled last run), so there's no point sending it to the LLM here.
        post_dt = _infer_post_date(fb_post_date) if fb_post_date else None
        if post_dt is not None and _is_before_cutoff(post_dt, effective_cutoff):
            skipped["older than cutoff"] += 1
            storage.record_post(
                post_url,
                target_url,
                text,
                storage.VERDICT_PREFILTERED,
                analysis={
                    "post_date": fb_post_date,
                    "reject_reason": "older_than_scan_cutoff",
                },
            )
            stats["prefiltered"] += 1
            continue

        rejection = _content_prefilter(text, post_url)
        if rejection:
            verdict, analysis, message = rejection
            _safe_print(f"    [{group_label}] Pre-filtered: {message}")
            storage.record_post(post_url, target_url, text, verdict, analysis=analysis)
            stats["prefiltered"] += 1
            continue

        # --- Pre-filter: same listing text already seen (this run or a past one) ---
        # Crossposts to multiple groups are common and otherwise reach the LLM
        # once per group even though the text is identical — this catches that
        # before spending a call. Claiming the hash happens under the lock so
        # concurrent group threads scanning the same crosspost at once still
        # only let the first one through.
        text_hash = _text_dedup_hash(text)
        dup_of_url = None
        if text_hash:
            with _text_dedup_lock:
                dup_of_url = _run_text_hashes.get(text_hash)
                if dup_of_url is None:
                    _run_text_hashes[text_hash] = post_url
            if dup_of_url is None:
                existing = storage.find_by_text_hash(text_hash, exclude_url=post_url)
                if existing:
                    dup_of_url = existing["url"]
        if dup_of_url:
            _safe_print(f"    [{group_label}] Pre-filtered: duplicate text of an already-processed post ({dup_of_url}).")
            storage.record_post(post_url, target_url, text, storage.VERDICT_DUPLICATE_TEXT,
                                 analysis={"text_hash": text_hash, "reject_reason": f"duplicate_text_of:{dup_of_url}"})
            stats["prefiltered"] += 1
            continue

        valid_posts.append({"url": post_url, "text": text, "post_date": fb_post_date})
    if any(skipped.values()):
        _safe_print(f"    [{group_label}] Skipped without analysis: " + ", ".join(f"{n} {why}" for why, n in skipped.items() if n)
                    + f" (cutoff {effective_cutoff:%d/%m %H:%M}).")
    return valid_posts

def _scan_group_page(page, target_url: str, group_label: str, sheet, seen_urls, headless: bool, live: bool = False) -> dict:
    """
    The actual scan logic for a single group, run on a page that already
    exists. Shared between parallel mode (_scan_group, which creates a
    dedicated browser/context per thread) and sequential mode (run_scraper
    when MAX_CONCURRENT_GROUPS == 1, which runs every group in sequence on the
    same page inside the original persistent context, to preserve the
    profile's real fingerprint).

    live=False (default, dry-run): classifies posts normally (LLM calls,
    Maps/geocoding calls, and their quota tracking all still happen — the
    read-side of the pipeline is identical either way) but a would-be match
    is never written to the sheet and never recorded as VERDICT_ADDED, so a
    dry run can never block a real future match via should_skip's cache.
    Non-match verdicts (prefiltered/rejected/parse_failed) are still recorded
    regardless of live, since that's just avoiding repeat LLM cost on posts
    already known not to match, not a "commit" of a result.

    Returns a dict summarizing the run: added, checkpoint_hit (True when the
    group was skipped/stopped due to a checkpoint that can't be solved in
    headless mode), posts_seen, prefiltered, llm_parsed.
    """
    stats = {"added": 0, "checkpoint_hit": False, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}
    pending_rows = []
    pending_seen_urls = set()
    pending_records = []
    graphql = GraphqlPostCapture(page) if GRAPHQL_CAPTURE_ENABLED else None
    try:
        _safe_print(f"\n{'='*50}\nScanning {group_label}\n{target_url}\n{'='*50}")

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            _safe_print(f"    WARNING: [{group_label}] Navigation interrupted by Facebook. Checking for security checkpoints...")

        page.wait_for_timeout(3000)
        _handle_checkpoint_if_present(page, target_url, group_label, headless)
        _dismiss_popups(page)

        # Captures the group's real display name so a no-city address can later be
        # resolved to a candidate city (see _resolve_ambiguous_city) — page.title()
        # is far more stable across FB DOM changes than a CSS selector, and costs
        # nothing extra since the page is already loaded.
        try:
            group_title = page.title()
            if group_title:
                storage.save_group_city_hint(target_url, group_title, _extract_candidate_cities(group_title))
        except Exception:
            pass

        # A private group the account hasn't joined shows a Join button and no posts, and used to be
        # found only after FIRST_SCAN_SCROLL_COUNT scrolls of an empty page (~1.5 min each, 5 groups
        # on 2026-11-12). A public group shows its posts to non-members and is scanned as usual.
        membership = _blocked_membership(page)
        if membership:
            storage.set_group_status(target_url, membership)
            if membership == "pending":
                _safe_print(f"    [{group_label}] join request still waiting for approval — skipped.")
            else:
                _safe_print(f"    WARNING: [{group_label}] not a member of this group — join it on Facebook so its posts can be read.")
            return stats

        _ensure_sorted_by_new_posts(page, group_label)

        scan_started_at = datetime.now()
        last_scan_time = storage.get_last_scan_time(target_url)
        age_cutoff = datetime.now() - timedelta(days=MAX_POST_AGE_DAYS)
        effective_cutoff = max(age_cutoff, last_scan_time) if last_scan_time else age_cutoff

        max_scrolls = SCROLL_COUNT if last_scan_time else FIRST_SCAN_SCROLL_COUNT
        if last_scan_time and GROUP_YIELD_SCALING:
            verdicts = storage.get_group_stats().get(target_url, {})
            max_scrolls = _yield_scaled_scroll_cap(verdicts.get(storage.VERDICT_ADDED, 0), sum(verdicts.values()), max_scrolls)

        with timed("scroll"):
            harvested, caught_up = _scroll_and_harvest(page, group_label, effective_cutoff, max_scrolls)

        _safe_print(f"[{group_label}] Found {len(harvested)} real posts.")
        stats["posts_seen"] = len(harvested)
        if graphql is not None:
            _report_graphql_capture(graphql, harvested, group_label)

        # Shown in the Groups tab's status column, so a newly added group the account hasn't joined yet
        # is visible there instead of silently yielding nothing run after run.
        if harvested:
            storage.set_group_status(target_url, "ok")
        elif _is_visible(page.get_by_role("button", name=_JOIN_GROUP_RE)):
            storage.set_group_status(target_url, "not_member")
            _safe_print(f"    WARNING: [{group_label}] not a member of this group — join it on Facebook so its posts can be read.")
        else:
            storage.set_group_status(target_url, "no_posts")

        if len(harvested) == 0:
            _safe_print(f"    [{group_label}] No posts detected. Saving debug screenshot...")
            try:
                page.screenshot(path=f"debug_fb_{group_label.replace(' ', '_').replace('/', '-')}.png")
            except Exception as e:
                _safe_print(f"    WARNING: [{group_label}] debug screenshot failed: {e}")
            return stats

        valid_posts = _collect_valid_posts(harvested, target_url, group_label, seen_urls, effective_cutoff, stats)

        _safe_print(f"[{group_label}] Found {len(valid_posts)} valid posts for LLM parsing.")

        added = 0
        pending_records = []
        pending_rows = []
        pending_seen_urls = set()
        pending_telegram = []
        local_lock = threading.Lock()

        def process_post(post):
            process_candidate_listing(post["url"], post["text"], post["post_date"], target_url, group_label, live,
                                       seen_urls, pending_rows, pending_seen_urls, pending_records, stats, local_lock,
                                       pending_telegram)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_post, post) for post in valid_posts]
            for future in as_completed(futures):
                future.result()

        flush_pending_listings(sheet, seen_urls, pending_rows, pending_seen_urls, pending_records,
                               pending_telegram, stats, live, group_label)

        # A dry run commits nothing, so it must not move the baseline either: a would-be match it
        # printed but never recorded would then read as older than last_scan_time on the next live run.
        if caught_up and live:
            storage.set_last_scan_time(target_url, scan_started_at)
    except GmapsQuotaHalted:
        _safe_print(f"    [{group_label}] Stopping: Google Maps monthly cap reached (GMAPS_ON_CAP='halt').")
    except HeadlessCheckpointAbort:
        stats["checkpoint_hit"] = True
    finally:
        if graphql is not None:
            graphql.detach()
    return stats

def _report_graphql_capture(graphql: GraphqlPostCapture, harvested: dict, group_label: str):
    # Observational only: the evidence for trusting the capture before any post data is taken
    # from it. Never affects the scan.
    try:
        s = compare_with_dom(harvested, graphql.collect(), _infer_post_date)
    except Exception as e:
        _safe_print(f"    [{group_label}] GraphQL capture: comparison failed ({e}).")
        return
    _safe_print(f"    [{group_label}] GraphQL capture: {s['captured']} post(s) captured, {s['matched']}/{s['dom_posts']} "
                f"DOM posts matched ({s['with_text']} with text), dates agree on {s['dates_agree']}/{s['dated']}.")

def _scan_group(target_url: str, group_label: str, sheet, seen_urls, storage_state_path: str, headless: bool,
                user_agent: str = "", browser_locale: str = "", live: bool = False) -> dict:
    """
    Parallel mode: each thread runs its own Playwright instance (the sync API
    isn't thread-safe when sharing one browser/context between threads) — the
    login is shared via a storage_state exported once from the main profile,
    not by sharing the context object. The actual per-group scan logic is
    shared with sequential mode via _scan_group_page.
    """
    # Random opening-time spread between tabs — looks less bot-like than opening them all simultaneously
    time.sleep(random.uniform(0.5, 3.0))

    if headless:
        with _checkpoint_lock:
            already_hit = _headless_checkpoint_hit
        if already_hit:
            _safe_print(f"    [{group_label}] Skipping: a security checkpoint was already hit in another group (headless mode). Rerun with --headful.")
            return {"added": 0, "checkpoint_hit": True, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=_CHROME_LAUNCH_ARGS
        )
        # Match the real profile's fingerprint: a worker with Playwright's default
        # UA/locale next to the logged-in profile's real ones = same account on two
        # different "devices" simultaneously, a stronger bot signal than either alone.
        context_kwargs = {"storage_state": storage_state_path, "viewport": {"width": 1366, "height": 1600}}
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        if browser_locale:
            context_kwargs["locale"] = browser_locale
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        _apply_stealth(page)
        try:
            return _scan_group_page(page, target_url, group_label, sheet, seen_urls, headless, live)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

def _print_gmaps_quota_status():
    usage = storage.get_gmaps_usage()
    remaining = max(GMAPS_MONTHLY_CAP - usage, 0)
    print(f"Google Maps quota this month: {usage}/{GMAPS_MONTHLY_CAP} used, {remaining} remaining.")
    if GMAPS_MONTHLY_CAP > 0 and usage >= GMAPS_MONTHLY_CAP * 0.8:
        print(f"WARNING: Google Maps usage is at {usage}/{GMAPS_MONTHLY_CAP} ({usage / GMAPS_MONTHLY_CAP:.0%}) of the monthly cap.")

def _make_group_label(index: int, total: int, url: str) -> str:
    # Order is shuffled every run, so "Group 5/33" alone can't be traced back to the Groups tab.
    group_id = url.rstrip("/").rsplit("/", 1)[-1]
    return f"Group {index}/{total} {group_id}"

def _resend_owed_alerts(sheet) -> int:
    # The batch flush records VERDICT_ADDED before sending, so a failed send used to lose the alert
    # for good (the post is never rescanned). A listing no longer in the sheet (merged cross-post,
    # pruned, voted out) is skipped, not alerted.
    owed = storage.get_owed_alert_urls(ALERT_RETRY_MAX_AGE_HOURS)
    if not owed:
        return 0
    tabs = [sheet]
    if ALERTS_SCAN_ENABLED:
        tabs.append(open_listings_tab(sheet.spreadsheet, YAD2_TAB_NAME))
    rows_by_url = {row[0]: row for tab in tabs for row in tab.get_all_values()[1:] if row and row[0]}
    sent = 0
    for url in owed:
        row = rows_by_url.get(url)
        if row is None:
            continue
        score_cell = row[_SCORE_COL_INDEX].strip() if len(row) > _SCORE_COL_INDEX else ""
        try:
            score = int(score_cell)
        except ValueError:
            score = 0
        try:
            if telegram_notifier.send_listing_alert(url, _fields_from_sheet_row(row), score):
                sent += 1
        except Exception as e:
            print(f"WARNING: re-sending owed Telegram alert failed for {url}: {e}")
    print(f"Re-sent {sent}/{len(owed)} owed Telegram alert(s) from earlier runs.")
    return sent

def run_scraper(headless: bool = False, live: bool = False, source: str | None = None, group: str | None = None):
    """
    One scrape: pick the active groups from the sheet's Groups tab, log in, scan every group
    (sequentially in the real profile, or in parallel worker browsers), then do the end-of-run
    upkeep. source "fb" skips the Yad2 alert emails, "yad2" reads only them (no browser); group
    scans just that one group from the tab. Returns the run's totals; apartment_bot.py turns them
    into the exit code. Raises LoginRequired when Facebook's session is gone and can't be restored.
    """
    if not live:
        print("DRY RUN: classifying and printing only — no sheet writes, no dedupe/prune. Rerun without --dry-run to commit.")
    run_started = time.perf_counter()
    run_started_at = datetime.now()
    sheet, seen_urls = setup_google_sheet()
    _print_gmaps_quota_status()

    # The sheet's Groups tab decides which groups to scan (groups_sync.py); a dry run only reads it.
    group_urls = _scope_group_urls(sync_groups(sheet.spreadsheet, write=live), source, group)
    print(f"Groups to scan: {len(group_urls)}.")
    is_full_scan = source is None and group is None
    scope = _scope_label(source, group)
    shuffled_urls = random.sample(group_urls, len(group_urls))
    totals = _new_totals(len(shuffled_urls))
    run_message = None  # (message_id, info) of the run-started message, which the progress bar edits
    if live and TELEGRAM_ENABLED:
        try:
            info = _run_started_info(group_urls, run_started_at, alerts=ALERTS_SCAN_ENABLED and source != "fb",
                                     scope=scope)
            message_id = telegram_notifier.send_run_started(info)
            run_message = (message_id, info) if message_id else None
        except Exception as e:
            print(f"WARNING: Telegram run-started alert failed: {e}")
        _resend_owed_alerts(sheet)
    if ALERTS_SCAN_ENABLED and source != "fb":
        _scan_alert_emails(sheet, live, totals)
    if group_urls:
        _scan_facebook_groups(shuffled_urls, sheet, seen_urls, headless, live, totals, run_message,
                              save_pace=is_full_scan, check_dead_links=is_full_scan)

    gmaps_calls_this_month = storage.get_gmaps_usage()
    _print_run_summary(totals, gmaps_calls_this_month)
    if live:
        _end_of_run_upkeep(sheet)
    else:
        print("DRY RUN: skipping sheet dedupe/sort and DB pruning (rerun without --dry-run to commit).")
    if live and TELEGRAM_ENABLED:
        _notify_run_finished(totals, gmaps_calls_this_month, group_urls, run_started_at,
                             time.perf_counter() - run_started, scope)

    phases = ", ".join(f"{name} {seconds:.0f}s" for name, seconds in sorted(get_phase_seconds().items()))
    print(f"Timing: {time.perf_counter() - run_started:.0f}s wall; cumulative across group threads: {phases or 'none'}.")
    return totals

def _scope_group_urls(group_urls: list[str], source: str | None, group: str | None) -> list[str]:
    """The groups a run scans: all active ones, none for a Yad2-only run, or just `group` (any link
    shape; it must be a row of the Groups tab, paused rows included)."""
    if source == "yad2":
        return []
    if group is None:
        return group_urls
    wanted = canonical_group_url(group)
    if wanted not in set(group_urls) | {g["url"] for g in storage.get_groups()}:
        raise ValueError(f"{group} is not in the '{GROUPS_TAB_NAME}' tab")
    return [wanted]

def _scope_label(source: str | None, group: str | None) -> str | None:
    """How the run messages name a partial scan; None for a full one."""
    if group:
        url = canonical_group_url(group)
        name = next((g["name"] for g in storage.get_groups() if g["url"] == url and g["name"]), "")
        return f"קבוצה אחת: {name or url}"
    return {"fb": "פייסבוק בלבד", "yad2": "התראות יד2 בלבד"}.get(source)

def _scan_facebook_groups(shuffled_urls: list[str], sheet, seen_urls, headless: bool, live: bool, totals: dict,
                          run_message, save_pace: bool, check_dead_links: bool):
    # The pace and the daily dead-link check belong to full scans: one group's pace isn't the
    # whole scan's, and a quick one-group scan shouldn't spend minutes checking every listing link.
    profile_dir = os.path.join(os.getcwd(), "chrome_profile")
    storage_state_path = os.path.join(profile_dir, "_session_state.json")
    sequential_mode = MAX_CONCURRENT_GROUPS == 1

    # --- Login phase: single persistent-context tab, sequential ---
    with sync_playwright() as p:
        context = _launch_login_context(p, profile_dir, headless)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _apply_stealth(page)
            _ensure_logged_in(page, headless)

            if check_dead_links and PRUNE_DEAD_LINKS_ENABLED and _dead_link_check_due(datetime.now()):
                _prune_dead_links(page, sheet, headless, live)
                if live:
                    storage.set_bot_state(_DEAD_LINKS_CHECKED_KEY, datetime.now().isoformat(timespec="seconds"))

            if sequential_mode:
                progress = _ScanProgress(len(shuffled_urls), totals, run_message, _load_scan_pace())
                try:
                    _scan_groups_sequentially(page, shuffled_urls, sheet, seen_urls, headless, live, totals,
                                              progress=progress)
                    if save_pace:
                        _save_scan_pace(progress, totals)
                finally:
                    progress.stop()
            else:
                real_user_agent, real_locale = _export_session_for_workers(page, context, storage_state_path)
        finally:
            # Closed on every path — a failed storage_state() export, a lost login, or Ctrl+C mid-scan
            # used to leave the login context to the sync_playwright teardown.
            _close_context(context)

    if not sequential_mode:
        progress = _ScanProgress(len(shuffled_urls), totals, run_message, _load_scan_pace())
        try:
            _scan_groups_in_parallel(shuffled_urls, sheet, seen_urls, storage_state_path, headless,
                                     real_user_agent, real_locale, live, totals, progress=progress)
            if save_pace:
                _save_scan_pace(progress, totals)
        finally:
            progress.stop()

def _new_totals(total_groups: int) -> dict:
    # The one definition of the run-totals shape: run_scraper's return value, which
    # apartment_bot.py records via storage.finish_run and turns into the exit code.
    return {"groups_scanned": 0, "total": total_groups, "checkpoint_skipped": 0,
            "total_added": 0, "posts_seen": 0, "prefiltered": 0, "llm_parsed": 0}

def _scan_alert_emails(sheet, live: bool, totals: dict):
    # Runs before the browser opens, and a mailbox problem (password revoked, Gmail down) is only a
    # warning: the Facebook scan must never depend on the email source.
    print("Reading Yad2 alert emails...")
    try:
        stats = scan_alert_emails(sheet.spreadsheet, live)
    except Exception as e:
        print(f"WARNING: Yad2 alert emails skipped: {e}")
        return
    totals["posts_seen"] += stats["posts_seen"]
    totals["prefiltered"] += stats["prefiltered"]
    totals["total_added"] += stats["added"]
    print(f"Yad2 alert emails: {stats['posts_seen']} listing(s) read, {stats['added']} added.")

def _launch_login_context(p, profile_dir: str, headless: bool):
    persistent_kwargs = {"locale": BROWSER_LOCALE}
    if headless:
        # Headless Chromium's UA says "HeadlessChrome" in both the HTTP header and
        # JS — the most obvious headless tell, and a UA can only be set at context
        # creation. Probe a throwaway browser for the real platform-correct UA and
        # strip the "Headless" marker.
        try:
            probe = p.chromium.launch(headless=True)
            try:
                probe_ua = probe.new_page().evaluate("navigator.userAgent") or ""
            finally:
                probe.close()
            if "HeadlessChrome" in probe_ua:
                persistent_kwargs["user_agent"] = _normalize_user_agent(probe_ua)
        except Exception:
            pass
    return p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=headless,
        viewport={"width": 1366, "height": 1600},
        ignore_default_args=["--no-sandbox", "--enable-automation"],
        args=_CHROME_LAUNCH_ARGS,
        **persistent_kwargs
    )

def _ensure_logged_in(page, headless: bool):
    print("Opening Facebook...")
    page.goto("https://www.facebook.com", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    if not _is_visible(page.locator('input[name="email"]')):
        if not headless:
            print("Already logged into Facebook. Skipping manual login.")
        return
    if headless:
        raise LoginRequired("Cannot login manually in headless mode. Please run with --headful first.")

    print("\nPlease log in to Facebook manually in the browser window that just opened.\n")
    for _ in range(LOGIN_MAX_ATTEMPTS):
        input("  -> Press ENTER here in the terminal ONLY AFTER you have fully logged in and see your feed... ")
        page.wait_for_timeout(2000)
        if not _is_visible(page.locator('input[name="email"]')):
            return
        print("ERROR: Facebook login form is still visible. Please complete login first.")
    raise LoginRequired("Login not completed after several attempts.")

def _scan_groups_sequentially(page, urls: list[str], sheet, seen_urls, headless: bool, live: bool, totals: dict,
                              scan_group=None, progress: "_ScanProgress | None" = None):
    # No storage_state at all: every group is scanned on the same page inside the original
    # context, to preserve the profile's real fingerprint. scan_group defaults to
    # _scan_group_page (the browser-driving step); tests pass a stand-in.
    scan_group = scan_group or _scan_group_page
    print("Sequential mode (MAX_CONCURRENT_GROUPS=1): scanning all groups one at a time in the original "
          "browser profile — safest against checkpoints, slowest. No session-state file is written.")
    total = len(urls)
    for i, url in enumerate(urls, 1):
        if i > 1:
            time.sleep(random.uniform(5, 15))
        label = _make_group_label(i, total, url)
        try:
            stats = scan_group(page, url, label, sheet, seen_urls, headless, live)
        except Exception as e:
            _safe_print(f"\nERROR: [{label}] crashed: {e}")
            if progress:
                progress.group_done()
            continue
        _tally(totals, stats)
        if progress:
            progress.group_done()
        if stats["checkpoint_hit"]:
            # _tally already counted the group that hit the checkpoint; a headless checkpoint ends
            # the whole sequential scan, so the groups never reached are skipped too.
            remaining = total - i
            totals["checkpoint_skipped"] += remaining
            _safe_print(f"Stopping sequential scan: {remaining} remaining group(s) also skipped due to the checkpoint.")
            break

def _export_session_for_workers(page, context, storage_state_path: str) -> tuple[str, str]:
    # Parallel workers must present the logged-in profile's own fingerprint (see _scan_group);
    # "HeadlessChrome" is stripped so a headless run's workers still claim the normal Chrome UA.
    # Cookies/session go to a file because one context can't be shared between threads
    # (Playwright's sync API isn't thread-safe).
    real_user_agent = ""
    real_locale = ""
    try:
        real_user_agent = _normalize_user_agent(page.evaluate("navigator.userAgent") or "")
        real_locale = page.evaluate("navigator.language") or ""
    except Exception:
        pass
    context.storage_state(path=storage_state_path)
    return real_user_agent, real_locale

def _scan_groups_in_parallel(urls: list[str], sheet, seen_urls, storage_state_path: str, headless: bool,
                             real_user_agent: str, real_locale: str, live: bool, totals: dict,
                             progress: "_ScanProgress | None" = None):
    print(f"Continuing to scan groups ({MAX_CONCURRENT_GROUPS} in parallel — this raises checkpoint/ban risk "
          f"vs. sequential mode; set MAX_CONCURRENT_GROUPS=1 in config.py if you start seeing checkpoints)...")
    labels = {url: _make_group_label(idx, len(urls), url) for idx, url in enumerate(urls, 1)}
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GROUPS) as executor:
        futures = {
            executor.submit(_scan_group, url, label, sheet, seen_urls, storage_state_path, headless,
                            real_user_agent, real_locale, live): label
            for url, label in labels.items()
        }
        for future in as_completed(futures):
            try:
                _tally(totals, future.result())
            except Exception as e:
                _safe_print(f"\nERROR: [{futures[future]}] crashed: {e}")
            if progress:
                progress.group_done()

class _ScanProgress:
    # Group-scan progress: a console bar line and, on a live run with Telegram, a bar edited into
    # the run-started message. Reported when a group finishes and every PROGRESS_TICK_S in between
    # (a background ticker), so the time-left estimate keeps counting down while long groups are
    # still scrolling. A report identical to the previous one is skipped — Telegram rejects an
    # edit that changes nothing, and the console shouldn't repeat itself.

    def __init__(self, total: int, totals: dict, run_message: tuple[str, dict] | None = None,
                 prior_s_per_group: float | None = None):
        self.total = total
        self.totals = totals
        self.run_message = run_message
        self.prior_s_per_group = prior_s_per_group
        self.started = time.perf_counter()
        self.done = 0
        self.done_elapsed_s = 0.0
        self._lock = threading.Lock()
        self._last_console = None
        self._last_telegram = None
        self._stopped = threading.Event()
        threading.Thread(target=self._tick, daemon=True).start()

    def group_done(self):
        with self._lock:
            self.done += 1
            self.done_elapsed_s = time.perf_counter() - self.started
        self._report()

    def stop(self):
        self._stopped.set()

    def _tick(self):
        while not self._stopped.wait(PROGRESS_TICK_S):
            self._report()

    def _report(self):
        with self._lock:
            elapsed_s = time.perf_counter() - self.started
            left_s = _estimate_seconds_left(self.done, self.total, self.done_elapsed_s, elapsed_s,
                                            self.prior_s_per_group)
            added = self.totals["total_added"]
            line = _progress_line(self.done, self.total, added, elapsed_s, left_s)
            if line != self._last_console:
                self._last_console = line
                _safe_print(line)
            progress_text = telegram_notifier.format_run_progress(self.done, self.total, added, left_s)
            finished = self.done >= self.total
            # Every tick, changed or not: /status (telegram_commands) trusts only a fresh entry.
            storage.set_bot_state(storage.SCAN_PROGRESS_KEY, json.dumps(
                {"text": progress_text, "updated": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False))
            if not self.run_message or progress_text == self._last_telegram:
                return
            self._last_telegram = progress_text
        message_id, info = self.run_message
        try:
            telegram_notifier.update_run_progress(message_id, info, progress_text, finished)
        except Exception as e:
            _safe_print(f"WARNING: Telegram progress update failed: {e}")

def _estimate_seconds_left(done: int, total: int, done_elapsed_s: float, elapsed_s: float,
                           prior_s_per_group: float | None = None) -> float | None:
    # Projects the whole scan from a time per group, then counts down against the clock: between
    # finished groups the estimate keeps shrinking, and each finished group corrects it. The groups
    # that finish first are the quick ones (caught up, or skipped), so the observed pace alone reads
    # far too fast early on (~3 min left at 13/54 of a 9-min scan). The previous run's pace anchors
    # it instead, and the observed pace takes over in proportion to the groups done.
    observed = done_elapsed_s / done if done else None
    if prior_s_per_group is None:
        per_group = observed
    elif observed is None:
        per_group = prior_s_per_group
    else:
        weight = done / total
        per_group = weight * observed + (1 - weight) * prior_s_per_group
    if per_group is None:
        return None
    return max(0.0, per_group * total - elapsed_s)

def _load_scan_pace() -> float | None:
    value = storage.get_bot_state(_scan_pace_key())
    return float(value) if value else None

def _save_scan_pace(progress: "_ScanProgress", totals: dict):
    # Only a complete, checkpoint-free scan is a fair pace: groups aborted by a checkpoint finish
    # in seconds and would make the next run's estimate far too short.
    if progress.done < progress.total or totals["checkpoint_skipped"] or not progress.done:
        return
    storage.set_bot_state(_scan_pace_key(), f"{progress.done_elapsed_s / progress.done:.1f}")

def _scan_pace_key() -> str:
    # Keyed by concurrency: a sequential scan's pace per group is several times a parallel one's.
    return f"scan_seconds_per_group_x{MAX_CONCURRENT_GROUPS}"

def _progress_line(done: int, total: int, added: int, elapsed_s: float, left_s: float | None) -> str:
    # A line per report, not a bar redrawn in place: six groups print at once in parallel mode and
    # the console is teed to the log file, so a \r-redrawn bar would garble both.
    filled = round(_PROGRESS_BAR_WIDTH * done / total) if total else _PROGRESS_BAR_WIDTH
    line = (f"[{'#' * filled}{'-' * (_PROGRESS_BAR_WIDTH - filled)}] {done}/{total} groups | "
            f"{added} added | {_minutes(elapsed_s)} elapsed")
    if done >= total:
        return line
    if left_s is None:
        return line + " | estimating time left..."
    if left_s < 60:
        return line + " | almost done"
    return line + f" | ~{_minutes(left_s)} left"

def _minutes(seconds: float) -> str:
    return f"{round(seconds / 60)} min" if seconds >= 90 else f"{round(seconds)}s"

def _tally(totals: dict, stats: dict):
    totals["groups_scanned"] += 1
    totals["total_added"] += stats["added"]
    totals["posts_seen"] += stats["posts_seen"]
    totals["prefiltered"] += stats["prefiltered"]
    totals["llm_parsed"] += stats["llm_parsed"]
    if stats["checkpoint_hit"]:
        totals["checkpoint_skipped"] += 1

def _close_context(context):
    try:
        context.close()
    except Exception as e:
        _safe_print(f"WARNING: closing browser context failed: {e}")

def _print_run_summary(totals: dict, gmaps_calls_this_month: int):
    print(f"\nScraping finished successfully. {totals['total_added']} apartments added.")
    print(f"Summary: {totals['groups_scanned']}/{totals['total']} groups scanned, {totals['posts_seen']} posts seen, "
          f"{totals['prefiltered']} pre-filtered, {totals['llm_parsed']} sent to the LLM, {totals['total_added']} matches added, "
          f"{gmaps_calls_this_month} Maps calls used this month, {totals['checkpoint_skipped']} checkpoint(s) hit.")
    if totals["checkpoint_skipped"]:
        print(f"{totals['checkpoint_skipped']} group(s) skipped due to a security checkpoint — rerun headful.")

def _end_of_run_upkeep(sheet):
    print("Deduplicating cross-posted listings, dropping stale rows, and sorting by post date...")
    with timed("sheets"):
        duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(sheet)
    print(f"Removed {duplicates_removed} duplicate repost(s) and {stale_removed} stale row(s) (older than {MAX_POST_AGE_DAYS} days). "
          f"Sheet now has {kept} listings, sorted by post date (newest first).")
    if ALERTS_SCAN_ENABLED:
        with timed("sheets"):
            duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(open_listings_tab(sheet.spreadsheet, YAD2_TAB_NAME))
        print(f"'{YAD2_TAB_NAME}' tab: removed {duplicates_removed} duplicate(s) and {stale_removed} stale row(s); {kept} listings.")

    pruned = storage.prune_old_posts(MAX_POST_AGE_DAYS)
    if pruned:
        print(f"Lightened {pruned} local DB row(s) older than {MAX_POST_AGE_DAYS} days (verdict kept, so they won't be rescanned).")

    # Refresh the Groups tab: names/cities just learned from page titles, and this run's stats.
    sync_groups(sheet.spreadsheet, write=True)

    try:
        backup_if_stale()
    except Exception as e:
        print(f"WARNING: DB backup failed: {e}")

def _run_started_info(group_urls: list[str], now: datetime, alerts: bool, scope: str | None) -> dict:
    overview = storage.get_group_overview()
    return {
        "groups": len(group_urls),
        "new_groups": sum(1 for url in group_urls if not overview.get(url, {}).get("posts")),
        "alerts": alerts,
        "scope": scope,
        "previous": storage.get_last_successful_live_run(),
        "now": now,
    }

def _notify_run_finished(totals: dict, gmaps_calls_this_month: int, group_urls: list[str],
                         run_started_at: datetime, duration_s: float, scope: str | None):
    try:
        overview = storage.get_group_overview()
        telegram_notifier.send_run_finished({
            "duration_s": duration_s,
            "scope": scope,
            "activity": storage.get_run_activity(run_started_at.isoformat()),
            "not_member_groups": sum(1 for url in group_urls if overview.get(url, {}).get("status") == "not_member"),
            "groups_scanned": totals["groups_scanned"],
            "total": totals["total"],
            "posts_seen": totals["posts_seen"],
            "prefiltered": totals["prefiltered"],
            "llm_parsed": totals["llm_parsed"],
            "added": totals["total_added"],
            "gmaps_calls": gmaps_calls_this_month,
            "checkpoint_skipped": totals["checkpoint_skipped"],
            "errors": run_log.current.errors if run_log.current else 0,
            "warnings": run_log.current.warnings if run_log.current else 0,
        })
    except Exception as e:
        print(f"WARNING: Telegram run-finished alert failed: {e}")
