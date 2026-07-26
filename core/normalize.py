"""
=== Pure text/domain normalization ===

Post text, dates, fees, floor, parking, address formatting, sheet-cell
safety, dedup-key text normalization — all pure string/regex transforms, no
I/O. Extracted from apartment_bot.py so this logic is unit-testable without
credentials or a browser.
"""
import re
import threading
from datetime import datetime, timedelta

from config import MAX_POST_AGE_DAYS
from core.util import _safe_print

def map_bool(val):
    if val is True: return "כן"
    if val is False: return "לא"
    return ""

# Strips invisible BIDI characters that Facebook injects and that break regexes
BIDI_RE = re.compile(r'[‎‏‪-‮⁦-⁩]')

# article.inner_text() also includes the comments section below the post — cut at
# the first marker so a price/detail from another user's comment (not the poster's)
# doesn't contaminate data extraction.
_COMMENT_SECTION_RE = re.compile(
    r'View more comments|View \d+ repl|Write a (?:public )?comment|Submit your first comment|Most relevant'
)

def _strip_comment_section(text: str) -> str:
    match = _COMMENT_SECTION_RE.search(text)
    return text[:match.start()].strip() if match else text

# Israeli mobile (05X) and landline/VoIP (0[23489]/07[2-9]) numbers, with or
# without a separator — a poster's own contact number doesn't change between
# reposts even when they reword the ad text by hand (confirmed against a real
# crosspost pair in the DB: same phones, genuinely different wording).
_PHONE_RE = re.compile(r'0(?:5\d|7[2-9])[-\s]?\d{7}|0[23489][-\s]?\d{7}')

_ROOMS_EXTRACT_RE = re.compile(r'([1-9](?:\.5)?)\s*חד')

# Price "second chance" — only a number found within ~25 chars of a price
# word/marker, not any 4-5 digit number in the text (so it doesn't grab a phone
# number, someone else's comment, etc.). The \D (non-digit) gap blocks crossing
# over another number sitting between the candidate and the marker — so "was
# 6500, now 7200 ש"ח" doesn't attribute the ש"ח to 6500 even though it's within
# the 25-char window.
_PRICE_MARKER = r'(?:₪|ש["״]?ח|שכ["״]?ד|שכר\s*דירה|מחיר|לחודש)'

_PRICE_CONTEXT_RE = re.compile(
    rf'{_PRICE_MARKER}\D{{0,25}}(?<![0-9])([0-9]{{4,5}})(?![0-9])'
    rf'|(?<![0-9])([0-9]{{4,5}})(?![0-9])\D{{0,25}}{_PRICE_MARKER}'
)

# Converts the relative date Facebook shows ("6h", "1d", "3w", plus the longer
# forms Facebook sometimes renders: "3 hrs", "1 day", "2 wks") to an absolute date (DD/MM)
_RELATIVE_DATE_RE = re.compile(
    r'^(\d+)\s*(s|sec|secs|second|seconds|'
    r'm|min|mins|minute|minutes|'
    r'h|hr|hrs|hour|hours|'
    r'd|day|days|'
    r'w|wk|wks|week|weeks)$',
    re.IGNORECASE
)

_RELATIVE_DATE_UNIT_ALIASES = {
    's': 's', 'sec': 's', 'secs': 's', 'second': 's', 'seconds': 's',
    'm': 'm', 'min': 'm', 'mins': 'm', 'minute': 'm', 'minutes': 'm',
    'h': 'h', 'hr': 'h', 'hrs': 'h', 'hour': 'h', 'hours': 'h',
    'd': 'd', 'day': 'd', 'days': 'd',
    'w': 'w', 'wk': 'w', 'wks': 'w', 'week': 'w', 'weeks': 'w',
}

_RELATIVE_DATE_UNITS = {
    's': lambda v: timedelta(seconds=v),
    'm': lambda v: timedelta(minutes=v),
    'h': lambda v: timedelta(hours=v),
    'd': lambda v: timedelta(days=v),
    'w': lambda v: timedelta(weeks=v),
}

_YESTERDAY_RE = re.compile(r'^yesterday$', re.IGNORECASE)

# Detects absolute English-language dates that Facebook sometimes shows instead of relative text (e.g. "July 9 at 5:50 PM")
_MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10, 'october': 10,
    'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}

_ABSOLUTE_DATE_RE = re.compile(r'\b([A-Za-z]+)\s+(\d{1,2})\b')

