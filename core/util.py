"""
=== Shared low-level utilities ===

Dependency-free sink: imports nothing project-local, ever. That invariant is
what keeps this module safe to import from anywhere (evaluate, sheets, maps,
fb_scraper, yad2_scraper) without ever risking a circular import.
"""
import threading
import time

_print_lock = threading.Lock()
_sheet_lock = threading.Lock()  # guards seen_urls reads/writes AND sheet writes together
_checkpoint_lock = threading.Lock()
_resume_event = threading.Event()
_resume_event.set()  # set = running; cleared = paused for a checkpoint on some tab


def _safe_print(msg: str):
    with _print_lock:
        print(msg)


def _with_retries(fn, attempts: int = 3, base_delay: float = 1.0):
    """Runs fn up to `attempts` times on failure, with increasing delay between attempts. Returns/raises on the last attempt."""
    last_err = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
    if last_err:
        raise last_err
    raise RuntimeError("Function failed repeatedly without capturing an exception")
