"""
=== backup_db ===

Timestamped backup of bot_data.db — the sole record of dedup state (which
posts have already been seen/rejected) and the raw-text archive that powers
--replay/--reparse-rejected. No backup existed before this; a corrupted file
or a bad migration would silently lose all of it.

    python backup_db.py

Every live scrape also calls backup_if_stale() at the end, so a backup happens
automatically once the newest one is older than config.BACKUP_MAX_AGE_DAYS.
Backups live in backups/ (gitignored); newest KEEP kept, older pruned.
"""

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

import storage
from config import BACKUP_MAX_AGE_DAYS

BACKUP_DIR = Path(__file__).parent / "backups"
KEEP = 14


def _prune(keep: int = KEEP) -> int:
    files = sorted(BACKUP_DIR.glob("bot_data-*.db"))
    old = files[:-keep] if keep > 0 else files
    for f in old:
        try:
            f.unlink()
        except Exception as exc:
            print(f"[backup] could not remove {f.name}: {exc}")
    return len(old)


def backup():
    if not storage.DB_PATH.exists():
        print("[backup] no bot_data.db yet — nothing to back up")
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"bot_data-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(storage.DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)  # SQLite's online backup API — consistent snapshot even mid-write
    finally:
        dst.close()
        src.close()
    pruned = _prune()
    print(f"[backup] wrote {dest.name} (kept {KEEP}, pruned {pruned})")
    return dest


def backup_if_stale(max_age_days: int = BACKUP_MAX_AGE_DAYS):
    """Runs backup() unless the newest snapshot is younger than max_age_days. Returns the new path, or None if skipped."""
    existing = sorted(BACKUP_DIR.glob("bot_data-*.db"))
    if existing:
        newest_age = datetime.now() - datetime.fromtimestamp(existing[-1].stat().st_mtime)
        if newest_age < timedelta(days=max_age_days):
            return None
    return backup()


if __name__ == "__main__":
    backup()