def _parse_absolute_fb_date(text: str) -> str | None:
    match = _ABSOLUTE_DATE_RE.search(text)
    if not match:
        return None
    month = _MONTH_NAMES.get(match.group(1).lower())
    if not month:
        return None
    try:
        now = datetime.now()
        dt = datetime(now.year, month, int(match.group(2)))
        if dt > now:
            dt = dt.replace(year=now.year - 1)
        return dt.strftime("%d/%m")
    except ValueError:
        return None

_date_warning_lock = threading.Lock()

_unparsed_date_logged = False

def relative_to_date(rel: str) -> str:
    """
    Converts the relative/absolute date Facebook shows to a DD/MM date. The bot
    assumes an English-language Facebook UI (not Hebrew) — see README. An
    unrecognized format passes through unchanged, with a one-time-per-run
    warning log so a future locale change doesn't degrade silently.
    """
    global _unparsed_date_logged
    text = (rel or "").strip()
    if not text:
        return rel

    if _YESTERDAY_RE.match(text):
        return (datetime.now() - timedelta(days=1)).strftime("%d/%m")

    match = _RELATIVE_DATE_RE.match(text)
    if match:
        value = int(match.group(1))
        unit = _RELATIVE_DATE_UNIT_ALIASES[match.group(2).lower()]
        return (datetime.now() - _RELATIVE_DATE_UNITS[unit](value)).strftime("%d/%m")

    absolute = _parse_absolute_fb_date(text)
    if absolute:
        return absolute

    with _date_warning_lock:
        already_logged = _unparsed_date_logged
        _unparsed_date_logged = True
    if not already_logged:
        _safe_print(f"WARNING: unrecognized Facebook post-date format: {rel!r} — passing through unchanged. "
                    f"If Facebook's UI locale changed, date parsing may need an update.")
    return rel

def _normalize_bimonthly_fee(raw: str):
    """
    Arnona (municipal tax)/vaad bayit (building fee) are standardly billed once
    every two months in Israel — if the post stated a monthly amount, double it
    to the bi-monthly value. Returns an integer only (not a string), with no
    currency/unit marks.
    """
    if not raw:
        return ""
    digits = re.sub(r'[^\d]', '', raw)
    if not digits:
        return 0 if "כלול" in raw else ""
    value = int(digits)
    if re.search(r'לחודש(?!יים)', raw):
        value *= 2
    return value

_FLOOR_ORDINALS = {
    'קרקע': 0, 'ראשונה': 1, 'שניה': 2, 'שנייה': 2, 'שלישית': 3, 'רביעית': 4,
    'חמישית': 5, 'שישית': 6, 'שביעית': 7, 'שמינית': 8, 'תשיעית': 9, 'עשירית': 10,
}

_FLOOR_DIGIT_RE = re.compile(r'\d+')

def _parse_floor(raw: str):
    """
    Returns floor as an integer. 'קרקע' (ground) = 0. Hebrew ordinal words
    (first/second/...) and the 'X מתוך Y' (X of Y) pattern are supported. An
    explicit digit (if present) wins over 'קרקע' — "1 above ground floor" = 1, not 0.
    """
    if not raw:
        return ""
    raw = raw.strip()
    match = _FLOOR_DIGIT_RE.search(raw)
    if match:
        return int(match.group(0))
    for word, num in _FLOOR_ORDINALS.items():
        if word in raw:
            return num
    return ""

def _clean_post_for_llm(raw_text: str) -> str:
    """Strips URLs (useless for data extraction) and excess whitespace/blank lines — saves tokens."""
    clean = re.sub(r'https?://\S+|www\.\S+', '', raw_text)
    clean = re.sub(r'\n{2,}', '\n', clean)
    clean = re.sub(r'[ \t]{2,}', ' ', clean)
    return clean.strip()

_NOT_AGENT_RE = re.compile(r'ללא\s+תיווך|לא\s+מתיווך|בלי\s+תיווך|לא\s+תיווך')

_AGENT_SIGNAL_RE = re.compile(r'תיווך|נדל["״]?ן')

def _detect_agent(text: str, llm_is_agent):
    """
    Adds a deterministic signal on top of the model's judgment: the word 'תיווך'
    (agency) or a real-estate agency name in the text = definitely an agent.
    Explicit negation phrases ("ללא תיווך"/no agent, etc.) don't count as a
    positive signal — left to the model.
    """
    if _NOT_AGENT_RE.search(text):
        return llm_is_agent
    if _AGENT_SIGNAL_RE.search(text):
        return True
    return llm_is_agent

