"""
=== Browser-generic primitives ===

Chrome launch args, stealth patching, visibility checks, popup dismissal —
nothing Facebook-specific here. Used by fb_scraper.py; kept out of
apartment_bot.py so a browser-based source doesn't have to reach into a
script's private namespace for them.
"""
import json
import re
import sys

try:
    from playwright_stealth import stealth_sync, StealthConfig
except ImportError:
    stealth_sync = None
    StealthConfig = None


from config import STEALTH_ENABLED, BROWSER_LOCALE, WEBGL_VENDOR, WEBGL_RENDERER
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

_CHROME_VERSION_RE = re.compile(r'Chrome/(\d+)\.\d+\.\d+\.\d+')

# UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL enum values of WEBGL_debug_renderer_info
_WEBGL_SPOOF_JS = """(() => {
  const vendor = %s, renderer = %s;
  for (const proto of [WebGLRenderingContext.prototype, WebGL2RenderingContext.prototype]) {
    proto.getParameter = new Proxy(proto.getParameter, {
      apply(target, self, args) {
        if (args[0] === 37445) return vendor;
        if (args[0] === 37446) return renderer;
        return Reflect.apply(target, self, args);
      },
    });
  }
})();"""

# navigator.languages as headful Chrome reports it (["en-US", "en"]); Playwright's locale option alone yields just ["en-US"]
_LANGUAGES_JS = "Object.defineProperty(Object.getPrototypeOf(navigator), 'languages', {get: () => Object.freeze(%s), configurable: true});"

def _normalize_user_agent(user_agent: str) -> str:
    """
    Makes a headless UA match what headful Chrome sends: drops the "Headless" marker and
    applies Chrome's UA reduction (full build number -> "<major>.0.0.0"). The old headless
    shell still sends the full build (149.0.7827.55) where a real browser sends 149.0.0.0.
    """
    return _CHROME_VERSION_RE.sub(lambda match: f"Chrome/{match.group(1)}.0.0.0", user_agent.replace("HeadlessChrome", "Chrome"))

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
        # navigator_user_agent/navigator_languages OFF: the package fakes the UA with a
        # hardcoded Chrome 95 string — an ancient UA is a louder bot signal than what it
        # hides, and it would stomp the real-profile fingerprint the context was deliberately
        # given. Its languages patch ignores StealthConfig.languages and uses a randomly
        # generated Accept-Language instead (seen: ["en-IO", "*"]) — also worse than nothing.
        # Both verified against tf-playwright-stealth 1.2.0. Languages are set below from
        # BROWSER_LOCALE instead, matching the context's locale / Accept-Language header.
        # webgl_vendor OFF: the package's spoof is a random Mac/mobile GPU per run; the
        # WEBGL_* config pair below replaces it with one stable, Windows-consistent identity.
        stealth_sync(page, StealthConfig(navigator_user_agent=False, navigator_languages=False, webgl_vendor=False))
        page.add_init_script(_LANGUAGES_JS % json.dumps([BROWSER_LOCALE, BROWSER_LOCALE.split("-")[0]]))
        if WEBGL_VENDOR and WEBGL_RENDERER:
            page.add_init_script(_WEBGL_SPOOF_JS % (json.dumps(WEBGL_VENDOR), json.dumps(WEBGL_RENDERER)))
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
