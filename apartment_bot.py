import argparse
import json
import sys
import time
import traceback

from config import (
    TARGET_URLS,
    MIN_PRICE, MAX_PRICE, DESTINATION_ADDRESS,
    LOCATIONS,
    MAX_POST_AGE_DAYS, MAX_WALKING_DISTANCE_KM,
    TELEGRAM_ENABLED, SHEET_HEADERS, ALERTS_LOOKBACK_DAYS, ALERTS_DUMP_DIR,
)
from core.util import _with_retries
from core.normalize import BIDI_RE, _strip_comment_section
from core.llm import analyze_post_with_llm
from core.evaluate import (
    _evaluate_post_data, _build_row, _analysis_from_fields, _DISTANCE_FILTERABLE_SOURCES,
    _content_prefilter,
)
from sheets import setup_google_sheet, dedupe_and_sort_sheet, _fields_from_sheet_row, _SCORE_COL_INDEX
from fb_scraper import run_scraper
from mail_source import dump_alert_emails
import env
import scoring
import storage
import telegram_notifier


def reparse_rejected_posts():
    """
    Re-runs the LLM + filters against raw text already stored in SQLite for posts
    previously verdict-ed rejected_price / rejected_rooms / rejected_distance /
    price_unknown / parse_failed — the prompt-iteration workflow. Opens no browser;
    still writes real matches to the sheet via the normal (non-Playwright) Sheets/Maps
    API clients.
    """
    sheet, seen_urls = setup_google_sheet()
    candidates = storage.get_reparse_candidates()
    print(f"\nRe-parsing {len(candidates)} rejected/failed post(s) from local DB (no browser)...")

    added = 0
    for post in candidates:
        url = post["url"]
        text = post["raw_text"]
        group_url = post["group_url"]
        if not text or url in seen_urls:
            continue

        data = analyze_post_with_llm(text)
        if not data:
            storage.record_post(url, group_url, text, storage.VERDICT_PARSE_FAILED,
                                 analysis=_analysis_from_fields({}, post.get("post_date") or "", storage.VERDICT_PARSE_FAILED))
            print(f"    Still failing to parse: {url}")
            continue

        verdict, fields = _evaluate_post_data(data, text, group_url)
        if verdict != storage.VERDICT_ADDED:
            storage.record_post(url, group_url, text, verdict, data, analysis=_analysis_from_fields(fields, post.get("post_date") or "", verdict))
            print(f"    Still {verdict}: {url}")
            continue

        post_date = post.get("post_date") or ""
        new_row = _build_row(url, post_date, fields)
        if fields.get("distance_source") == "excluded_south":
            storage.record_post(url, group_url, text, storage.VERDICT_REJECTED_LOCATION, data, analysis=_analysis_from_fields(fields, post_date, storage.VERDICT_REJECTED_LOCATION))
            print(f"    Still rejected_location: {url}")
            continue
        dist_meters = fields.get("distance_meters")
        if fields.get("distance_source") in _DISTANCE_FILTERABLE_SOURCES and dist_meters is not None and (dist_meters / 1000.0) > MAX_WALKING_DISTANCE_KM:
            storage.record_post(url, group_url, text, storage.VERDICT_REJECTED_DISTANCE, data, analysis=_analysis_from_fields(fields, post_date, storage.VERDICT_REJECTED_DISTANCE))
            print(f"    Still rejected_distance: {url}")
            continue
        try:
            _with_retries(lambda: sheet.append_row(new_row, value_input_option="USER_ENTERED", table_range="A1"))
            seen_urls.add(url)
            storage.record_post(url, group_url, text, storage.VERDICT_ADDED, data, analysis=_analysis_from_fields(fields, post_date))
            added += 1
            price_display = f"{int(fields['price_val']):,} ₪" if fields['price_val'] else "מחיר לא צוין"
            print(f"    SUCCESS: Apartment added: {fields['rooms_val']} rooms | {price_display} | Address: {fields['address']}")
            if TELEGRAM_ENABLED:
                try:
                    telegram_notifier.send_listing_alert(url, fields, new_row[_SCORE_COL_INDEX])
                except Exception as e:
                    print(f"    WARNING: Telegram alert failed for {url}: {e}")
        except Exception as e:
            print(f"    ERROR: writing to sheet: {e}")

    if added:
        duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(sheet)
        print(f"Removed {duplicates_removed} duplicate repost(s) and {stale_removed} stale row(s). Sheet now has {kept} listings, sorted by post date (newest first).")
    print(f"\nDone. {added} new apartment(s) added from reparse.")


