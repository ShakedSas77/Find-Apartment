"""
=== Persistence layer (SQLite) ===

Local memory for every post ever scanned — not just the matches that reach the
sheet. Saves repeat LLM calls on posts already rejected/disqualified, and lets
the raw text be replayed for prompt iteration (--reparse-rejected) without
opening a browser.

The sheet (Google Sheet) stays the source of truth for actual matches; this DB
is the memory of everything else (rejected/pre-filtered/failed).
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "bot_data.db"  # project root, not core/

VERDICT_ADDED = "added"
VERDICT_REJECTED_PRICE = "rejected_price"
VERDICT_REJECTED_ROOMS = "rejected_rooms"
VERDICT_REJECTED_DISTANCE = "rejected_distance"
VERDICT_PREFILTERED = "prefiltered"
VERDICT_PARSE_FAILED = "parse_failed"
VERDICT_PRICE_UNKNOWN = "price_unknown"
VERDICT_DUPLICATE_LISTING = "duplicate_listing"
VERDICT_DUPLICATE_TEXT = "duplicate_text"
VERDICT_REJECTED_LOCATION = "rejected_location"
VERDICT_REJECTED_ENTRY_DATE = "rejected_entry_date"

ALL_VERDICTS = [
    VERDICT_ADDED, VERDICT_REJECTED_PRICE, VERDICT_REJECTED_ROOMS,
    VERDICT_REJECTED_DISTANCE, VERDICT_PREFILTERED, VERDICT_PARSE_FAILED,
    VERDICT_PRICE_UNKNOWN, VERDICT_DUPLICATE_LISTING, VERDICT_DUPLICATE_TEXT,
    VERDICT_REJECTED_LOCATION, VERDICT_REJECTED_ENTRY_DATE,
]

# Max LLM attempts for a post that failed to parse, before giving up on it permanently
MAX_PARSE_ATTEMPTS = 3

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                url TEXT PRIMARY KEY,
                group_url TEXT,
                raw_text TEXT,
                parsed_json TEXT,
                verdict TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT,
                last_processed TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                month TEXT PRIMARY KEY,
                gmaps_calls INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS address_cache (
                address_key TEXT PRIMARY KEY,
                original_address TEXT,
                canonical_address TEXT,
                city TEXT,
                confidence TEXT,
                warning TEXT,
                distance_text TEXT,
                distance_meters INTEGER,
                distance_source TEXT,
                geocode_status TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_city_hints (
                group_url TEXT PRIMARY KEY,
                group_name TEXT,
                cities_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_scan_state (
                group_url TEXT PRIMARY KEY,
                last_scanned_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                url TEXT,
                voter_id TEXT,
                voter_name TEXT,
                vote TEXT,
                voted_at TEXT,
                PRIMARY KEY (url, voter_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Run-to-run bookkeeping that belongs to no single post or group (e.g. when the dead-link check last ran).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                url TEXT,
                voter_id TEXT,
                voter_name TEXT,
                score INTEGER,
                rated_at TEXT,
                PRIMARY KEY (url, voter_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_status (
                group_url TEXT PRIMARY KEY,
                status TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                url TEXT PRIMARY KEY,
                name TEXT,
                cities TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                position INTEGER,
                synced_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                finished_at TEXT,
                mode TEXT,
                status TEXT,
                groups_scanned INTEGER,
                groups_total INTEGER,
                posts_seen INTEGER,
                prefiltered INTEGER,
                llm_parsed INTEGER,
                added INTEGER,
                checkpoints INTEGER,
                warnings INTEGER,
                errors INTEGER,
                log_path TEXT
            )
        """)

        existing_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        migrations = {
            "price_val": "ALTER TABLE posts ADD COLUMN price_val REAL",
            "rooms_val": "ALTER TABLE posts ADD COLUMN rooms_val REAL",
            "address": "ALTER TABLE posts ADD COLUMN address TEXT",
            "address_confidence": "ALTER TABLE posts ADD COLUMN address_confidence TEXT",
            "distance_text": "ALTER TABLE posts ADD COLUMN distance_text TEXT",
            "distance_meters": "ALTER TABLE posts ADD COLUMN distance_meters INTEGER",
            "post_date": "ALTER TABLE posts ADD COLUMN post_date TEXT",
            "reject_reason": "ALTER TABLE posts ADD COLUMN reject_reason TEXT",
            "model_used": "ALTER TABLE posts ADD COLUMN model_used TEXT",
            "text_hash": "ALTER TABLE posts ADD COLUMN text_hash TEXT",
            "telegram_chat_id": "ALTER TABLE posts ADD COLUMN telegram_chat_id TEXT",
            "telegram_message_id": "ALTER TABLE posts ADD COLUMN telegram_message_id TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                conn.execute(statement)

        existing_address_cache_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(address_cache)").fetchall()
        }
        address_cache_migrations = {
            "lat": "ALTER TABLE address_cache ADD COLUMN lat REAL",
            "lon": "ALTER TABLE address_cache ADD COLUMN lon REAL",
        }
        for column, statement in address_cache_migrations.items():
            if column not in existing_address_cache_columns:
                conn.execute(statement)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_text_hash ON posts(text_hash)")


