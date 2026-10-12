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

# Gemini LLM – model fallback chain, quality-first. Dated snapshot names (gemini-2.5-flash,
# gemini-2.5-flash-lite) get pulled from new users without warning (404, verified 2026-07-18).
# "-latest" is an alias that auto-updates to the active version and isn't blocked the same way.
# On a model-level error (404/quota, or GEMINI_MAX_CONSECUTIVE_ERRORS transient errors like a
# 503 "high demand"), core/llm.py advances to the next entry instead of dropping straight to the
# local Ollama fallback — a transient Gemini outage is usually model-specific, not account-wide.
# Verified callable against this key 2026-08-04 (client.models.list() lists names the key can't
# actually call, e.g. gemini-3.5-flash-lite/gemini-flash-lite-latest were both 503ing that day —
# re-probe with a real generate_content call, not just list(), before trusting a new entry here).
GEMINI_MODELS = [
    "gemini-3.6-flash",       # newest/strongest verified — primary
    "gemini-3.5-flash",
    "gemini-flash-latest",    # higher-quality alias, smaller free-tier quota
    "gemini-3.1-flash-lite",  # lightest — last resort before Ollama
]

# ─── Target locations ────────────────────────────────────────────────────────────
# Facebook groups to scan — placeholder examples, replace with groups you're a member of
# (each group's URL is in the browser address bar when it's open). See README for details.
# Real group URLs go in config_local.py (gitignored) instead of here — the repo is
# public, and TARGET_URLS reveals which groups/areas you're monitoring.
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
MIN_ROOMS = 2.5
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
MAX_POST_AGE_DAYS = 14

# Scheduled-run log files (logs/run_*.log) older than this get deleted by run_scheduled.bat.
LOG_RETENTION_DAYS = 14

# ─── Pre-filters (before the AI) ──────────────────────────────────────────────────
# The post must contain at least one of these phrases to pass the check (saves a lot of time)
ROOMS_PRE_FILTER_REGEX = r'(?<!\d)(2\.5|3|3\.5)\s*חד|שלוש[ה]?\s*חד|שלוש[ה]?\s*וחצי\s*חד|שתיים\s*וחצי\s*חד|(?<!\d)[23]\s*וחצי\s*חד'

# Words that automatically disqualify a post:
NEGATIVE_KEYWORDS = r'סאבלט|סטודיו|קליניקה|מחפש|מחפשת|מחפשים|מחפשות|לא רלוונטי'

# 'שותפ'/'שותף' (roommate) is handled separately: disqualifies roommate posts, unless
# 'זוג' (couple) is mentioned nearby — e.g. a landlord describing tenant-type flexibility
# ("suitable for a couple or 2 roommates") — see _ROOMMATE_COUPLE_EXCEPTION_RE
# in apartment_bot.py. [פף] also covers the singular form "שותף" (final פ) as well as
# שותפה/שותפים/שותפות (feminine/plural/collective forms).
ROOMMATE_KEYWORDS = r'שות[פף]'

# Locations we want to instantly disqualify (e.g. not in Tel Aviv/Ramat Gan)
EXCLUDED_LOCATIONS = ["בני ברק", "נס ציונה", "אור יהודה", "חולון", "פתח תקווה", "מרום נווה", "עוזיאל", "נגבה", "נווה שאנן"]

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
MAX_WALKING_DISTANCE_KM = 5.0

# Listings south of this latitude are filtered out. Only applied when the
# address geocodes precisely (GMAPS_VALIDATE_ADDRESSES must be on) — an
# address that can't be geocoded confidently is never rejected on this basis.
# Set to None to disable. Default: Tel Aviv HaHagana rail station
# (32.054062, 34.7847899), confirmed via Google Geocoding directly.
EXCLUDE_SOUTH_OF_LAT = 32.054062
# NOTE: an address naming one of FOCUS_NEIGHBORHOODS_TEL_AVIV (defined further down) is
# exempt from this cutoff — several of them sit near or below that latitude.

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
# Hard upper bound on scroll iterations per group — a safety cap for when a group's
# post dates are unavailable/unparseable and early-stop can never trigger, not the
# normal case (see MIN_SCROLLS_BEFORE_EARLY_STOP / CONSECUTIVE_OLD_POSTS_TO_STOP).
SCROLL_COUNT = 30
# Scroll cap for a group with no saved last-scan time. Its early-stop cutoff is the full
# MAX_POST_AGE_DAYS lookback, which a busy group can't reach in SCROLL_COUNT scrolls (~1.5
# new posts each); without a higher cap it never catches up, never saves a baseline, and
# re-crawls the same range every run. One deep backfill sets the baseline.
FIRST_SCAN_SCROLL_COUNT = 80
SCROLL_DELAY_MS = 1000
# Per-scroll wheel distance range. Kept near the viewport height (1600px): Facebook only
# renders posts close to the viewport and loads new ones lazily, so a step far beyond it
# (the old 3000-5000) outruns the feed and stalls.
SCROLL_STEP_MIN_PX = 1200
SCROLL_STEP_MAX_PX = 2000