def replay_all_posts(use_llm: bool = False):
    """
    Read-only, no browser, no sheet, no DB writes: re-runs every post stored
    in SQLite (raw_text) through the current filters/normalization and reports
    which posts' verdict would change vs. what's stored. The tuning workflow
    after editing a filter/threshold/scoring rule — see exactly what flips
    without rebuilding the sheet (which would wipe hand-edited rows) or
    mutating the DB's cached verdicts.
    By default it reuses each post's stored LLM parse (parsed_json) — no LLM
    calls, so it's fast and free; a post that never reached the LLM but would
    now pass the content pre-filters can't be judged that way and is counted,
    not guessed. use_llm=True (--replay --llm) re-parses every post instead —
    for prompt/LLM changes, at one LLM call per post.
    Note: the relative date stored at original scan time ("3 days ago") doesn't
    update with time — replay doesn't re-apply the MAX_POST_AGE_DAYS filter, only filters/LLM/normalization.
    """
    posts = storage.get_all_posts()
    mode = "re-parsing with the LLM" if use_llm else "reusing stored LLM parses"
    print(f"\nReplaying {len(posts)} stored post(s) through the current code, {mode} (read-only — no sheet/DB writes)...")

    changed = []
    skipped_no_text = 0
    needs_llm = 0
    for post in posts:
        url = post["url"]
        old_verdict = post.get("verdict") or "(none)"
        group_url = post.get("group_url") or ""
        text = post.get("raw_text") or ""
        if not text:
            skipped_no_text += 1
            continue
        text = _strip_comment_section(BIDI_RE.sub('', text))

        prefiltered = _content_prefilter(text)
        if prefiltered is not None:
            new_verdict, _, _ = prefiltered
        else:
            if use_llm:
                data = analyze_post_with_llm(text)
            elif post.get("parsed_json"):
                data = json.loads(post["parsed_json"])
            else:
                needs_llm += 1
                continue
            if not data:
                new_verdict = storage.VERDICT_PARSE_FAILED
            else:
                new_verdict, fields = _evaluate_post_data(data, text, group_url, post_url=url)
                if new_verdict == storage.VERDICT_ADDED:
                    if fields.get("distance_source") == "excluded_south":
                        new_verdict = storage.VERDICT_REJECTED_LOCATION
                    else:
                        dist_meters = fields.get("distance_meters")
                        if fields.get("distance_source") in _DISTANCE_FILTERABLE_SOURCES and dist_meters is not None and (dist_meters / 1000.0) > MAX_WALKING_DISTANCE_KM:
                            new_verdict = storage.VERDICT_REJECTED_DISTANCE

        if new_verdict != old_verdict:
            changed.append((url, old_verdict, new_verdict))
            print(f"    CHANGED: {old_verdict} -> {new_verdict}  {url}")

    print(f"\nDone. {len(changed)} verdict(s) changed out of {len(posts)} stored posts "
          f"({skipped_no_text} skipped: raw_text pruned). Nothing was written — "
          f"re-run the real scraper (live is the default) to actually commit any newly-matching posts.")
    if needs_llm:
        print(f"{needs_llm} post(s) now pass the content pre-filters but were never parsed by the LLM — "
              f"run --replay --llm to judge them too.")
    return changed