def should_skip(url: str) -> bool:
    """
    True if the post has already been processed and can be skipped: any verdict
    except parse_failed (which retries automatically up to MAX_PARSE_ATTEMPTS times).
    """
    with _lock, _connect() as conn:
        row = conn.execute("SELECT verdict, attempts FROM posts WHERE url = ?", (url,)).fetchone()
    if not row:
        return False
    if row["verdict"] != VERDICT_PARSE_FAILED:
        return True
    return row["attempts"] >= MAX_PARSE_ATTEMPTS


def find_by_text_hash(text_hash: str, exclude_url: str | None = None) -> dict | None:
    """
    Finds an already-processed post (any run, any verdict except parse_failed
    — a prior parse failure shouldn't stop a fresh LLM attempt) with the same
    normalized-text hash, i.e. the same listing text reposted verbatim to a
    different group/URL. Lets the scan skip the LLM call entirely for a
    text-identical crosspost instead of paying for it twice.
    """
    if not text_hash:
        return None
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT url, verdict FROM posts WHERE text_hash = ? AND verdict != ? AND url != ? LIMIT 1",
            (text_hash, VERDICT_PARSE_FAILED, exclude_url or ""),
        ).fetchone()
    return dict(row) if row else None


def record_post(
    url: str,
    group_url: str,
    raw_text: str,
    verdict: str,
    parsed_data: dict | None = None,
    analysis: dict | None = None,
):
    now = datetime.now().isoformat()
    parsed_json = json.dumps(parsed_data, ensure_ascii=False) if parsed_data is not None else None
    analysis = analysis or {}
    with _lock, _connect() as conn:
        existing = conn.execute("SELECT first_seen, attempts FROM posts WHERE url = ?", (url,)).fetchone()
        if existing:
            attempts = existing["attempts"] + 1 if verdict == VERDICT_PARSE_FAILED else existing["attempts"]
            conn.execute(
                """UPDATE posts SET
                       group_url=?,
                       raw_text=?,
                       parsed_json=?,
                       verdict=?,
                       attempts=?,
                       last_processed=?,
                       price_val=?,
                       rooms_val=?,
                       address=?,
                       address_confidence=?,
                       distance_text=?,
                       distance_meters=?,
                       post_date=?,
                       reject_reason=?,
                       model_used=?,
                       text_hash=?
                   WHERE url=?""",
                (
                    group_url,
                    raw_text,
                    parsed_json,
                    verdict,
                    attempts,
                    now,
                    analysis.get("price_val"),
                    analysis.get("rooms_val"),
                    analysis.get("address"),
                    analysis.get("address_confidence"),
                    analysis.get("distance_text"),
                    analysis.get("distance_meters"),
                    analysis.get("post_date"),
                    analysis.get("reject_reason"),
                    analysis.get("model_used"),
                    analysis.get("text_hash"),
                    url,
                ),
            )
        else:
            attempts = 1 if verdict == VERDICT_PARSE_FAILED else 0
            conn.execute(
                """INSERT INTO posts (
                       url, group_url, raw_text, parsed_json, verdict, attempts, first_seen, last_processed,
                       price_val, rooms_val, address, address_confidence, distance_text, distance_meters,
                       post_date, reject_reason, model_used, text_hash
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    url,
                    group_url,
                    raw_text,
                    parsed_json,
                    verdict,
                    attempts,
                    now,
                    now,
                    analysis.get("price_val"),
                    analysis.get("rooms_val"),
                    analysis.get("address"),
                    analysis.get("address_confidence"),
                    analysis.get("distance_text"),
                    analysis.get("distance_meters"),
                    analysis.get("post_date"),
                    analysis.get("reject_reason"),
                    analysis.get("model_used"),
                    analysis.get("text_hash"),
                ),
            )


def _address_cache_key(address: str) -> str:
    return " ".join((address or "").strip().lower().split())


def get_address_cache(address: str) -> dict | None:
    key = _address_cache_key(address)
    if not key:
        return None
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM address_cache WHERE address_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def save_address_cache(
    original_address: str,
    canonical_address: str,
    city: str,
    confidence: str,
    warning: str,
    distance_text: str,
    distance_meters,
    distance_source: str,
    geocode_status: str,
    lat: float | None = None,
    lon: float | None = None,
):
    key = _address_cache_key(original_address)
    if not key:
        return
    now = datetime.now().isoformat()
    distance_meters_value = (
        int(distance_meters)
        if isinstance(distance_meters, (int, float)) and distance_meters != float("inf")
        else None
    )
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO address_cache (
                   address_key, original_address, canonical_address, city, confidence, warning,
                   distance_text, distance_meters, distance_source, geocode_status, updated_at,
                   lat, lon
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(address_key) DO UPDATE SET
                   original_address=excluded.original_address,
                   canonical_address=excluded.canonical_address,
                   city=excluded.city,
                   confidence=excluded.confidence,
                   warning=excluded.warning,
                   distance_text=excluded.distance_text,
                   distance_meters=excluded.distance_meters,
                   distance_source=excluded.distance_source,
                   geocode_status=excluded.geocode_status,
                   updated_at=excluded.updated_at,
                   lat=excluded.lat,
                   lon=excluded.lon""",
            (
                key,
                original_address,
                canonical_address,
                city,
                confidence,
                warning,
                distance_text,
                distance_meters_value,
                distance_source,
                geocode_status,
                now,
                lat,
                lon,
            ),
        )