# Bounded poll after a "See more" click, replacing a blind fixed sleep — returns as
# soon as the click visibly took effect instead of always waiting the full ceiling.
# Local DOM re-checks over the existing CDP connection only, no extra Facebook
# requests — carries no anti-bot signal, only affects local pacing.
SEE_MORE_SETTLE_POLL_MS = 100
SEE_MORE_SETTLE_MAX_MS = 500
# Brief pause before the one retry on a transient "See more" click failure.
SEE_MORE_RETRY_DELAY_MS = 150
# Floor on scroll iterations before early-stop is allowed to trigger, so a single
# old/pinned post near the top of the feed can't cut a scan short.
MIN_SCROLLS_BEFORE_EARLY_STOP = 3
# How many old posts in a row (older than MAX_POST_AGE_DAYS or the group's last scan
# time, whichever is more recent) it takes during scrolling to conclude we've scrolled
# past all new content. Tolerance against pinned/sponsored posts breaking chronological
# order — a fallback safety net independent of the group feed's sort mode.
CONSECUTIVE_OLD_POSTS_TO_STOP = 5

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
MAX_CONCURRENT_GROUPS = 6

# ─── Google Sheets ─────────────────────────────────────────────────────────────
SHEET_HEADERS = [
    "לינק למודעה", "מחיר", "חדרים", "מרחק הליכה (ק\"מ)", "תאריך כניסה",
    "קומה", "מעלית", "חניה", "ארנונה (לחודשיים)", "ועד בית (לחודשיים)", "ממ\"ד/מקלט", "תיווך/פרטי",
    "תאריך פרסום", "כתובת", "זמן סריקה", "ציון התאמה", "עלות חודשית כוללת"
]

# Bi-monthly arnona above this is treated as an extraction bug and dropped (left blank).
ARNONA_MAX_BIMONTHLY = 2000

# ─── Total monthly cost ─────────────────────────────────────────────────────────
# compute_total_monthly_cost() (scoring.py): rent + arnona + vaad + agent fee amortized.
# Arnona/vaad missing from a post fall back to these *monthly* amounts. City is matched
# as a substring of the address; no match uses TOTAL_COST_DEFAULT_CITY.
DEFAULT_ARNONA_MONTHLY_BY_CITY = {"גבעתיים": 900, "רמת גן": 700, "תל אביב": 500}
TOTAL_COST_DEFAULT_CITY = "רמת גן"
DEFAULT_VAAD_MONTHLY = 250
# One month's rent as broker fee, spread over this many months of lease. Separate from
# AGENT_FEE_AMORTIZE_MONTHS, which only feeds the fit score.
TOTAL_COST_AGENT_FEE_AMORTIZE_MONTHS = 24

# ─── Fit score ──────────────────────────────────────────────────────────────────
# compute_fit_score() (scoring.py) weights, must sum to 100.
SCORE_WEIGHT_PRICE = 25
SCORE_WEIGHT_LOCATION = 25
SCORE_WEIGHT_ENTRY_DATE = 25
SCORE_WEIGHT_AMENITIES = 10  # floor/elevator condition (primary) + shelter (minor bonus)
SCORE_WEIGHT_ROOMS = 10
SCORE_WEIGHT_PARKING = 5

# Agent listings carry a one-time broker's fee (typically one month's rent) the
# private-listing price doesn't. Amortized over a year of lease and folded into the
# *scoring* price only (not the displayed price) so agent vs. private listings are
# compared on a fairer effective-monthly-cost basis.
AGENT_FEE_AMORTIZE_MONTHS = 12

# Price scoring is flat-ish up to this point, then decays rapidly toward MAX_PRICE
# and beyond — "~6500 is still ok, but it gets bad fast after that."
SCORE_PRICE_SOFT_CEILING = 6500

# Entry-date scoring is rolling, measured from *today* (not a fixed calendar
# date) — full marks (1.0) for on-or-before today, since moving in ASAP is
# always best and never penalized for being early, then a smooth (Gaussian)
# decay the later the listing's entry date gets, not stepped tiers, so e.g.
# Nov 20 scores lower than Nov 1 even though both are "November".
# score = 1.0 if days_signed <= 0, else exp(-days_signed^2 / (2*sigma^2));
# at days_signed == sigma the score is ~0.61, at 2*sigma it's ~0.14. Sigma is
# tuned so December-or-later is already very low (see scoring._entry_date_score,
# core.normalize.resolve_entry_date). A missing/unparseable entry date is the
# only case that scores a hard 0, since there's genuinely no date to measure.
SCORE_ENTRY_DATE_SIGMA_DAYS_AFTER = 25