def print_stats():
    counts, month, gmaps_calls = storage.get_stats()
    print("\n=== Local DB stats ===")
    for verdict in storage.ALL_VERDICTS:
        print(f"  {verdict}: {counts.get(verdict, 0)}")
    print(f"\nGoogle Maps calls this month ({month}): {gmaps_calls}")

    group_stats = storage.get_group_stats()
    if group_stats:
        print("\n--- per-group yield (added | total) ---")
        for group_url, verdict_counts in sorted(group_stats.items()):
            added = verdict_counts.get(storage.VERDICT_ADDED, 0)
            total = sum(verdict_counts.values())
            flag = "   <- 0 matches, candidate to drop from TARGET_URLS" if added == 0 else ""
            print(f"  {group_url}   {added:>3} | {total:>4}{flag}")

    top_locations = storage.top_excluded_locations()
    if top_locations:
        print("\n--- top excluded locations (tune EXCLUDED_LOCATIONS) ---")
        for location, cnt in top_locations:
            print(f"  {cnt:4}  {location}")

    top_addresses = storage.top_rejected_distance_addresses()
    if top_addresses:
        print("\n--- top rejected_distance addresses (tune MAX_WALKING_DISTANCE_KM/geocoding) ---")
        for address, cnt in top_addresses:
            print(f"  {cnt:4}  {address}")

_TOTAL_COST_COL_INDEX = SHEET_HEADERS.index("עלות חודשית כוללת")

def recompute_sheet_scores():
    """
    Rewrites the ציון התאמה and עלות חודשית כוללת cells of every existing sheet row using the current
    scoring.compute_fit_score() formula — for when the formula itself changes
    (as in the entry-date curve redesign) and old rows are left holding scores
    from the previous version. No browser, no Maps/Gemini calls.
    """
    sheet, _ = setup_google_sheet()
    data = sheet.get_all_values()
    rows = data[1:]
    if not rows:
        print("Sheet has no data rows.")
        return

    changed = 0
    new_scores = []
    for row in rows:
        old_score = row[_SCORE_COL_INDEX] if len(row) > _SCORE_COL_INDEX else ""
        fields = _fields_from_sheet_row(row)
        new_score = scoring.compute_fit_score(fields)
        if str(new_score) != old_score.strip():
            changed += 1
        new_scores.append([new_score, scoring.compute_total_monthly_cost(fields)])

    col_letter = chr(ord('A') + _SCORE_COL_INDEX)
    last_col_letter = chr(ord('A') + _TOTAL_COST_COL_INDEX)
    _with_retries(lambda: sheet.update(
        range_name=f"{col_letter}2:{last_col_letter}{len(rows) + 1}",
        values=new_scores,
        value_input_option="RAW",
    ))
    print(f"Recomputed fit score for {len(rows)} row(s); {changed} changed.")

def backfill_ratings():
    """
    Pushes every currently-listed sheet row to Telegram with the 0-10 rating
    keyboard, for collecting human judgment on apartments already added —
    the training set for eventually regressing compute_fit_score()'s weights
    against real preference (storage.get_ratings_with_listing_data()). Reuses
    the same row->fields reconstruction as --recompute-scores. No browser, no
    Maps/Gemini calls. 3s pacing between sends — Telegram's group-chat rate
    limit is roughly 20 messages/minute; telegram_notifier._call() also
    retries once on a 429, honoring the server's own retry_after cooldown.
    """
    if not TELEGRAM_ENABLED:
        print("TELEGRAM_ENABLED is False in config.py — nothing to send.")
        return
    sheet, _ = setup_google_sheet()
    data = sheet.get_all_values()
    rows = data[1:]
    if not rows:
        print("Sheet has no data rows.")
        return

    telegram_notifier.send_backfill_announcement(len(rows))
    sent = 0
    for row in rows:
        url = row[0] if row else ""
        if not url:
            continue
        score_cell = row[_SCORE_COL_INDEX].strip() if len(row) > _SCORE_COL_INDEX else ""
        try:
            score = int(score_cell)
        except ValueError:
            score = 0
        message_id = telegram_notifier.send_listing_alert(url, _fields_from_sheet_row(row), score)
        if message_id:
            sent += 1
        time.sleep(3)
    print(f"Sent {sent}/{len(rows)} listing(s) to Telegram for rating.")