def get_group_city_hint(group_url: str) -> list[str] | None:
    """Candidate cities inferred from a group's real FB name, last captured at scan time."""
    if not group_url:
        return None
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT cities_json FROM group_city_hints WHERE group_url = ?", (group_url,)
        ).fetchone()
    if not row or not row["cities_json"]:
        return None
    return json.loads(row["cities_json"])


def save_group_city_hint(group_url: str, group_name: str, cities: list[str]):
    if not group_url:
        return
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO group_city_hints (group_url, group_name, cities_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(group_url) DO UPDATE SET
                   group_name=excluded.group_name,
                   cities_json=excluded.cities_json,
                   updated_at=excluded.updated_at""",
            (group_url, group_name, json.dumps(cities, ensure_ascii=False), now),
        )


def get_last_scan_time(group_url: str) -> datetime | None:
    """When this group was last fully scanned, so a scan can stop scrolling
    once it reaches posts already covered last time."""
    if not group_url:
        return None
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT last_scanned_at FROM group_scan_state WHERE group_url = ?", (group_url,)
        ).fetchone()
    if not row or not row["last_scanned_at"]:
        return None
    return datetime.fromisoformat(row["last_scanned_at"])


def set_last_scan_time(group_url: str, when: datetime):
    if not group_url:
        return
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO group_scan_state (group_url, last_scanned_at)
               VALUES (?, ?)
               ON CONFLICT(group_url) DO UPDATE SET last_scanned_at=excluded.last_scanned_at""",
            (group_url, when.isoformat()),
        )