# 'שותפ'/'שותף' (roommate) alone disqualifies roommate posts, but a landlord who
# writes "suitable for a couple or 2 roommates" is describing tenant-type
# flexibility — not renting out a single room. If 'זוג' (couple) appears nearby
# (within ~30 chars), don't disqualify.
# The lookbehind (?<!מי) prevents matching 'מיזוג' (air conditioning, very common
# in listings); the lookahead (?!י) prevents 'זוגי'/'זוגית' (double bed). [פף]
# also covers the singular form "שותף".
_ROOMMATE_COUPLE_EXCEPTION_RE = re.compile(
    r'(?<!מי)זוג(?!י).{0,30}שות[פף]|שות[פף].{0,30}(?<!מי)זוג(?!י)',
    re.DOTALL
)

_PARKING_NONE_RE = re.compile(r'אין\s*חניה|בלי\s*חניה|ללא\s*חניה|^אין$')

_PARKING_PRIVATE_RE = re.compile(r'פרטי|טאבו|מקור|צמוד|תת\s*קרקעי|חניון')

_PARKING_STREET_RE = re.compile(r'רחוב|ציבור|חופשית')

def _classify_parking(raw: str) -> str:
    """Classifies the model's free-text parking field into one of three fixed categories, or blank if unclear/unstated."""
    if not raw:
        return ""
    raw = raw.strip()
    if _PARKING_NONE_RE.search(raw):
        return "לא"
    if _PARKING_PRIVATE_RE.search(raw):
        return "פרטית"
    if _PARKING_STREET_RE.search(raw):
        return "ברחוב"
    return ""

_FOREIGN_LETTERS_RE = re.compile(r'[^\u0590-\u05FF\d\s.,\-\/\\\'"()\[\]]+')

def _strip_foreign_letters(text: str) -> str:
    """
    Deterministic backstop for the prompt's 'Hebrew only' rule. Strips any
    letter that isn't Hebrew, a digit, or punctuation, to prevent foreign-
    language hallucinations from the model.
    """
    if not text:
        return text
    cleaned = _FOREIGN_LETTERS_RE.sub('', text)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip(' \t-–—,/')
    return cleaned

_IMMEDIATE_RE = re.compile(r'מיידי|מיד|עכשיו|כניסה\s*מיידית|היום')

_DATE_DDMM_RE = re.compile(r'(\d{1,2})[./](\d{1,2})(?:[./]\d{2,4})?')

def _normalize_entry_date(text: str) -> str:
    """
    Normalizes the entry date, strips foreign-language text, converts
    "immediate"-type phrases to the standard "מיידי", and zero-pads a
    numeric day.month(.year) date (e.g. "1.8" / "1.8.26") to "DD/MM",
    dropping the year. Non-numeric dates (e.g. "ספטמבר") pass through as-is.
    """
    if not text:
        return ""
    text = _strip_foreign_letters(text)
    if _IMMEDIATE_RE.search(text):
        return "מיידי"
    m = _DATE_DDMM_RE.fullmatch(text.strip().rstrip('./'))
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{day:02d}/{month:02d}"
    return text

def _reject_hallucinated_address(address: str, source_text: str) -> str:
    """
    qwen2.5 repeatedly invents "נמל התעופה" (airport) as an address for posts that
    never mention an airport at all — even with an explicit anti-invention prompt
    rule. Deterministic denylist: reject it unless the source text actually says so.
    """
    if address and "נמל התעופה" in address and "נמל התעופה" not in source_text and "שדה תעופה" not in source_text:
        return ""
    return address

def _warn_if_fee_implausible(label: str, value, max_bimonthly: int):
    if not value:
        return
    if value > max_bimonthly:
        _safe_print(f"\n    WARNING: {label} looks unusually high ({value}) - verify manually.")

# Rows are written with value_input_option="USER_ENTERED" (needed so numeric
# columns like price/rooms are stored as real numbers, not text) — that also
# lets Sheets interpret a cell starting with =/+/-/@ as a formula. Row content
# ultimately comes from scraped Facebook text via an LLM (untrusted input), so
# this is an explicit safeguard, not incidental: prefix any such string with a
# literal-text apostrophe before it's sent. (In practice every string field
# here is already narrowed to a fixed set or Hebrew-only text that happens to
# exclude these characters — this guard is deliberate defense-in-depth so that
# stays true even if that normalization changes later.)
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")

