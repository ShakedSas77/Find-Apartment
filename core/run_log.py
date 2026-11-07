"""
=== Per-run log file ===

Every scrape writes its own log under logs/, however it was started (scheduled
task, run_bot.bat, the IDE, a bare `python apartment_bot.py`). Before this only
run_scheduled.bat redirected output to a file, so manual runs left nothing
behind to audit.

start_run_log() tees sys.stdout/sys.stderr: the console sees output unchanged,
the file gets each line prefixed with [HH:MM:SS]. Lines starting with the
project's ERROR:/WARNING: prefixes are counted, and the first few kept, for the
end-of-run summary and the runs table.
"""
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

_FIRST_PROBLEMS_KEPT = 5
_PROBLEM_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(ERROR|WARNING):")


# The active run's log, so code deep in a run (the Telegram run-finished alert) can report its counts.
current: "RunLog | None" = None


class RunLog:
    """Counts and samples the WARNING:/ERROR: lines written while the tee is active."""

    def __init__(self, path: Path):
        self.path = path
        self.warnings = 0
        self.errors = 0
        self.first_problems: list[str] = []

    def note_line(self, line: str):
        match = _PROBLEM_RE.match(line)
        if not match:
            return
        if match.group(1) == "ERROR":
            self.errors += 1
        else:
            self.warnings += 1
        if len(self.first_problems) < _FIRST_PROBLEMS_KEPT:
            self.first_problems.append(line.strip()[:200])

    def summary(self) -> str:
        text = f"Log: {self.path} — {self.errors} error(s), {self.warnings} warning(s)."
        if self.first_problems:
            text += "\n" + "\n".join(f"  {line}" for line in self.first_problems)
        return text


class _Tee:
    def __init__(self, original, log_file, run_log: RunLog, lock: threading.Lock):
        self._original = original
        self._file = log_file
        self._run_log = run_log
        self._lock = lock
        self._partial = ""

    def write(self, text: str) -> int:
        self._original.write(text)
        with self._lock:
            self._partial += text
            *lines, self._partial = self._partial.split("\n")
            for line in lines:
                self._file.write(f"[{datetime.now():%H:%M:%S}] {line}\n")
                self._run_log.note_line(line)
            self._file.flush()
        return len(text)

    def flush(self):
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


def start_run_log(log_dir: str, retention_days: int) -> RunLog:
    """Starts teeing stdout/stderr into log_dir/bot_<timestamp>.log; prunes this tool's older logs."""
    directory = Path(log_dir)
    directory.mkdir(exist_ok=True)
    _prune_old_logs(directory, retention_days)
    path = directory / f"bot_{datetime.now():%Y-%m-%d_%H%M%S}.log"
    log_file = open(path, "a", encoding="utf-8")
    global current
    run_log = RunLog(path)
    current = run_log
    lock = threading.Lock()
    sys.stdout = _Tee(sys.stdout, log_file, run_log, lock)
    sys.stderr = _Tee(sys.stderr, log_file, run_log, lock)
    return run_log


def _prune_old_logs(directory: Path, retention_days: int):
    cutoff = datetime.now() - timedelta(days=retention_days)
    for old in directory.glob("bot_*.log"):
        try:
            if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
        except OSError:
            pass