def get_reparse_candidates() -> list[dict]:
    """Posts rejected on price/rooms/distance/entry date, that failed to parse, or with no stated price — reparse-rejected candidates."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE verdict IN (?, ?, ?, ?, ?, ?)",
            (VERDICT_REJECTED_PRICE, VERDICT_REJECTED_ROOMS, VERDICT_PARSE_FAILED, VERDICT_PRICE_UNKNOWN,
             VERDICT_REJECTED_DISTANCE, VERDICT_REJECTED_ENTRY_DATE),
        ).fetchall()
    return [dict(row) for row in rows]


def get_owed_alert_urls(max_age_hours: int) -> list[str]:
    """
    URLs of listings recorded as added within the last max_age_hours whose
    Telegram alert never went out (no stored message id) — a send that failed
    on a network/API error, after the row was already safely in the sheet.
    """
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT url FROM posts WHERE verdict = ? AND telegram_message_id IS NULL "
            "AND last_processed >= ? ORDER BY last_processed",
            (VERDICT_ADDED, cutoff),
        ).fetchall()
    return [row["url"] for row in rows]


def get_added_listing_data() -> dict[str, dict]:
    """
    url -> {address, price_val, rooms_val, distance_text} for every added
    listing, as currently recorded. Used by dedupe_and_sort_sheet() to key
    duplicates off the bot's own original parse rather than the live sheet
    cells (so a manual edit to a sheet row, e.g. fixing a price, doesn't stop
    a later crosspost from being recognized as a duplicate), and to sync the
    sheet's address/distance cells if either was recomputed after the row was
    first added (e.g. a corner-address enrichment or a distance fix).
    """
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT url, address, price_val, rooms_val, distance_text FROM posts WHERE verdict = ?",
            (VERDICT_ADDED,),
        ).fetchall()
    return {row["url"]: dict(row) for row in rows}


def update_added_listing_address(url: str, address: str):
    """
    Overwrites the stored address for an already-added listing — used when a
    later crosspost of the same physical listing (same dedupe key) states a
    more specific address (e.g. adds a "פינת X" corner qualifier the original
    post lacked) than what got recorded originally. dedupe_and_sort_sheet()
    syncs the sheet's כתובת cell from this value on its next rewrite pass.
    """
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE posts SET address = ? WHERE url = ? AND verdict = ?",
            (address, url, VERDICT_ADDED),
        )


def get_all_posts() -> list[dict]:
    """Every post ever scanned, regardless of verdict — used by --replay (testing new code/prompt without a browser)."""
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM posts").fetchall()
    return [dict(row) for row in rows]


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def get_gmaps_usage(month: str | None = None) -> int:
    month = month or _current_month()
    with _lock, _connect() as conn:
        row = conn.execute("SELECT gmaps_calls FROM api_usage WHERE month = ?", (month,)).fetchone()
    return row["gmaps_calls"] if row else 0


def increment_gmaps_usage(month: str | None = None) -> int:
    """Atomically bumps this month's Maps call counter by 1 and returns the new total."""
    month = month or _current_month()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO api_usage (month, gmaps_calls) VALUES (?, 1)
               ON CONFLICT(month) DO UPDATE SET gmaps_calls = gmaps_calls + 1""",
            (month,),
        )
        row = conn.execute("SELECT gmaps_calls FROM api_usage WHERE month = ?", (month,)).fetchone()
    return row["gmaps_calls"]


def get_stats() -> tuple[dict, str, int]:
    """Returns (count by verdict, current month YYYY-MM, this month's Maps calls)."""
    month = datetime.now().strftime("%Y-%m")
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT verdict, COUNT(*) AS cnt FROM posts GROUP BY verdict").fetchall()
        usage_row = conn.execute("SELECT gmaps_calls FROM api_usage WHERE month = ?", (month,)).fetchone()
    counts = {row["verdict"]: row["cnt"] for row in rows}
    gmaps_calls = usage_row["gmaps_calls"] if usage_row else 0
    return counts, month, gmaps_calls


def get_group_stats() -> dict[str, dict[str, int]]:
    """group_url -> {verdict: count}, for the per-group yield breakdown in --stats
    (flags groups with zero VERDICT_ADDED as candidates to drop from TARGET_URLS)."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT group_url, verdict, COUNT(*) AS cnt FROM posts "
            "WHERE group_url IS NOT NULL AND group_url != '' "
            "GROUP BY group_url, verdict"
        ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(row["group_url"], {})[row["verdict"]] = row["cnt"]
    return result


def top_excluded_locations(limit: int = 8) -> list[tuple[str, int]]:
    """Most common excluded_location: name out of prefiltered posts' reject_reason
    — tells you what's actually tripping EXCLUDED_LOCATIONS, to help tune it."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT reject_reason, COUNT(*) AS cnt FROM posts "
            "WHERE verdict = ? AND reject_reason LIKE 'excluded_location:%' "
            "GROUP BY reject_reason ORDER BY cnt DESC",
            (VERDICT_PREFILTERED,),
        ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        location = row["reject_reason"].split(":", 1)[1]
        counts[location] = counts.get(location, 0) + row["cnt"]
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def top_rejected_distance_addresses(limit: int = 8) -> list[tuple[str, int]]:
    """Most common address among rejected_distance posts — tells you what's
    landing just outside MAX_WALKING_DISTANCE_KM, to help tune that or the
    geocoding table."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT address, COUNT(*) AS cnt FROM posts "
            "WHERE verdict = ? AND address IS NOT NULL AND address != '' "
            "GROUP BY address ORDER BY cnt DESC LIMIT ?",
            (VERDICT_REJECTED_DISTANCE, limit),
        ).fetchall()
    return [(row["address"], row["cnt"]) for row in rows]


def set_telegram_message(url: str, chat_id: str, message_id: str):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE posts SET telegram_chat_id = ?, telegram_message_id = ? WHERE url = ?",
            (str(chat_id), str(message_id), url),
        )


def get_telegram_message(url: str) -> tuple[str, str] | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT telegram_chat_id, telegram_message_id FROM posts WHERE url = ?", (url,)
        ).fetchone()
    if not row or not row["telegram_message_id"]:
        return None
    return row["telegram_chat_id"], row["telegram_message_id"]


def record_vote(url: str, voter_id: str, voter_name: str, vote: str):
    """Upsert — a voter can change their mind, only their latest vote counts."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO votes (url, voter_id, voter_name, vote, voted_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(url, voter_id) DO UPDATE SET
                   voter_name=excluded.voter_name,
                   vote=excluded.vote,
                   voted_at=excluded.voted_at""",
            (url, voter_id, voter_name, vote, now),
        )


def get_votes(url: str) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT voter_id, voter_name, vote, voted_at FROM votes WHERE url = ?", (url,)
        ).fetchall()
    return [dict(row) for row in rows]


def record_rating(url: str, voter_id: str, voter_name: str, score: int):
    """Upsert — a voter's latest 0-10 rating replaces their previous one for
    that listing, same "latest tap wins" rule as record_vote."""
    now = datetime.now().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO ratings (url, voter_id, voter_name, score, rated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(url, voter_id) DO UPDATE SET
                   voter_name=excluded.voter_name,
                   score=excluded.score,
                   rated_at=excluded.rated_at""",
            (url, voter_id, voter_name, score, now),
        )


