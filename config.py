"""
=== Apartment Bot Settings ===

Edit the values below to tailor the scan to your needs.
"""

# ─── API keys ─────────────────────────────────────────────────────────────────
# Google Sheets – Service Account JSON key (downloaded from Google Cloud Console)
CREDENTIALS_FILE = "credentials.json"

# Google Sheets – spreadsheet ID (from the sheet's URL). Moved to .env (SHEET_ID=...)
# since the repo is public — don't put it back here. Loaded via os.getenv in apartment_bot.py.

# Google Maps – API key for Distance Matrix
# Create one at: https://console.cloud.google.com/apis/credentials

# Gemini LLM – API key for parsing listings
# Create one at: https://aistudio.google.com/app/apikey

# Gemini LLM – model name. Dated snapshot names (gemini-2.5-flash, gemini-2.5-flash-lite)
# get pulled from new users without warning (404, verified 2026-07-18). "-latest"
# is an alias that auto-updates to the active version and isn't blocked the same way.
# gemini-flash-latest is the higher-quality alternative (smaller free-tier quota).
GEMINI_MODEL = "gemini-flash-lite-latest"

# ─── Target locations ────────────────────────────────────────────────────────────
# Facebook groups to scan — placeholder examples, replace with groups you're a member of
# (each group's URL is in the browser address bar when it's open). See README for details.
TARGET_URLS = [
    "https://www.facebook.com/groups/000000000000001",
    "https://www.facebook.com/groups/000000000000002",
    "https://www.facebook.com/groups/000000000000003",
]

# ─── Locations ────────────────────────────────────────────────────────────────
# Display only, on the startup banner — not used for filtering.
LOCATIONS = [
    "רמת גן", "רמת-גן", 'ר"ג', "ר״ג",
    "גבעתיים",
]

# ─── Criteria (AI-based filtering) ───────────────────────────────────────────────
MIN_ROOMS = 3.0
MAX_ROOMS = 3.5
MIN_PRICE = 5500
MAX_PRICE = 6700

# A post that passed every filter (rooms/address/distance) but never stated a price
# in the text at all (both the LLM and the regex "second chance" found nothing) —
# usually "contact for details," not a real rejection.
# True = added to the sheet with a blank price cell; False = rejected (verdict price_unknown)
# like an unsuitable price.
INCLUDE_PRICE_UNKNOWN = False


# Posts older than this are skipped. The sheet still displays DD/MM, but filtering
# internally uses the full date.
MAX_POST_AGE_DAYS = 21

# Scheduled-run log files (logs/run_*.log) older than this get deleted by run_scheduled.bat.
LOG_RETENTION_DAYS = 14

# ─── Pre-filters (before the AI) ──────────────────────────────────────────────────
# The post must contain at least one of these phrases to pass the check (saves a lot of time)
ROOMS_PRE_FILTER_REGEX = r'(?<!\d)(3|3\.5)\s*חד|שלוש[ה]?\s*חד|שלוש[ה]?\s*וחצי\s*חד|(?<!\d)3\s*וחצי\s*חד'

# Words that automatically disqualify a post:
NEGATIVE_KEYWORDS = r'סאבלט|סטודיו|קליניקה|מחפש|מחפשת|מחפשים|מחפשות'

# 'שותפ'/'שותף' (roommate) is handled separately: disqualifies roommate posts, unless
# 'זוג' (couple) is mentioned nearby — e.g. a landlord describing tenant-type flexibility
# ("suitable for a couple or 2 roommates") — see _ROOMMATE_COUPLE_EXCEPTION_RE
# in apartment_bot.py. [פף] also covers the singular form "שותף" (final פ) as well as
# שותפה/שותפים/שותפות (feminine/plural/collective forms).
ROOMMATE_KEYWORDS = r'שות[פף]'

# Locations we want to instantly disqualify (e.g. not in Tel Aviv/Ramat Gan)
EXCLUDED_LOCATIONS = ["בני ברק"]

# ─── Stability ──────────────────────────────────────────────────────────────────
# Consecutive Gemini errors (not 429 quota) before permanently switching to Ollama
GEMINI_MAX_CONSECUTIVE_ERRORS = 3
# Number of manual login attempts before exiting
LOGIN_MAX_ATTEMPTS = 5

# Prompt instruction language (not the output language — that's always Hebrew): "en" or "hebrew".
# Lets you A/B the two prompt variants in prompts.py.
PROMPT_LANGUAGE = "en"

# ─── Distance ────────────────────────────────────────────────────────────────────
# Destination address to compute walking distance from every listing (display only — not a filter)
DESTINATION_ADDRESS = "רחוב הדוגמה 1, תל אביב, ישראל"

# Maximum walking distance in km. Listings farther than this are filtered out. Set to 99.0 to disable filtering.
MAX_WALKING_DISTANCE_KM = 4.0

# Cities allowed as a result when validating an address against Google Geocoding
GMAPS_TARGET_CITIES = ["רמת גן", "גבעתיים", "תל אביב-יפו", "תל אביב"]

# Whether to validate addresses against Geocoding before Distance Matrix.
# True is recommended: prevents misleading distances from vague/incorrect addresses.
GMAPS_VALIDATE_ADDRESSES = True

# Whether to compute distance only for addresses with good confidence.
# True = don't waste Distance Matrix calls on a city-only or weak address.
GMAPS_DISTANCE_ONLY_CONFIDENT_ADDRESS = True

# Monthly Distance Matrix call quota (safety margin below the free tier's ~10,000 elements/month).
# Over the cap -> behavior per GMAPS_ON_CAP. The counter resets itself every month (keyed by YYYY-MM).
GMAPS_MONTHLY_CAP = 9000
# "skip" — stop computing distance (the distance column keeps a placeholder), but keep scanning and adding to the sheet.
# "halt" — stop the entire run so as to never exceed the cap.
GMAPS_ON_CAP = "skip"

# ─── Scrolling ──────────────────────────────────────────────────────────────────
SCROLL_COUNT = 20
SCROLL_DELAY_MS = 1000

# ─── Anti-detection ─────────────────────────────────────────────────────────────
# Applies tf-playwright-stealth patches to every page (hides navigator.webdriver,
# headless UA artifacts, missing chrome.runtime). Kill switch in case a stealth
# patch ever breaks Facebook rendering — the bot runs fine without it.
STEALTH_ENABLED = True

# At the start of every run, visit every URL already in the sheet and remove any
# whose post Facebook now shows as unavailable (deleted, or visibility changed).
# Kill switch in case this ever needs to be turned off without a code change.
PRUNE_DEAD_LINKS_ENABLED = True

# ─── Concurrency ────────────────────────────────────────────────────────────────
# How many groups scan simultaneously. Higher = faster, but also higher risk of a
# block/CAPTCHA: parallel mode (>1) opens a separate browser+context per group with
# injected cookies — several simultaneous browsers from one account, a different
# fingerprint than the real profile. If checkpoints start happening, the safest
# value is 1: true sequential mode, scanning group by group on the same page inside
# the real profile (chrome_profile/), never exporting storage_state at all.
MAX_CONCURRENT_GROUPS = 8

# ─── Google Sheets ─────────────────────────────────────────────────────────────
SHEET_HEADERS = [
    "לינק למודעה", "מחיר", "חדרים", "מרחק הליכה (ק\"מ)", "תאריך כניסה",
    "קומה", "מעלית", "חניה", "ארנונה (לחודשיים)", "ועד בית (לחודשיים)", "ממ\"ד/מקלט", "תיווך/פרטי",
    "תאריך פרסום", "כתובת", "זמן סריקה"
]
