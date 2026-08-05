"""
=== Facebook group scraper ===

run_scraper() is the public entry point: logs in (manual on first run,
persisted via chrome_profile/), scans TARGET_URLS (parallel or sequential
per MAX_CONCURRENT_GROUPS), and feeds every candidate post through
core.evaluate.process_candidate_listing. Checkpoint/CAPTCHA handling, dead-
link pruning, article selectors, and the "See more" expansion logic are all
FB-DOM-specific and live here rather than in a shared module.
"""
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

import storage
import telegram_notifier
from config import (
    TARGET_URLS, SCROLL_COUNT, SCROLL_DELAY_MS, MIN_SCROLLS_BEFORE_EARLY_STOP,
    CONSECUTIVE_OLD_POSTS_TO_STOP, MIN_ROOMS, MAX_ROOMS, ROOMS_PRE_FILTER_REGEX,
    NEGATIVE_KEYWORDS, ROOMMATE_KEYWORDS, EXCLUDED_LOCATIONS, LOGIN_MAX_ATTEMPTS,
    MAX_CONCURRENT_GROUPS, GMAPS_MONTHLY_CAP, MAX_POST_AGE_DAYS,
    PRUNE_DEAD_LINKS_ENABLED, TELEGRAM_ENABLED,
    SEE_MORE_SETTLE_POLL_MS, SEE_MORE_SETTLE_MAX_MS, SEE_MORE_RETRY_DELAY_MS,
)
from browser import _CHROME_LAUNCH_ARGS, _apply_stealth, _is_visible, _dismiss_popups
from core.util import _safe_print, _checkpoint_lock, _resume_event, _sheet_lock
from core.errors import GmapsQuotaHalted, HeadlessCheckpointAbort
from core.normalize import (
    BIDI_RE, _strip_comment_section, relative_to_date, _infer_post_date,
    _extract_candidate_cities, _ROOMMATE_COUPLE_EXCEPTION_RE, _is_recognized_fb_date_text,
)
from core.dedupe import _text_dedup_hash
from core.evaluate import process_candidate_listing
from sheets import setup_google_sheet, dedupe_and_sort_sheet, _append_rows_batch, _rewrite_sheet_data_rows

_text_dedup_lock = threading.Lock()

_run_text_hashes: dict[str, str] = {}  # text_hash -> claiming url, this run only (fresh per process)

_headless_checkpoint_hit = False  # set once any group hits a checkpoint in --headless mode; other groups then skip fast

_DEAD_POST_RE = re.compile(r"isn.t available right now", re.IGNORECASE)

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

    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception:
        return True
    return _is_visible(page.get_by_text(_DEAD_POST_RE))

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

    dead_indices = []
    for i, row in enumerate(rows):
        url = row[0] if row else ""
        if not url:
            continue
        if _is_post_removed(page, url, headless, "Dead-link check"):
            dead_indices.append(i)
            _safe_print(f"    Dead link found (post no longer available): {url}")
        time.sleep(random.uniform(2, 5))

    if not dead_indices:
        print("Dead-link check: 0 removed listing(s) found.")
        return 0

    if not live:
        print(f"DRY RUN: would remove {len(dead_indices)} dead listing(s) from the sheet. Pass --live to commit.")
        return len(dead_indices)

    keep_rows = [row for i, row in enumerate(rows) if i not in dead_indices]
    _rewrite_sheet_data_rows(sheet, len(rows), keep_rows)
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

def _wait_for_text_growth(article, before_len: int) -> str:
    """Bounded poll after a "See more" click: re-reads article.inner_text() at short
    intervals up to a ceiling, returning as soon as text visibly grows, instead of
    always sleeping the full fixed duration. Local DOM re-checks only — no extra
    requests to Facebook. Best-effort: returns the last successfully read text."""
    text = ""
    elapsed = 0
    while elapsed < SEE_MORE_SETTLE_MAX_MS:
        try:
            article.page.wait_for_timeout(SEE_MORE_SETTLE_POLL_MS)
        except Exception:
            break
        elapsed += SEE_MORE_SETTLE_POLL_MS
        try:
            text = article.inner_text().strip()
        except Exception:
            break
        if len(text) > before_len:
            return text
    return text