def get_ratings(url: str) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT voter_id, voter_name, score, rated_at FROM ratings WHERE url = ?", (url,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_ratings_with_listing_data() -> list[dict]:
    """
    Every human 0-10 rating joined with that listing's stored fields
    (price/rooms/distance plus the full LLM parse in parsed_json, which has
    floor/elevator/parking/arnona/vaad/shelter/entry_date/is_agent) — the
    training set for eventually regressing compute_fit_score()'s weights
    against real human judgment instead of guessed weights.
    """
    with _lock, _connect() as conn:
        rows = conn.execute("""
            SELECT r.url, r.voter_id, r.voter_name, r.score, r.rated_at,
                   p.price_val, p.rooms_val, p.address, p.distance_meters, p.distance_text,
                   p.parsed_json
            FROM ratings r
            LEFT JOIN posts p ON p.url = r.url
        """).fetchall()
    return [dict(row) for row in rows]


# bot_state keys shared between the scan process, the Telegram listener and doctor.py.
SCANS_PAUSED_KEY = "scheduled_scans_paused_since"  # ISO time, "" = not paused
SCAN_PID_KEY = "scan_pid"  # "<run id>:<pid>" of the scan in progress, so /stop can end a scheduled one
SCAN_PROGRESS_KEY = "scan_progress"  # JSON {text, updated} — the progress line /status shows


