"""
backup_if_stale() against a throwaway DB and backup dir (filesystem only, no real bot_data.db touched).
"""
import os
import sqlite3
import time

import pytest

import backup_db
import storage


@pytest.fixture
def temp_backup_env(tmp_path, monkeypatch):
    db_path = tmp_path / "bot_data.db"
    sqlite3.connect(db_path).execute("create table posts (url text)").connection.close()
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "backups")
    return tmp_path / "backups"


def test_backup_is_written_when_none_exist(temp_backup_env):
    backup_db.backup_if_stale(max_age_days=7)

    assert len(list(temp_backup_env.glob("bot_data-*.db"))) == 1


def test_backup_is_skipped_when_newest_is_fresh(temp_backup_env):
    backup_db.backup_if_stale(max_age_days=7)

    assert backup_db.backup_if_stale(max_age_days=7) is None


def test_backup_is_written_when_newest_is_older_than_threshold(temp_backup_env):
    first = backup_db.backup_if_stale(max_age_days=7)
    ten_days_ago = time.time() - 10 * 86400
    os.utime(first, (ten_days_ago, ten_days_ago))

    assert backup_db.backup_if_stale(max_age_days=7) is not None