def _sheet_safe_cell(value):
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + value
    return value

# ─── Cross-post dedup + sort ────────────────────────────────────────────────
# Posts sometimes get reposted or posted to multiple groups under a different
# URL — not caught by seen_urls. Duplicates are identified by (normalized
# street, rooms, price) and the most recent post is kept.
_CITY_TOKENS_RE = re.compile(r'רמת[\s-]?גן|גבעתיים|תל[\s-]?אביב|ר["״]?ג\b|\bרג\b')

_ADDRESS_PUNCT_RE = re.compile(r'[",./\-–—_]')

_POST_DATE_DDMM_RE = re.compile(r'^(\d{1,2})/(\d{1,2})$')

_HEBREW_RE = re.compile(r'[\u0590-\u05FF]')

_STREET_HINT_RE = re.compile(r'רחוב|רח[\'’׳]|שדרות|שד[\'’׳]|דרך|סמטת|סמטה|כיכר|משעול')

_LANDMARK_HINT_RE = re.compile(r'ליד|בסמוך|קרוב ל|צמוד ל|באזור|בשכונת|שכונת')

# Street-type word/abbreviation carries no distinguishing signal for dedupe —
# "רחוב הרצל" and "רח' הרצל" are the same street. LLM output uses the ASCII
# apostrophe (U+0027), not the Hebrew geresh (׳), so both must be recognized.
_STREET_TYPE_RE = _STREET_HINT_RE

# A cross-street qualifier ("X פינת Y" / "X על פינת Y") is a more specific
# description of the same corner, not a different address — one post calling
# it "שמחה" and another "רחוב שמחה, פינת שפירא" are very likely the same
# listing. Dropped before keying so they collapse to the same primary street;
# _format_core_with_city's _CORNER_DISPLAY_RE is a different, fuller parse
# used for display formatting, not reused here since this only needs the
# cross-street half discarded, not restructured.
_CORNER_HINT_RE = re.compile(r'\s+(?:על\s+)?פינת\s+.+$')

def _normalize_address_key(address: str) -> str:
    if not address or address == "לא צוין":
        return ""
    norm = _CITY_TOKENS_RE.sub('', address)
    norm = _CORNER_HINT_RE.sub('', norm)
    norm = _STREET_TYPE_RE.sub('', norm)
    norm = _ADDRESS_PUNCT_RE.sub(' ', norm)
    return re.sub(r'\s+', ' ', norm).strip()

# ─── Address display formatting ─────────────────────────────────────────────
# Reshapes the LLM's raw (verbatim-from-post) address into the sheet's
# preferred display convention: "<street> <number>, <city>". Only fires when
# an explicit city is present in the text — same "never invent" discipline as
# _reject_hallucinated_address: a neighborhood or street mentioned without a
# city isn't guessed at, it's left as-is.
_CITY_DISPLAY_RES = [
    (re.compile(r'(?:ב|ל)?רמת[\s-]?גן|(?:ב|ל)?ר["״]?ג\b|(?:ב|ל)?\bרג\b'), "רמת גן"),
    (re.compile(r'(?:ב|ל)?גבעתיים'), "גבעתיים"),
    # "יפו" is matched as an optional suffix of the SAME token (not a separate
    # city/street) since "תל אביב יפו"/"תל אביב-יפו" is one compound official
    # name — without this, _extract_city_and_core only consumed "תל אביב" and
    # left "יפו" behind looking like a real street/neighborhood core.
    (re.compile(r'(?:ב|ל)?תל[\s-]?אביב(?:[\s-]?יפו)?|(?:ב|ל)?ת["״]א\b|(?:ב|ל)?\bתא\b'), "תל אביב"),
]

_NEIGHBORHOOD_DISPLAY_RE = re.compile(r'^(?:ב)?שכונ(?:ת|ה)\s+(.+)$')

_CORNER_DISPLAY_RE = re.compile(r'^(.+?)\s+(?:על\s+)?פינת\s+(.+)$')

# Informal shorthand for the same "corner of X and Y" meaning ("כצנלסון/ויצמן").
_CORNER_SLASH_RE = re.compile(r'^(.+?)\s*/\s*(.+)$')

_TRAILING_NUMBER_RE = re.compile(r'^(.+?)\s+(\d+[א-ת]?)$')

