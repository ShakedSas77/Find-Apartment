import argparse
import json
import os
import re
import sys
import time
import traceback

from config import (
    MIN_PRICE, MAX_PRICE, DESTINATION_ADDRESS, ROOMS_PRE_FILTER_REGEX,
    LOCATIONS,
    MAX_POST_AGE_DAYS, MAX_WALKING_DISTANCE_KM,
    TELEGRAM_ENABLED, SHEET_HEADERS, ALERTS_LOOKBACK_DAYS, ALERTS_DUMP_DIR, LOG_RETENTION_DAYS,
)
from core.util import _with_retries
from core.errors import LoginRequired
from core.normalize import BIDI_RE, _strip_comment_section, _ENGLISH_ROOMS_RE
from core.llm import analyze_post_with_llm
from core.evaluate import (
    _evaluate_post_data, _build_row, _analysis_from_fields, _DISTANCE_FILTERABLE_SOURCES,
    _content_prefilter, _extract_price_candidates,
)
from integrations.sheets import open_output, dedupe_and_sort_sheet, _fields_from_sheet_row, _SCORE_COL_INDEX
from sources.fb_scraper import list_my_groups, log_in_to_facebook, run_scraper
from sources.mail_source import dump_alert_emails
from core.run_log import start_run_log
from integrations.groups_sync import sync_groups
from core.llm_eval import run_eval
import env
from core import scoring
from core import storage
from integrations import telegram_notifier


def reparse_rejected_posts():
    """
    Re-runs the LLM + filters against raw text already stored in SQLite for posts
    previously verdict-ed rejected_price / rejected_rooms / rejected_distance /
    price_unknown / parse_failed — the prompt-iteration workflow. Opens no browser;
    still writes real matches to the sheet via the normal (non-Playwright) Sheets/Maps
    API clients.
    """
    sheet, seen_urls = open_output()
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
            flag = "   <- 0 matches, candidate to pause in the Groups tab" if added == 0 else ""
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

    _print_suspicious_rejects()

    runs = storage.get_recent_runs(10)
    if runs:
        print("\n--- last runs (newest first) ---")
        for run in runs:
            print(f"  {run['started_at'][:16]}  {run['mode']:<7} {run['status']:<17} "
                  f"groups {run['groups_scanned'] or 0}/{run['groups_total'] or 0}, added {run['added'] or 0}, "
                  f"{run['errors'] or 0} error(s), {run['warnings'] or 0} warning(s)  {run['log_path'] or ''}")

def _print_suspicious_rejects():
    """
    Posts a pre-filter rule rejected even though they state a target room count AND an in-range price —
    the likeliest false rejects, grouped by rule. Every rule fix on 2026-11-07 (same-day cutoff, "מחפשים
    דייר", "סטודיו פילאטיס", "הנגבה בגבעתיים") started as exactly this kind of hand audit.
    """
    by_rule: dict[str, list[str]] = {}
    for post in storage.get_recent_prefiltered(MAX_POST_AGE_DAYS):
        text = post["raw_text"]
        rule = post["reject_reason"]
        if rule.startswith(("older_than_scan_cutoff", "for_sale", "no_price_amount")):
            continue
        has_rooms = re.search(ROOMS_PRE_FILTER_REGEX, text) or _ENGLISH_ROOMS_RE.search(text)
        if has_rooms and any(MIN_PRICE <= p <= MAX_PRICE for p in _extract_price_candidates(text)):
            by_rule.setdefault(rule, []).append(text)
    if not by_rule:
        return
    print(f"\n--- suspicious rejects, last {MAX_POST_AGE_DAYS} days (rule fired, but rooms + price fit) ---")
    for rule, texts in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(texts):4}  {rule}")
        keyword = rule.split(":", 1)[1] if ":" in rule else ""
        for text in texts[:2]:
            at = text.find(keyword) if keyword else 0
            print(f"          ...{text[max(0, at - 40):at + 50].replace(chr(10), ' ')}...")

_TOTAL_COST_COL_INDEX = SHEET_HEADERS.index("עלות חודשית כוללת")

