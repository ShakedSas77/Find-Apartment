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
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot_data.db"

VERDICT_ADDED = "added"
VERDICT_REJECTED_PRICE = "rejected_price"
VERDICT_REJECTED_ROOMS = "rejected_rooms"
VERDICT_REJECTED_DISTANCE = "rejected_distance"
VERDICT_PREFILTERED = "prefiltered"
VERDICT_PARSE_FAILED = "parse_failed"
VERDICT_PRICE_UNKNOWN = "price_unknown"

ALL_VERDICTS = [
    VERDICT_ADDED, VERDICT_REJECTED_PRICE, VERDICT_REJECTED_ROOMS,
    VERDICT_REJECTED_DISTANCE, VERDICT_PREFILTERED, VERDICT_PARSE_FAILED,
    VERDICT_PRICE_UNKNOWN,
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
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                conn.execute(statement)


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
                       model_used=?
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
                    url,
                ),
            )
        else:
            attempts = 1 if verdict == VERDICT_PARSE_FAILED else 0
            conn.execute(
                """INSERT INTO posts (
                       url, group_url, raw_text, parsed_json, verdict, attempts, first_seen, last_processed,
                       price_val, rooms_val, address, address_confidence, distance_text, distance_meters,
                       post_date, reject_reason, model_used
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                   distance_text, distance_meters, distance_source, geocode_status, updated_at
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                   updated_at=excluded.updated_at""",
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
            ),
        )


def get_reparse_candidates() -> list[dict]:
    """Posts rejected on price/rooms/distance, that failed to parse, or with no stated price — reparse-rejected candidates."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE verdict IN (?, ?, ?, ?, ?)",
            (VERDICT_REJECTED_PRICE, VERDICT_REJECTED_ROOMS, VERDICT_PARSE_FAILED, VERDICT_PRICE_UNKNOWN, VERDICT_REJECTED_DISTANCE),
        ).fetchall()
    return [dict(row) for row in rows]


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
