"""
=== שכבת התמדה (SQLite) ===

זיכרון מקומי לכל פוסט שנסרק אי-פעם — לא רק ההתאמות שמגיעות לגיליון. חוסך
LLM calls חוזרים על פוסטים שכבר נדחו/נפסלו, ומאפשר לשחזר את הטקסט הגולמי
שלהם לצורך איטרציה על הפרומפט (--reparse-rejected) בלי לפתוח דפדפן.

הגיליון (Google Sheet) נשאר מקור האמת להתאמות בפועל; ה-DB הזה הוא הזיכרון
של כל השאר (נדחה/סונן-מראש/נכשל).
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
VERDICT_PREFILTERED = "prefiltered"
VERDICT_PARSE_FAILED = "parse_failed"

ALL_VERDICTS = [
    VERDICT_ADDED, VERDICT_REJECTED_PRICE, VERDICT_REJECTED_ROOMS,
    VERDICT_PREFILTERED, VERDICT_PARSE_FAILED,
]

# מספר ניסיונות LLM מקסימלי לפוסט שנכשל בפרסור, לפני שמוותרים עליו לצמיתות
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


def should_skip(url: str) -> bool:
    """
    True אם הפוסט כבר עובד וניתן לדלג עליו: כל verdict חוץ מ-parse_failed
    (שמנסה שוב אוטומטית עד MAX_PARSE_ATTEMPTS פעמים).
    """
    with _lock, _connect() as conn:
        row = conn.execute("SELECT verdict, attempts FROM posts WHERE url = ?", (url,)).fetchone()
    if not row:
        return False
    if row["verdict"] != VERDICT_PARSE_FAILED:
        return True
    return row["attempts"] >= MAX_PARSE_ATTEMPTS


def record_post(url: str, group_url: str, raw_text: str, verdict: str, parsed_data: dict | None = None):
    now = datetime.now().isoformat()
    parsed_json = json.dumps(parsed_data, ensure_ascii=False) if parsed_data is not None else None
    with _lock, _connect() as conn:
        existing = conn.execute("SELECT first_seen, attempts FROM posts WHERE url = ?", (url,)).fetchone()
        if existing:
            attempts = existing["attempts"] + 1 if verdict == VERDICT_PARSE_FAILED else existing["attempts"]
            conn.execute(
                """UPDATE posts SET group_url=?, raw_text=?, parsed_json=?, verdict=?, attempts=?, last_processed=?
                   WHERE url=?""",
                (group_url, raw_text, parsed_json, verdict, attempts, now, url),
            )
        else:
            attempts = 1 if verdict == VERDICT_PARSE_FAILED else 0
            conn.execute(
                """INSERT INTO posts (url, group_url, raw_text, parsed_json, verdict, attempts, first_seen, last_processed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, group_url, raw_text, parsed_json, verdict, attempts, now, now),
            )


def get_reparse_candidates() -> list[dict]:
    """פוסטים שנדחו על מחיר/חדרים או שנכשלו בפרסור — מועמדים ל-reparse-rejected."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE verdict IN (?, ?, ?)",
            (VERDICT_REJECTED_PRICE, VERDICT_REJECTED_ROOMS, VERDICT_PARSE_FAILED),
        ).fetchall()
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
    """מחזירה (ספירה לפי verdict, חודש נוכחי YYYY-MM, קריאות Maps החודש)."""
    month = datetime.now().strftime("%Y-%m")
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT verdict, COUNT(*) AS cnt FROM posts GROUP BY verdict").fetchall()
        usage_row = conn.execute("SELECT gmaps_calls FROM api_usage WHERE month = ?", (month,)).fetchone()
    counts = {row["verdict"]: row["cnt"] for row in rows}
    gmaps_calls = usage_row["gmaps_calls"] if usage_row else 0
    return counts, month, gmaps_calls
