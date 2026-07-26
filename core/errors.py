"""
=== Shared signal exceptions ===

Zero-import leaf. Both exceptions are raised in one module and caught in
another (GmapsQuotaHalted: maps.py raises, fb_scraper.py/apartment_bot.py
catch. HeadlessCheckpointAbort: fb_scraper.py raises, core/evaluate.py
catches), so they need a home neither side owns to avoid a circular import.
"""


class GmapsQuotaHalted(Exception):
    """Raised when GMAPS_ON_CAP == 'halt' and the monthly quota has been reached — stops the entire run."""


class HeadlessCheckpointAbort(Exception):
    """Raised when a checkpoint/CAPTCHA is detected in headless mode — can't be solved manually, so this group is stopped."""
