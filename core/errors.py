"""
=== Shared signal exceptions ===

Zero-import leaf. Both exceptions are raised in one module and caught in
another (GmapsQuotaHalted: maps.py raises, fb_scraper.py catches.
HeadlessCheckpointAbort: raised and caught in fb_scraper.py), so they need a
home neither side owns to avoid a circular import.
"""


class GmapsQuotaHalted(Exception):
    """Raised when GMAPS_ON_CAP == 'halt' and the monthly quota has been reached — stops the entire run."""


class LoginRequired(Exception):
    """Raised when Facebook shows the login form and no login is possible (headless, or manual attempts exhausted) — the CLI records the run as login_required, not as a crash."""


class HeadlessCheckpointAbort(Exception):
    """Raised when a checkpoint/CAPTCHA is detected in headless mode — can't be solved manually, so this group is stopped."""