def get_bot_state(key: str) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_bot_state(key: str, value: str):
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_telegram_offset() -> int:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM telegram_state WHERE key = 'update_offset'").fetchone()
    return int(row["value"]) if row else 0


def set_telegram_offset(offset: int):
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO telegram_state (key, value) VALUES ('update_offset', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(offset),),
        )


def prune_old_posts(max_age_days: int) -> int:
    """
    Bounds DB growth without breaking the should_skip() dedup guarantee:
    clears raw_text/parsed_json for posts older than max_age_days (by
    first_seen), but keeps the row itself (url, verdict, attempts, all
    analysis columns) forever — should_skip() only ever reads verdict/
    attempts, so a pruned post can never be rescanned or re-added. Only
    VACUUMs (reclaims file space) when something was actually pruned, so a
    day with nothing to prune doesn't pay for a full file rewrite.
    Returns the number of rows pruned.
    """
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with _lock, _connect() as conn:
        cursor = conn.execute(
            "UPDATE posts SET raw_text = NULL, parsed_json = NULL "
            "WHERE first_seen < ? AND raw_text IS NOT NULL",
            (cutoff,),
        )
        pruned = cursor.rowcount
    if pruned > 0:
        with _lock, _connect() as conn:
            conn.execute("VACUUM")
    return pruned


def start_run(mode: str, log_path: str = "") -> int:
    """Records a scrape starting (status "running" until finish_run) and returns its id —
    so a run that crashes or is killed still leaves a row behind."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, mode, status, log_path) VALUES (?, ?, 'running', ?)",
            (datetime.now().isoformat(timespec="seconds"), mode, log_path),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, counts: dict, warnings: int, errors: int):
    """Closes a run's row with its outcome. counts uses run_scraper()'s summary keys."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, groups_scanned = ?, groups_total = ?, posts_seen = ?, "
            "prefiltered = ?, llm_parsed = ?, added = ?, checkpoints = ?, warnings = ?, errors = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), status, counts.get("groups_scanned"), counts.get("total"),
             counts.get("posts_seen"), counts.get("prefiltered"), counts.get("llm_parsed"), counts.get("total_added"),
             counts.get("checkpoint_skipped"), warnings, errors, run_id),
        )


def get_recent_runs(limit: int = 10) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_last_successful_live_run() -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE mode = 'live' AND status = 'ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_run_activity(since: str) -> dict:
    """
    What a run did, read back from the posts it touched since `since` (ISO time): how many got each
    verdict, which models parsed them, and the listings it added — for the run-finished message.
    """
    with _lock, _connect() as conn:
        verdicts = conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM posts WHERE last_processed >= ? GROUP BY verdict", (since,)
        ).fetchall()
        models = conn.execute(
            "SELECT model_used, COUNT(*) AS n FROM posts WHERE last_processed >= ? "
            "AND model_used IS NOT NULL AND model_used NOT IN ('', 'none') GROUP BY model_used ORDER BY n DESC", (since,)
        ).fetchall()
        added = conn.execute(
            "SELECT url, group_url, price_val, rooms_val, address, distance_text FROM posts "
            "WHERE verdict = ? AND last_processed >= ? ORDER BY last_processed", (VERDICT_ADDED, since)
        ).fetchall()
    return {
        "verdicts": {row["verdict"]: row["n"] for row in verdicts},
        "models": {row["model_used"]: row["n"] for row in models},
        "added": [dict(row) for row in added],
    }