def _wait_for_button_settle(button_locator):
    """Bounded poll after clicking a page-level "See more" button: FB typically
    removes/hides the button once text expands, so poll for that instead of a fixed
    sleep. Same no-extra-request reasoning as _wait_for_text_growth."""
    elapsed = 0
    while elapsed < SEE_MORE_SETTLE_MAX_MS:
        try:
            button_locator.page.wait_for_timeout(SEE_MORE_SETTLE_POLL_MS)
        except Exception:
            return
        elapsed += SEE_MORE_SETTLE_POLL_MS
        try:
            if not button_locator.is_visible():
                return
        except Exception:
            return

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
                            f"Rerun without --headless to resolve it manually.")
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

        _ensure_sorted_by_new_posts(page, group_label)

        scan_started_at = datetime.now()
        last_scan_time = storage.get_last_scan_time(target_url)
        age_cutoff = datetime.now() - timedelta(days=MAX_POST_AGE_DAYS)
        effective_cutoff = max(age_cutoff, last_scan_time) if last_scan_time else age_cutoff

        _safe_print(f"[{group_label}] Scrolling (up to {SCROLL_COUNT} times, stops early once past {effective_cutoff:%d/%m %H:%M})...")
        seen_article_count = 0
        consecutive_old = 0
        scrolls_done = 0
        for i in range(SCROLL_COUNT):
            scrolls_done = i + 1
            # Jitter the scroll distance too, not just the delay — a perfectly fixed pace and size reads as more bot-like
            # Occasional cursor drift: a human's mouse doesn't sit frozen while reading a feed
            if random.random() < 0.25:
                try:
                    page.mouse.move(random.randint(200, 1100), random.randint(200, 800), steps=random.randint(5, 15))
                except Exception:
                    pass
            page.mouse.wheel(0, random.randint(3000, 5000))
            jittered_delay = max(500, SCROLL_DELAY_MS + random.randint(-400, 400))
            page.wait_for_timeout(jittered_delay)

            try:
                current_articles = _find_articles(page)
            except Exception:
                current_articles = []
            new_articles = current_articles[seen_article_count:]
            seen_article_count = len(current_articles)

            for article in new_articles:
                try:
                    _, fb_post_date = extract_post_info(article)
                except Exception:
                    continue
                post_dt = _infer_post_date(fb_post_date) if fb_post_date else None
                if post_dt is None:
                    continue  # inconclusive (comment/unparseable) — leave streak unchanged
                if post_dt < effective_cutoff:
                    consecutive_old += 1
                else:
                    consecutive_old = 0

            if scrolls_done >= MIN_SCROLLS_BEFORE_EARLY_STOP and consecutive_old >= CONSECUTIVE_OLD_POSTS_TO_STOP:
                if scrolls_done < SCROLL_COUNT:
                    _safe_print(f"[{group_label}] Stopping early after {scrolls_done} scroll(s) — {consecutive_old} consecutive old post(s) found.")
                break
        _safe_print(f"[{group_label}] Done scrolling ({scrolls_done} scroll(s)).")

        # Click "See more" to reveal the full text of long posts
        for text_pattern in ["See more", "קרא עוד", "ראה עוד"]:
            for el in page.locator(f'div[role="button"]:has-text("{text_pattern}"), span:has-text("{text_pattern}")').all():
                try:
                    if el.is_visible():
                        el.click(timeout=1000)
                        _wait_for_button_settle(el)
                except Exception:
                    pass
        page.wait_for_timeout(1500)

        # Dynamic Selectors fallback loop
        raw_articles = _find_articles(page)

        # Process articles
        articles_data = []
        for article in raw_articles:
            try:
                text = article.inner_text().strip()
                if len(text) > 20:
                    articles_data.append({"element": article, "text": text})
            except Exception:
                continue

        _safe_print(f"[{group_label}] Found {len(articles_data)} real posts.")
        stats["posts_seen"] = len(articles_data)

        if len(articles_data) == 0:
            _safe_print(f"    [{group_label}] No posts detected. Saving debug screenshot...")
            try:
                page.screenshot(path=f"debug_fb_{group_label.replace(' ', '_').replace('/', '-')}.png")
            except Exception as e:
                _safe_print(f"    WARNING: [{group_label}] debug screenshot failed: {e}")
            return stats

        valid_posts = []
        for item in articles_data:
            article = item["element"]
            text = item["text"]

            # Per-article "See more" — JS click bypasses visibility/off-screen issues.
            # One retry on a transient failure (DOM not settled yet) — otherwise a post's
            # text stays truncated forever: storage.should_skip() makes any non-parse_failed
            # verdict permanent, and --reparse-rejected/--replay only ever re-read the
            # already-truncated stored raw_text, never re-click.
            for attempt in range(2):
                try:
                    article.scroll_into_view_if_needed(timeout=500)
                    clicked = article.evaluate(_SEE_MORE_CLICK_JS)
                    if clicked:
                        text = _wait_for_text_growth(article, len(text))
                    break
                except Exception:
                    if attempt == 0:
                        page.wait_for_timeout(SEE_MORE_RETRY_DELAY_MS)
                        continue

            text = BIDI_RE.sub('', text)
            text = _strip_comment_section(text)

            post_url, fb_post_date = extract_post_info(article)
            if post_url == "Link not extracted":
                continue  # skip comments or elements that aren't a real post

            if storage.should_skip(post_url):
                _safe_print(f"    [{group_label}] Pre-filtered: Already processed (cached verdict in local DB).")
                continue

            with _sheet_lock:
                already_seen = post_url in seen_urls
            if already_seen:
                _safe_print(f"    [{group_label}] Pre-filtered: Post already exists in Google Sheets (Duplicate).")
                continue

            # Same effective_cutoff the scroll loop used to decide when to stop — a post
            # this old either predates MAX_POST_AGE_DAYS or predates the group's last scan
            # (already handled last run), so there's no point sending it to the LLM here.
            post_dt = _infer_post_date(fb_post_date) if fb_post_date else None
            if post_dt is not None and post_dt < effective_cutoff:
                _safe_print(f"    [{group_label}] Pre-filtered: Post date {fb_post_date} is older than the scan cutoff ({effective_cutoff:%d/%m %H:%M}).")
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

            excluded_found = [loc for loc in EXCLUDED_LOCATIONS if loc in text]
            if excluded_found:
                _safe_print(f"    [{group_label}] Pre-filtered: Contains excluded location '{excluded_found[0]}'.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: roommate/shared-room posts, unless explicitly also couple-friendly ---
            roommate_match = re.search(ROOMMATE_KEYWORDS, text)
            if roommate_match and not _ROOMMATE_COUPLE_EXCEPTION_RE.search(text):
                _safe_print(f"    [{group_label}] Pre-filtered: Contains negative keyword '{roommate_match.group(0)}'.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: Remove other obvious non-relevant posts (Sublets, Studio, Commercial, Seekers) ---
            neg_match = re.search(NEGATIVE_KEYWORDS, text)
            if neg_match:
                _safe_print(f"    [{group_label}] Pre-filtered: Contains negative keyword '{neg_match.group(0)}'.")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: Identify Sales instead of Rentals ---
            # Many agent posts omit the word "for sale" and just write "Price:
            # 3,395,000 ₪" — a 7+ digit price (or "X million") is an
            # unambiguous sale signal even without the word itself.
            # The separator requirement ([.,]\d{3}) blocks a false match on a phone number (050-1234567).
            sale_price_match = re.search(r'(?<!\d)[1-9]\d{0,2}(?:[.,]\d{3}){2,}(?!\d)|[1-9](?:\.\d+)?\s*(?:מיליון|מליון)', text)
            if sale_price_match:
                _safe_print(f"    [{group_label}] Pre-filtered: Apartment for sale (price {sale_price_match.group(0).strip()}).")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
                stats["prefiltered"] += 1
                continue

            # --- Pre-filter: Check for room counts explicitly before heavy LLM processing ---
            if not re.search(ROOMS_PRE_FILTER_REGEX, text):
                clean_snip = text[:100].replace('\n', ' ')
                actual_rooms_match = re.search(r'([1-9](?:\.5)?)\s*חד', text)
                if actual_rooms_match:
                    found_val = actual_rooms_match.group(1)
                    _safe_print(f"    [{group_label}] Pre-filtered: Post is for {found_val} rooms (not matching target {MIN_ROOMS}-{MAX_ROOMS}).\n      URL: {post_url}\n      Text: {clean_snip}...")
                else:
                    _safe_print(f"    [{group_label}] Pre-filtered: No mention of matching room count.\n      URL: {post_url}\n      Text: {clean_snip}...")
                storage.record_post(post_url, target_url, text, storage.VERDICT_PREFILTERED)
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

        if pending_rows and live:
            with _sheet_lock:
                try:
                    _append_rows_batch(sheet, pending_rows)
                    seen_urls.update(pending_seen_urls)
                    for record_url, record_group_url, record_text, record_data, record_analysis in pending_records:
                        storage.record_post(record_url, record_group_url, record_text, storage.VERDICT_ADDED, record_data, analysis=record_analysis)
                    _safe_print(f"    [{group_label}] Batch wrote {len(pending_rows)} row(s) to Google Sheets.")
                    if TELEGRAM_ENABLED:
                        for tg_url, tg_fields, tg_score in pending_telegram:
                            try:
                                telegram_notifier.send_listing_alert(tg_url, tg_fields, tg_score)
                            except Exception as e:
                                _safe_print(f"    WARNING: [{group_label}] Telegram alert failed for {tg_url}: {e}")
                except Exception as e:
                    _safe_print(f"    ERROR: [{group_label}] batch writing to sheet: {e}")
                    stats["added"] -= len(pending_rows)
        elif pending_rows:
            _safe_print(f"    [{group_label}] DRY RUN: {len(pending_rows)} row(s) would be written to Google Sheets (skipped — pass --live to commit).")

        storage.set_last_scan_time(target_url, scan_started_at)
    except GmapsQuotaHalted:
        _safe_print(f"    [{group_label}] Stopping: Google Maps monthly cap reached (GMAPS_ON_CAP='halt').")
    except HeadlessCheckpointAbort:
        stats["checkpoint_hit"] = True
    return stats

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
            _safe_print(f"    [{group_label}] Skipping: a security checkpoint was already hit in another group (headless mode). Rerun without --headless.")
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

def run_scraper(headless: bool = False, live: bool = False):
    if not live:
        print("DRY RUN: classifying and printing only — no sheet writes, no dedupe/prune. Pass --live to commit.")
    sheet, seen_urls = setup_google_sheet()
    _print_gmaps_quota_status()

    profile_dir = os.path.join(os.getcwd(), "chrome_profile")
    storage_state_path = os.path.join(profile_dir, "_session_state.json")

    shuffled_urls = random.sample(TARGET_URLS, len(TARGET_URLS))
    total = len(shuffled_urls)
    if live and TELEGRAM_ENABLED:
        try:
            telegram_notifier.send_run_started(total)
        except Exception as e:
            print(f"WARNING: Telegram run-started alert failed: {e}")
    groups_scanned = 0
    total_added = 0
    total_posts_seen = 0
    total_prefiltered = 0
    total_llm_parsed = 0
    checkpoint_skipped = 0
    sequential_mode = MAX_CONCURRENT_GROUPS == 1

    # --- Login phase: single persistent-context tab, sequential ---
    with sync_playwright() as p:
        persistent_kwargs = {}
        if headless:
            # Headless Chromium's UA says "HeadlessChrome" in both the HTTP header and
            # JS — the most obvious headless tell, and a UA can only be set at context
            # creation. Probe a throwaway browser for the real platform-correct UA and
            # strip the "Headless" marker.
            try:
                probe = p.chromium.launch(headless=True)
                probe_page = probe.new_page()
                probe_ua = probe_page.evaluate("navigator.userAgent") or ""
                probe.close()
                if "HeadlessChrome" in probe_ua:
                    persistent_kwargs["user_agent"] = probe_ua.replace("HeadlessChrome", "Chrome")
            except Exception:
                pass
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            viewport={"width": 1366, "height": 1600},
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=_CHROME_LAUNCH_ARGS,
            **persistent_kwargs
        )
        page = context.pages[0] if context.pages else context.new_page()
        _apply_stealth(page)

        print("Opening Facebook...")
        page.goto("https://www.facebook.com", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if headless:
            if _is_visible(page.locator('input[name="email"]')):
                print("ERROR: Cannot login manually in headless mode. Please run without --headless first.")
                context.close()
                sys.exit(1)
        else:
            if _is_visible(page.locator('input[name="email"]')):
                print("\nPlease log in to Facebook manually in the browser window that just opened.\n")

                for attempt in range(LOGIN_MAX_ATTEMPTS):
                    input("  -> Press ENTER here in the terminal ONLY AFTER you have fully logged in and see your feed... ")
                    page.wait_for_timeout(2000)
                    if not _is_visible(page.locator('input[name="email"]')):
                        break
                    print("ERROR: Facebook login form is still visible. Please complete login first.")
                else:
                    print("ERROR: Login not completed after several attempts. Exiting.")
                    context.close()
                    sys.exit(1)
            else:
                print("Already logged into Facebook. Skipping manual login.")

        if PRUNE_DEAD_LINKS_ENABLED:
            _prune_dead_links(page, sheet, headless, live)

        if sequential_mode:
            # Sequential mode: no storage_state at all — scans every group in
            # sequence on the same page inside the original context, to
            # preserve the profile's real fingerprint
            print("Sequential mode (MAX_CONCURRENT_GROUPS=1): scanning all groups one at a time in the original "
                  "browser profile — safest against checkpoints, slowest. No session-state file is written.")
            for i, url in enumerate(shuffled_urls, 1):
                if i > 1:
                    time.sleep(random.uniform(5, 15))
                try:
                    stats = _scan_group_page(page, url, f"Group {i}/{total}", sheet, seen_urls, headless, live)
                except Exception as e:
                    _safe_print(f"\nERROR: [Group {i}/{total}] crashed: {e}")
                    continue
                groups_scanned += 1
                total_added += stats["added"]
                total_posts_seen += stats["posts_seen"]
                total_prefiltered += stats["prefiltered"]
                total_llm_parsed += stats["llm_parsed"]
                if stats["checkpoint_hit"]:
                    remaining = total - i
                    checkpoint_skipped += 1 + remaining
                    _safe_print(f"Stopping sequential scan: {remaining} remaining group(s) also skipped due to the checkpoint.")
                    break
            try:
                context.close()
            except Exception as e:
                _safe_print(f"WARNING: closing browser context failed: {e}")
        else:
            # Capture the logged-in profile's fingerprint so parallel workers can
            # present the same one (see _scan_group). "HeadlessChrome" is stripped
            # so a headless run's workers still claim the normal Chrome UA.
            real_user_agent = ""
            real_locale = ""
            try:
                real_user_agent = (page.evaluate("navigator.userAgent") or "").replace("HeadlessChrome", "Chrome")
                real_locale = page.evaluate("navigator.language") or ""
            except Exception:
                pass
            # Exports cookies/session to a file so independent threads can use
            # them — one context/browser can't be shared between threads (Playwright's sync API isn't thread-safe)
            context.storage_state(path=storage_state_path)
            try:
                context.close()
            except Exception as e:
                _safe_print(f"WARNING: closing browser context failed: {e}")

    if not sequential_mode:
        print(f"Continuing to scan groups ({MAX_CONCURRENT_GROUPS} in parallel — this raises checkpoint/ban risk "
              f"vs. sequential mode; set MAX_CONCURRENT_GROUPS=1 in config.py if you start seeing checkpoints)...")
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_GROUPS) as executor:
            futures = {
                executor.submit(_scan_group, url, f"Group {idx}/{total}", sheet, seen_urls, storage_state_path, headless,
                                real_user_agent, real_locale, live): idx
                for idx, url in enumerate(shuffled_urls, 1)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    stats = future.result()
                    groups_scanned += 1
                    total_added += stats["added"]
                    total_posts_seen += stats["posts_seen"]
                    total_prefiltered += stats["prefiltered"]
                    total_llm_parsed += stats["llm_parsed"]
                    if stats["checkpoint_hit"]:
                        checkpoint_skipped += 1
                except Exception as e:
                    _safe_print(f"\nERROR: [Group {idx}/{total}] crashed: {e}")

    gmaps_calls_this_month = storage.get_gmaps_usage()
    print(f"\nScraping finished successfully. {total_added} apartments added.")
    print(f"Summary: {groups_scanned}/{total} groups scanned, {total_posts_seen} posts seen, "
          f"{total_prefiltered} pre-filtered, {total_llm_parsed} sent to the LLM, {total_added} matches added, "
          f"{gmaps_calls_this_month} Maps calls used this month, {checkpoint_skipped} checkpoint(s) hit.")
    if checkpoint_skipped:
        print(f"{checkpoint_skipped} group(s) skipped due to a security checkpoint — rerun headful.")

    if live:
        print("Deduplicating cross-posted listings, dropping stale rows, and sorting by post date...")
        duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(sheet)
        print(f"Removed {duplicates_removed} duplicate repost(s) and {stale_removed} stale row(s) (older than {MAX_POST_AGE_DAYS} days). "
              f"Sheet now has {kept} listings, sorted by post date (newest first).")

        pruned = storage.prune_old_posts(MAX_POST_AGE_DAYS)
        if pruned:
            print(f"Lightened {pruned} local DB row(s) older than {MAX_POST_AGE_DAYS} days (verdict kept, so they won't be rescanned).")
    else:
        print("DRY RUN: skipping sheet dedupe/sort and DB pruning (pass --live to commit).")

    if live and TELEGRAM_ENABLED:
        try:
            telegram_notifier.send_run_finished({
                "groups_scanned": groups_scanned,
                "total": total,
                "posts_seen": total_posts_seen,
                "prefiltered": total_prefiltered,
                "llm_parsed": total_llm_parsed,
                "added": total_added,
                "gmaps_calls": gmaps_calls_this_month,
                "checkpoint_skipped": checkpoint_skipped,
            })
        except Exception as e:
            print(f"WARNING: Telegram run-finished alert failed: {e}")

    return {
        "groups_scanned": groups_scanned,
        "total": total,
        "checkpoint_skipped": checkpoint_skipped,
        "total_added": total_added,
    }
