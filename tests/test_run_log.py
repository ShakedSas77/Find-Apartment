"""
Tests for run logging: the per-run log file (tee + problem counting), the runs
table, and doctor's stale-run check. Storage is a real temp DB; the clock is
not faked — run ages are set by writing started_at directly.
"""
import io
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

import config
import doctor
from core import run_log
from core import storage


@pytest.fixture
def tee(tmp_path):
    """A _Tee over an in-memory console, without touching the real sys.stdout."""
    console = io.StringIO()
    log_path = tmp_path / "bot_test.log"
    log_file = open(log_path, "a", encoding="utf-8")
    recorder = run_log.RunLog(log_path)
    stream = run_log._Tee(console, log_file, recorder, threading.Lock())
    yield stream, console, log_path, recorder
    log_file.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


def test_console_output_is_unchanged(tee):
    stream, console, _, _ = tee

    stream.write("Scanning Group 1/33\n")

    assert console.getvalue() == "Scanning Group 1/33\n"


def test_log_file_lines_get_a_time_prefix(tee):
    stream, _, log_path, _ = tee

    stream.write("Scanning Group 1/33\n")

    assert log_path.read_text(encoding="utf-8").startswith(f"[{datetime.now():%H:}")


def test_partial_writes_are_joined_into_one_log_line(tee):
    stream, _, log_path, _ = tee

    stream.write("Found ")
    stream.write("12 real posts.\n")

    assert log_path.read_text(encoding="utf-8").split("] ", 1)[1] == "Found 12 real posts.\n"


def test_error_and_warning_lines_are_counted_including_group_prefixed_ones(tee):
    stream, _, _, recorder = tee

    stream.write("    ERROR: local Ollama analysis failed\nWARNING: [Group 3/33] Telegram alert failed\n[Group 2/33] fine\n")

    assert (recorder.errors, recorder.warnings) == (1, 1)


def test_old_bot_logs_are_pruned(tmp_path):
    old = tmp_path / "bot_2026-01-01_000000.log"
    old.write_text("x")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))

    run_log._prune_old_logs(tmp_path, retention_days=14)

    assert not old.exists()


def test_a_finished_ok_live_run_is_the_last_successful_one(db):
    run_id = storage.start_run("live")
    storage.finish_run(run_id, "ok", {"groups_scanned": 33, "total": 33, "total_added": 2}, warnings=1, errors=0)

    assert storage.get_last_successful_live_run()["added"] == 2


def test_a_dry_run_never_counts_as_a_successful_live_run(db):
    run_id = storage.start_run("dry_run")
    storage.finish_run(run_id, "ok", {}, warnings=0, errors=0)

    assert storage.get_last_successful_live_run() is None


def test_doctor_fails_when_the_last_live_run_is_stale(db):
    run_id = storage.start_run("live")
    storage.finish_run(run_id, "ok", {}, warnings=0, errors=0)
    old = (datetime.now() - timedelta(hours=config.RUN_STALE_HOURS + 1)).isoformat(timespec="seconds")
    with storage._connect() as conn:
        conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (old, run_id))

    assert doctor._check_last_run()[1] == doctor.FAIL


def test_doctor_passes_after_a_recent_live_run(db):
    storage.finish_run(storage.start_run("live"), "ok", {}, warnings=0, errors=0)

    assert doctor._check_last_run()[1] == doctor.PASS