def _extract_city_and_core(address: str) -> tuple[str, str] | None:
    """Finds an explicit city token in the address text. Returns (remaining core, canonical city), or None."""
    for city_re, canonical in _CITY_DISPLAY_RES:
        m = city_re.search(address)
        if m:
            core = address[:m.start()] + address[m.end():]
            # Edge-trim only (not a global punctuation substitution like
            # _ADDRESS_PUNCT_RE): a Hebrew acronym street name can carry an
            # internal גרשיים ("הרא"ה", "ל"ה"), and an already-formatted
            # corner keeps its internal " - " separator. Stripping those
            # unconditionally would corrupt real content and break
            # idempotency (re-running this on its own prior output must be
            # a no-op).
            core = re.sub(r'\s+', ' ', core).strip(' ,./-–—_\t')
            return core, canonical
    return None

def _format_core_with_city(core: str, city: str) -> str:
    """Shapes address text (city already removed) plus a resolved city into the display format. Empty string if unusable."""
    # Nothing left after removing the city token means the post only ever
    # named the city itself (e.g. "תל אביב יפו" with no street at all) — the
    # honest result is the bare city name, not falling back to duplicating
    # the raw city mention as if it were a real address.
    if not core:
        return city
    if not _HEBREW_RE.search(core):
        return ""

    m = _NEIGHBORHOOD_DISPLAY_RE.match(core)
    if m:
        neighborhood = m.group(1).strip()
        return f"{neighborhood}, {city}" if neighborhood else ""

    m = _CORNER_DISPLAY_RE.match(core) or _CORNER_SLASH_RE.match(core)
    if m:
        street_a = _STREET_HINT_RE.sub('', m.group(1)).strip(' ,./-–—_\t')
        street_b = _STREET_HINT_RE.sub('', m.group(2)).strip(' ,./-–—_\t')
        return f"{street_a} פינת {street_b}, {city}" if street_a and street_b else ""

    street_core = _STREET_HINT_RE.sub('', core, count=1).strip()
    if not street_core:
        return ""

    m = _TRAILING_NUMBER_RE.match(street_core)
    if m:
        street, number = m.group(1).strip(), m.group(2).strip()
        return f"{street} {number}, {city}" if street else ""

    return f"{street_core}, {city}"

def _extract_candidate_cities(group_name: str) -> list[str]:
    """Distinct canonical cities mentioned in a FB group's real display name (e.g. 'דירות רמת גן/גבעתיים')."""
    cities = []
    for city_re, canonical in _CITY_DISPLAY_RES:
        if city_re.search(group_name) and canonical not in cities:
            cities.append(canonical)
    return cities

def _infer_post_date(date_str: str, now: datetime | None = None) -> datetime | None:
    """
    Converts displayed DD/MM into a full datetime near 'now'.
    Keeps sheet formatting as DD/MM, but makes filtering/sorting year-safe.
    """
    now = now or datetime.now()
    match = _POST_DATE_DDMM_RE.match((date_str or "").strip())
    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))

    try:
        candidate = datetime(now.year, month, day)
    except ValueError:
        return None

    if candidate > now + timedelta(days=1):
        candidate = candidate.replace(year=now.year - 1)

    return candidate

def _is_recent_post_date(date_str: str) -> bool:
    parsed = _infer_post_date(date_str)
    if parsed is None:
        return True
    return parsed >= datetime.now() - timedelta(days=MAX_POST_AGE_DAYS)

def _post_date_sort_key(date_str: str) -> datetime:
    parsed = _infer_post_date(date_str)
    return parsed or datetime.min

def _classify_address_confidence(address: str) -> tuple[str, str]:
    cleaned = (address or "").strip()
    if not cleaned:
        return "missing", "כתובת חסרה"

    if cleaned in _CITY_ONLY_ADDRESSES:
        return "low", "כתובת ברמת עיר בלבד"

    if not _HEBREW_RE.search(cleaned):
        return "missing", "כתובת לא תקינה"

    if _STREET_HINT_RE.search(cleaned) or re.search(r'\d+', cleaned):
        return "high", ""

    if _LANDMARK_HINT_RE.search(cleaned):
        return "medium", "כתובת לפי שכונה/ציון דרך - לבדיקה"

    if len(_normalize_address_key(cleaned)) < 4:
        return "low", "כתובת קצרה/כללית מדי"

    return "medium", "כתובת ללא אינדיקציה ברורה לרחוב"

_CITY_ONLY_ADDRESSES = {"רמת גן", "רמת-גן", "גבעתיים", "תל אביב", 'ר"ג', "ר״ג"}