# Listings whose entry date is confidently parsed as more than this many days
# out are rejected outright (verdict rejected_entry_date), not just scored
# low — see core/evaluate.py. An unparseable/missing entry date is never
# rejected on this basis, only an actual resolved date this far out.
SCORE_ENTRY_DATE_MAX_DAYS_AHEAD = 60

# Directional location adjustment relative to DESTINATION_LAT/DESTINATION_LON:
# north of the destination is a bonus, south or east is a penalty (west is neutral —
# not mentioned as either good or bad). Points per km, folded into the location
# score and clamped so it can only shift that component by +/- SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT.
SCORE_LOCATION_NORTH_BONUS_PER_KM = 1.5
SCORE_LOCATION_SOUTH_PENALTY_PER_KM = 1.5
SCORE_LOCATION_EAST_PENALTY_PER_KM = 1.5
SCORE_LOCATION_DIRECTION_MAX_ADJUSTMENT = 4
# Givataim is preferred over Ramat Gan — a flat bonus added to the same
# direction-adjustment budget above (detected from the formatted address string).
SCORE_LOCATION_GIVATAIM_BONUS = 2

# Preferred neighborhoods (Tel Aviv + Givatayim): a listing whose address or post text
# mentions one gets SCORE_FOCUS_NEIGHBORHOOD_BONUS added to its total fit score (clamped
# at 100). A ranking boost only — nothing outside these areas is filtered out. Spelling
# variants (gershayim/quote forms) are listed explicitly because matching is a plain substring.
FOCUS_NEIGHBORHOODS_TEL_AVIV = [
    "תל חיים", "ביצרון", "רמת ישראל", "שיכון חברת החשמל", "שכון חברת החשמל",
    "שיכון עממי ג", "שכון עממי ג", "יד אליהו", "נחלת יצחק", "רמת הטייסים",
]
FOCUS_NEIGHBORHOODS_GIVATAYIM = [
    "גבעת רמב\"ם", "גבעת רמב״ם", "גבעת רמבם", "תל גנים", "ארלוזורוב",
    "הל\"ה", "הל״ה",
]
FOCUS_NEIGHBORHOODS = FOCUS_NEIGHBORHOODS_TEL_AVIV + FOCUS_NEIGHBORHOODS_GIVATAYIM
SCORE_FOCUS_NEIGHBORHOOD_BONUS = 15

# No-elevator is only penalized once the floor is high enough that stairs are a
# real burden; below this floor a walk-up is considered fine.
SCORE_NO_ELEVATOR_FLOOR_THRESHOLD = 3
# Points deducted per floor above the threshold when there's no elevator.
SCORE_NO_ELEVATOR_PENALTY_PER_FLOOR = 2
# Points granted (out of SCORE_WEIGHT_AMENITIES) when shelter/ממ"ד is present.
SCORE_SHELTER_BONUS = 2

# 3.5 rooms scores a bit lower than 3.0 — this is the max fraction of
# SCORE_WEIGHT_ROOMS shaved off at MAX_ROOMS, interpolated linearly from MIN_ROOMS.
SCORE_ROOMS_MAX_PENALTY_FRACTION = 0.2

# ─── Free routing fallback (over Google's monthly cap) ──────────────────────────
# Straight-line (haversine) distance to DESTINATION_ADDRESS, used only when
# GMAPS_MONTHLY_CAP is hit and Google's own geocoding is therefore also unavailable
# (see get_walking_distance()). Placeholders matching the placeholder
# DESTINATION_ADDRESS above — override both in config_local.py with your real
# address's coordinates (same reasoning as TARGET_URLS: repo is public).
DESTINATION_LAT = 32.0809
DESTINATION_LON = 34.7806

# Streets aren't straight lines; this scales the haversine distance up to
# approximate real walking distance. Uncalibrated against real data — a rough
# correction, not a precise one.
STRAIGHT_LINE_CALIBRATION_FACTOR = 1.35

# Required by Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/):
# identify the app and give a contact method.
NOMINATIM_USER_AGENT = "apartment-bot/1.0 (contact: ssasporta@gmail.com)"

# ─── Telegram (optional push + voting) ───────────────────────────────────────────
# Kill switch — bot runs fine with this False and no Telegram env vars set at all.
# When True, TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID must be set in .env.
TELEGRAM_ENABLED = True

# Down-votes at which bot_listener.py deletes the listing's row from the sheet outright.
VOTES_TO_REMOVE = 2
# Up-votes at which bot_listener.py sends a highlight alert to the Telegram chat.
VOTES_TO_HIGHLIGHT = 2
# Added to the sheet's fit-score cell per up-vote (clamped to 100).
SCORE_VOTE_BOOST = 10

# ─── Local overrides ────────────────────────────────────────────────────────────
# config_local.py (gitignored) can override any setting above — currently used
# for TARGET_URLS so real group URLs never land in the (public) repo history.
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