def prune_data():
    """
    On-demand cleanup, no browser: drops sheet rows and lightens local DB rows
    older than MAX_POST_AGE_DAYS. Runs automatically at the end of every
    scheduled scan too (run_scraper()) — this flag is for running it standalone.
    """
    sheet, _ = setup_google_sheet()
    print(f"\nPruning rows/records older than {MAX_POST_AGE_DAYS} days...")
    duplicates_removed, stale_removed, kept = dedupe_and_sort_sheet(sheet)
    print(f"Removed {duplicates_removed} duplicate repost(s) and {stale_removed} stale row(s). Sheet now has {kept} listings.")
    pruned = storage.prune_old_posts(MAX_POST_AGE_DAYS)
    print(f"Lightened {pruned} local DB row(s) (verdict kept, so they won't be rescanned).")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook Apartment Scraper Bot - Realtime")
    parser.add_argument("--headful", dest="headless", action="store_false",
                         help="Show the browser window (needed for the first FB login or solving a checkpoint). "
                              "Default is headless.")
    parser.add_argument("--dry-run", dest="live", action="store_false",
                         help="Classify + print only: no sheet writes, no added verdicts, no dedupe/prune. "
                              "Default is live (commit results).")
    # Former opt-in flags, now the defaults — still accepted so existing scripts/tasks keep working.
    parser.add_argument("--headless", dest="headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--live", dest="live", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(headless=True, live=True)
    parser.add_argument("--reparse-rejected", action="store_true",
                         help="Re-run LLM + filters on stored rejected/failed posts, no browser")
    parser.add_argument("--replay", action="store_true",
                         help="Re-run ALL stored posts through the current filters (reusing stored LLM parses) and report which verdicts would change, no browser, no sheet/DB writes")
    parser.add_argument("--llm", action="store_true",
                         help="With --replay: re-parse every post with the LLM instead of reusing stored parses (for prompt changes; one LLM call per post)")
    parser.add_argument("--stats", action="store_true",
                         help="Print verdict counts and Maps usage from the local DB, then exit")
    parser.add_argument("--prune", action="store_true",
                         help="Drop sheet rows and lighten local DB rows older than MAX_POST_AGE_DAYS, no browser, then exit")
    parser.add_argument("--recompute-scores", action="store_true",
                         help="Rewrite every existing sheet row's fit score with the current scoring formula, no browser, then exit")
    parser.add_argument("--backfill-ratings", action="store_true",
                         help="Send every existing sheet row to Telegram with the 0-10 rating keyboard, no browser, then exit")
    parser.add_argument("--mail-dump", action="store_true",
                         help="Save recent saved-search alert emails (read-only IMAP) as text files in ALERTS_DUMP_DIR, then exit")
    args = parser.parse_args()

    env.require_env()
    storage.init_db()

    if args.stats:
        print_stats()
        sys.exit(0)

    if args.reparse_rejected:
        reparse_rejected_posts()
        sys.exit(0)

    if args.replay:
        replay_all_posts(use_llm=args.llm)
        sys.exit(0)

    if args.prune:
        prune_data()
        sys.exit(0)

    if args.recompute_scores:
        recompute_sheet_scores()
        sys.exit(0)

    if args.backfill_ratings:
        backfill_ratings()
        sys.exit(0)

    if args.mail_dump:
        written = dump_alert_emails()
        print(f"Saved {written} alert email(s) from the last {ALERTS_LOOKBACK_DAYS} day(s) to {ALERTS_DUMP_DIR}/.")
        sys.exit(0)

    print("\n=======================================================")
    print("  Apartment Search Bot - Real-time updates")
    print("=======================================================")
    print(f"  Groups:      {len(TARGET_URLS)}")
    print(f"  Areas (info): {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")

    try:
        result = run_scraper(headless=args.headless, live=args.live)
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    if result["checkpoint_skipped"] > 0:
        sys.exit(2)
    if result["groups_scanned"] == 0:
        sys.exit(1)
    sys.exit(0)
