"""
=== Browser-generic primitives ===

Chrome launch args, stealth patching, visibility checks, popup dismissal —
nothing Facebook-specific here. Both fb_scraper.py and yad2_scraper.py use
these; kept out of apartment_bot.py so a second source doesn't have to reach
into a script's private namespace for them.
"""
import sys

try:
    from playwright_stealth import stealth_sync, StealthConfig
except ImportError:
    stealth_sync = None
    StealthConfig = None


from config import STEALTH_ENABLED
from core.util import _safe_print

# On Linux, Chrome's sandbox can fail to initialize in some VM/container
# environments, hanging navigation indefinitely rather than erroring cleanly
# (verified 2026-07-18: a bare `google-chrome --no-sandbox --disable-gpu` CLI
# call loaded facebook.com in 11s, while Playwright's launch — which strips
# --no-sandbox for anti-detection reasons, fine on Windows — hung on
# page.goto() past a 30s timeout on every single group). Add both flags back
# on Linux only; Windows keeps the original anti-detection args untouched.
_CHROME_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--autoplay-policy=user-gesture-required"]
if sys.platform.startswith("linux"):
    _CHROME_LAUNCH_ARGS += ["--no-sandbox", "--disable-gpu"]

_stealth_warned = False

def _apply_stealth(page):
    """
    Masks automation fingerprints (navigator.webdriver, headless UA artifacts,
    missing chrome.runtime) via tf-playwright-stealth. Must run on a page
    BEFORE its first navigation — the patches are add_init_script based.
    Failure is never fatal: the bot ran for months without stealth.
    """
    global _stealth_warned
    if not STEALTH_ENABLED:
        return
    if stealth_sync is None:
        if not _stealth_warned:
            _stealth_warned = True
            _safe_print("WARNING: tf-playwright-stealth not installed — running without stealth patches. Run: pip install -r requirements.txt")
        return
    try:
        # navigator_user_agent/navigator_languages OFF: the package fakes them with a
        # hardcoded Chrome 95 UA and en-US — an ancient UA is a louder bot signal than
        # what it hides, and it would stomp the real-profile fingerprint the context
        # was deliberately given (verified 2026-07-18 against tf-playwright-stealth 1.2.0).
        stealth_sync(page, StealthConfig(navigator_user_agent=False, navigator_languages=False))
    except Exception as e:
        if not _stealth_warned:
            _stealth_warned = True
            _safe_print(f"WARNING: stealth patch failed, continuing without it: {e}")

def _is_visible(locator) -> bool:
    try:
        return locator.first.is_visible()
    except Exception:
        return False

def _dismiss_popups(page):
    for selector in [
        'div[aria-label="Close"]',
        'div[aria-label="סגירה"]',
        'button:has-text("Not Now")',
        'button:has-text("לא עכשיו")',
    ]:
        try:
            page.locator(selector).first.click(timeout=2000)
        except Exception:
            pass