def recompute_sheet_scores():
    """
    Rewrites the ציון התאמה and עלות חודשית כוללת cells of every existing sheet row using the current
    scoring.compute_fit_score() formula — for when the formula itself changes
    (as in the entry-date curve redesign) and old rows are left holding scores
    from the previous version. No browser, no Maps/Gemini calls.
    """
    sheet, _ = open_output()
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
    sheet, _ = open_output()
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
    sheet, _ = open_output()
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
    parser.add_argument("--sync-groups", action="store_true",
                         help="Sync the sheet's Groups tab with the local DB (fill names/cities, refresh stats), then exit")
    parser.add_argument("--eval", action="store_true",
                         help="Score the LLM's extraction of test_posts/*.txt against test_posts/expected.json (one LLM call per fixture), then exit")
    parser.add_argument("--ollama-model", metavar="NAME",
                         help="With --eval: score this local Ollama model instead of the Gemini chain (e.g. aya-expanse:8b)")
    parser.add_argument("--mail-dump", action="store_true",
                         help="Save recent saved-search alert emails (read-only IMAP) as text files in ALERTS_DUMP_DIR, then exit")
    parser.add_argument("--source", choices=["fb", "yad2"],
                         help="Scan only the Facebook groups (fb) or only the Yad2 alert emails (yad2). Default: both")
    parser.add_argument("--group", metavar="URL",
                         help="Scan only this Facebook group (a link from the Groups tab); implies --source fb")
    parser.add_argument("--scheduled", action="store_true",
                         help="Set by the Task Scheduler launcher: skip the run while scheduled scans are paused (/pause in Telegram)")
    parser.add_argument("--login", action="store_true",
                         help="Open the bot's browser to log in to Facebook once; it closes by itself when done")
    parser.add_argument("--list-my-groups", action="store_true",
                         help="Print the Facebook groups the logged-in account has joined, as JSON (rental-looking first)")
    args = parser.parse_args()

    # Before the key check: the web app's setup runs these before the AI and the rest are chosen.
    if args.login:
        sys.exit(0 if log_in_to_facebook() else 1)
    if args.list_my_groups:
        try:
            print(json.dumps(list_my_groups(), ensure_ascii=False))
        except LoginRequired as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

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

    if args.sync_groups:
        sheet, _ = open_output()
        active = sync_groups(sheet.spreadsheet, write=True, sort_by_yield=True)
        print(f"Groups tab synced and sorted by match rate: {len(active)} active group(s) of {len(storage.get_groups())}.")
        sys.exit(0)

    if args.eval:
        run_eval(ollama_model=args.ollama_model)
        sys.exit(0)

    if args.mail_dump:
        written = dump_alert_emails()
        print(f"Saved {written} alert email(s) from the last {ALERTS_LOOKBACK_DAYS} day(s) to {ALERTS_DUMP_DIR}/.")
        sys.exit(0)

    paused_since = storage.get_bot_state(storage.SCANS_PAUSED_KEY)
    if args.scheduled and paused_since:
        print(f"Scheduled scans are paused since {paused_since[:16]} (/resume in Telegram) - skipping this run.")
        sys.exit(0)

    run_log = start_run_log("logs", LOG_RETENTION_DAYS)
    print("\n=======================================================")
    print("  Apartment Search Bot - Real-time updates")
    print("=======================================================")
    print("  Groups:      from the sheet's Groups tab")
    print(f"  Areas (info): {', '.join(LOCATIONS)}")
    print(f"  Price range: ₪{MIN_PRICE:,} – ₪{MAX_PRICE:,}")
    print(f"  Distance to: {DESTINATION_ADDRESS}")
    print("=======================================================")

    source = "fb" if args.group else args.source
    # A partial scan gets its own mode, so "the previous run" and doctor's staleness check
    # (get_last_successful_live_run) keep meaning a full scan.
    mode = "live" if args.live else "dry_run"
    if args.group:
        mode += "_group"
    elif source:
        mode += f"_{source}"
    run_id = storage.start_run(mode, str(run_log.path))
    storage.set_bot_state(storage.SCAN_PID_KEY, f"{run_id}:{os.getpid()}")
    try:
        result = run_scraper(headless=args.headless, live=args.live, source=source, group=args.group)
    except LoginRequired as e:
        print(f"ERROR: {e}")
        storage.finish_run(run_id, "login_required", {}, run_log.warnings, run_log.errors)
        print(run_log.summary())
        sys.exit(1)
    except BaseException:
        traceback.print_exc()
        storage.finish_run(run_id, "crashed", {}, run_log.warnings, run_log.errors)
        print(run_log.summary())
        sys.exit(1)

    if result["checkpoint_skipped"] > 0:
        status, exit_code = "checkpoint", 2
    elif result["groups_scanned"] == 0 and result["total"] > 0:
        status, exit_code = "no_groups_scanned", 1
    else:
        status, exit_code = "ok", 0
    storage.finish_run(run_id, status, result, run_log.warnings, run_log.errors)
    print(run_log.summary())
    sys.exit(exit_code)
