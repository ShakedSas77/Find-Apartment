"""
Imports a Netscape-format cookies.txt (e.g. exported via the "Get cookies.txt
LOCALLY" Chrome extension) into yad2_profile/, Playwright's persistent
browser profile for the Yad2 scraper.

Why this exists: yad2.co.il sits behind Radware bot-protection that blocks
Playwright-driven Chrome outright (detects the CDP automation itself, not
mouse/click behavior) — confirmed by testing headful, with channel="chrome",
with stealth patches, and with real manual interaction inside the automated
window; all still show a fresh Radware challenge. The only path through is
cookies from a genuinely non-automated Chrome session: solve the challenge by
hand in your everyday browser, export cookies, import them here once.

Usage:
    python import_yad2_cookies.py path/to/yad2_cookies.txt
"""
import sys

from playwright.sync_api import sync_playwright

from browser import _apply_stealth, _CHROME_LAUNCH_ARGS, _is_visible

PROFILE_DIR = "yad2_profile"
VERIFY_URL = "https://www.yad2.co.il/realestate/rent/tel-aviv-area"


def parse_netscape_cookies(path: str) -> list[dict]:
    cookies = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            domain, _include_subdomains, path_, secure, expires, name, value = parts
            cookie = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path_,
                "secure": secure.upper() == "TRUE",
                "httpOnly": http_only,
            }
            expires_int = int(expires) if expires.isdigit() else 0
            if expires_int > 0:
                cookie["expires"] = expires_int
            cookies.append(cookie)
    return cookies


def main():
    if len(sys.argv) != 2:
        print("Usage: python import_yad2_cookies.py path/to/yad2_cookies.txt")
        sys.exit(1)

    cookies = parse_netscape_cookies(sys.argv[1])
    yad2_cookies = [c for c in cookies if "yad2.co.il" in c["domain"]]
    if not yad2_cookies:
        print(f"ERROR: no yad2.co.il cookies found in {sys.argv[1]}.")
        sys.exit(1)
    print(f"Parsed {len(yad2_cookies)} yad2.co.il cookie(s).")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel="chrome",
            headless=False,
            viewport={"width": 1366, "height": 1600},
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=_CHROME_LAUNCH_ARGS,
        )
        context.add_cookies(yad2_cookies)
        print(f"Injected cookies into {PROFILE_DIR}/.")

        page = context.pages[0] if context.pages else context.new_page()
        _apply_stealth(page)
        page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        blocked = _is_visible(page.get_by_text("Verifying your browser", exact=False))
        print("Still blocked by Radware:" if blocked else "Not blocked —", "title:", page.title())
        page.screenshot(path="yad2_cookie_import_verify.png", full_page=True)
        print("Saved yad2_cookie_import_verify.png")

        context.close()


if __name__ == "__main__":
    main()