def get_added_per_day(days: int) -> dict[str, int]:
    """'YYYY-MM-DD' -> listings added that day, over the last `days` days (days with none are absent)."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT substr(last_processed, 1, 10) AS day, COUNT(*) AS n FROM posts "
            "WHERE verdict = ? AND last_processed >= ? GROUP BY day ORDER BY day", (VERDICT_ADDED, cutoff),
        ).fetchall()
    return {row["day"]: row["n"] for row in rows}


def get_recent_prefiltered(days: int) -> list[dict]:
    """Posts rejected by a content pre-filter rule in the last `days` days, with their stored text —
    the input for --stats' suspicious-rejects audit."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT url, reject_reason, raw_text FROM posts WHERE verdict = ? AND raw_text IS NOT NULL "
            "AND reject_reason IS NOT NULL AND first_seen >= ?",
            (VERDICT_PREFILTERED, cutoff),
        ).fetchall()
    return [dict(row) for row in rows]


def replace_groups(groups: list[dict]):
    """Mirrors the sheet's Groups tab: the table becomes exactly `groups` (url, name, cities, active,
    notes), in sheet order. A group removed from the tab is removed here too; its posts and stats stay."""
    now = datetime.now().isoformat(timespec="seconds")
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM groups")
        conn.executemany(
            "INSERT INTO groups (url, name, cities, active, notes, position, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(g["url"], g.get("name", ""), g.get("cities", ""), 1 if g.get("active", True) else 0,
              g.get("notes", ""), i, now) for i, g in enumerate(groups)],
        )


def get_groups() -> list[dict]:
    """The local mirror of the Groups tab, in sheet order — the fallback when the sheet can't be read."""
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT url, name, cities, active, notes FROM groups ORDER BY position").fetchall()
    return [dict(row, active=bool(row["active"])) for row in rows]


def get_group_overview() -> dict[str, dict]:
    """group_url -> {name, cities, posts, added, last_added, last_scanned, status} from what scans have recorded:
    the FB page title and inferred cities (group_city_hints), post/match counts, and scan baselines."""
    with _lock, _connect() as conn:
        hints = conn.execute("SELECT group_url, group_name, cities_json FROM group_city_hints").fetchall()
        counts = conn.execute(
            "SELECT group_url, COUNT(*) AS posts, SUM(verdict = ?) AS added, "
            "MAX(CASE WHEN verdict = ? THEN last_processed END) AS last_added "
            "FROM posts WHERE group_url IS NOT NULL AND group_url != '' GROUP BY group_url",
            (VERDICT_ADDED, VERDICT_ADDED),
        ).fetchall()
        scans = conn.execute("SELECT group_url, last_scanned_at FROM group_scan_state").fetchall()
        statuses = conn.execute("SELECT group_url, status FROM group_status").fetchall()
    overview: dict[str, dict] = {}
    for row in hints:
        overview.setdefault(row["group_url"], {}).update(
            name=row["group_name"] or "", cities=json.loads(row["cities_json"]) if row["cities_json"] else [])
    for row in counts:
        overview.setdefault(row["group_url"], {}).update(
            posts=row["posts"], added=row["added"] or 0, last_added=row["last_added"])
    for row in scans:
        overview.setdefault(row["group_url"], {})["last_scanned"] = row["last_scanned_at"]
    for row in statuses:
        overview.setdefault(row["group_url"], {})["status"] = row["status"]
    return overview


def set_group_status(group_url: str, status: str):
    """The latest scan outcome for a group: "ok", "not_member" (FB shows a Join button and no posts), "pending"
    (join requested), "no_posts", or "needs_login" (guest mode met Facebook's login wall)."""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO group_status (group_url, status, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(group_url) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
            (group_url, status, datetime.now().isoformat(timespec="seconds")),
        )
