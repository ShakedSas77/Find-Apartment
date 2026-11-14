"""
Data for the web app's pages, kept free of Streamlit so it's tested directly: the listings table built
from the results file, the filters on it, the Groups table, and what the quick setup still needs.
"""
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import gspread
import pandas as pd

import config
import env
from config import (
    SCORE_FOCUS_NEIGHBORHOOD_BONUS, SCORE_WEIGHT_AMENITIES, SCORE_WEIGHT_ENTRY_DATE, SCORE_WEIGHT_LOCATION,
    SCORE_WEIGHT_PARKING, SCORE_WEIGHT_PRICE, SCORE_WEIGHT_ROOMS, SHEET_HEADERS, YAD2_TAB_NAME,
)
from core import storage
from core.dedupe import apartment_key
from core.local_settings import is_saved
from core.normalize import _infer_post_date
from core.scoring import score_breakdown
from integrations.groups_sync import HEADERS as GROUP_HEADERS, stats_cells
from integrations.sheets import _fields_from_sheet_row

# How a run's type and outcome (storage `runs` rows) read in the app.
RUN_TYPES = {"live": "מלאה", "live_fb": "פייסבוק", "live_yad2": "יד2", "live_group": "קבוצה אחת"}
RUN_STATUSES = {"ok": "✅ הצליחה", "running": "⏳ רצה", "stopped": "⏹ נעצרה", "checkpoint": "⚠️ בדיקת אבטחה",
                "no_groups_scanned": "⚠️ אף קבוצה", "login_required": "🔑 צריך להתחבר", "crashed": "❌ נכשלה"}

SOURCE = "מקור"
STATUS, NOTE = "מעקב", "הערה"
# storage.TRACKING_STATUSES as the app shows them; "" = not marked yet.
STATUS_LABELS = {"": "—", "interested": "⭐ מעניינת", "contacted": "📞 פניתי", "visited": "👀 ראיתי",
                 "not_relevant": "❌ לא רלוונטית"}
STATUS_KEYS = {label: key for key, label in STATUS_LABELS.items()}
MAPS = "מפה"
LAT, LON = "lat", "lon"
TIMES_POSTED, PRICE_CHANGE = "פרסומים", "שינוי מחיר"
# core.scoring.score_breakdown's parts: (Hebrew label, maximum points).
_SCORE_PARTS = {"price": ("מחיר", SCORE_WEIGHT_PRICE), "location": ("מיקום ומרחק", SCORE_WEIGHT_LOCATION),
                "entry_date": ("תאריך כניסה", SCORE_WEIGHT_ENTRY_DATE),
                "amenities": ("קומה, מעלית, ממ\"ד", SCORE_WEIGHT_AMENITIES), "rooms": ("חדרים", SCORE_WEIGHT_ROOMS),
                "parking": ("חניה", SCORE_WEIGHT_PARKING), "focus": ("שכונה מועדפת (בונוס)", SCORE_FOCUS_NEIGHBORHOOD_BONUS)}
PRICE, ROOMS, DISTANCE, SCORE = "מחיר", "חדרים", 'מרחק הליכה (ק"מ)', "ציון התאמה"
ADDRESS, POST_DATE, SCAN_TIME = "כתובת", "תאריך פרסום", "זמן סריקה"
_NUMBER_COLUMNS = [PRICE, ROOMS, DISTANCE, SCORE, "עלות חודשית כוללת", "ארנונה (לחודשיים)", "ועד בית (לחודשיים)"]


def read_listings(spreadsheet) -> pd.DataFrame:
    """Every listing in the results file: the main tab, plus the Yad2 tab when there is one."""
    tabs = [("פייסבוק", spreadsheet.sheet1.get_all_values())]
    try:
        tabs.append(("יד2", spreadsheet.worksheet(YAD2_TAB_NAME).get_all_values()))
    except gspread.WorksheetNotFound:
        pass
    return listings_frame(tabs)


def listings_frame(tabs: list[tuple[str, list[list[str]]]]) -> pd.DataFrame:
    """(source name, sheet rows with the header first) per tab -> one table, numbers as numbers,
    with the source and a Google Maps link added."""
    rows = []
    for source, values in tabs:
        for row in values[1:]:
            if row and row[0]:
                # Older rows hold the text "None" where a value was missing.
                cells = ["" if c == "None" else c for c in (row + [""] * len(SHEET_HEADERS))[:len(SHEET_HEADERS)]]
                rows.append(dict(zip(SHEET_HEADERS, cells), **{SOURCE: source}))
    frame = pd.DataFrame(rows, columns=SHEET_HEADERS + [SOURCE])
    for column in _NUMBER_COLUMNS:
        frame[column] = pd.to_numeric(frame[column].astype(str).str.replace(",", ""), errors="coerce")
    frame[MAPS] = frame[ADDRESS].map(
        lambda a: f"https://www.google.com/maps/search/?api=1&query={quote_plus(a + ', ישראל')}" if a else None)
    return frame


def with_tracking(frame: pd.DataFrame, tracking: dict[str, dict]) -> pd.DataFrame:
    """Adds the user's status (as its label) and note for each listing (storage.get_listing_tracking())."""
    frame = frame.copy()
    frame[STATUS] = frame["לינק למודעה"].map(lambda url: STATUS_LABELS[tracking.get(url, {}).get("status", "")])
    frame[NOTE] = frame["לינק למודעה"].map(lambda url: tracking.get(url, {}).get("note", ""))
    return frame


def tracking_changes(before: pd.DataFrame, after: pd.DataFrame) -> list[tuple[str, str, str]]:
    """(url, status key, note) for each row whose status or note was edited in the table."""
    changed = (before[STATUS] != after[STATUS]) | (before[NOTE].fillna("") != after[NOTE].fillna(""))
    return [(row["לינק למודעה"], STATUS_KEYS.get(row[STATUS] or "—", ""), row[NOTE] or "")
            for _, row in after[changed].iterrows()]


def filter_listings(frame: pd.DataFrame, max_price: float | None = None, min_rooms: float | None = None,
                    max_km: float | None = None, sources: list[str] | None = None, text: str = "",
                    min_score: float | None = None, max_age_days: int | None = None,
                    statuses: list[str] | None = None, hide_not_relevant: bool = False) -> pd.DataFrame:
    """Rows within every given limit. A blank price, distance or post date is kept — unknown isn't out
    of range, the same rule the scan uses."""
    keep = pd.Series(True, index=frame.index)
    if statuses:
        keep &= frame[STATUS].isin(statuses)
    if hide_not_relevant:
        keep &= frame[STATUS] != STATUS_LABELS["not_relevant"]
    if min_score is not None:
        keep &= frame[SCORE] >= min_score
    if max_age_days is not None:
        cutoff = datetime.now() - timedelta(days=max_age_days)
        posted = frame[POST_DATE].map(_infer_post_date)
        keep &= posted.map(lambda d: d is None or d >= cutoff)
    if max_price is not None:
        keep &= frame[PRICE].isna() | (frame[PRICE] <= max_price)
    if min_rooms is not None:
        keep &= frame[ROOMS] >= min_rooms
    if max_km is not None:
        keep &= frame[DISTANCE].isna() | (frame[DISTANCE] <= max_km)
    if sources:
        keep &= frame[SOURCE].isin(sources)
    if text:
        keep &= frame[ADDRESS].str.contains(text, regex=False, na=False)
    return frame[keep]


def with_coordinates(frame: pd.DataFrame) -> pd.DataFrame:
    """Adds each listing's map position, from where the bot located its address when measuring the
    distance (the address cache); blank when it couldn't be placed."""
    places = {address: storage.get_address_cache(address) or {} for address in frame[ADDRESS].unique() if address}
    frame = frame.copy()
    frame[LAT] = frame[ADDRESS].map(lambda a: places.get(a, {}).get("lat"))
    frame[LON] = frame[ADDRESS].map(lambda a: places.get(a, {}).get("lon"))
    return frame


def history_by_flat(posts: list[dict]) -> dict[tuple, list[dict]]:
    """storage.get_priced_posts() grouped by flat (core.dedupe.apartment_key), oldest post first."""
    flats: dict[tuple, list[dict]] = {}
    for post in posts:
        key = apartment_key(post["address"], post["rooms_val"])
        if key:
            flats.setdefault(key, []).append(post)
    return flats


def flat_history(history: dict[tuple, list[dict]], address: str, rooms) -> list[dict]:
    """Every stored post of this flat, oldest first; empty when the address is too weak to match."""
    key = apartment_key(address, rooms)
    return history.get(key, []) if key else []


def price_timeline(posts: list[dict]) -> list[dict]:
    """A flat's posts as its price history: one entry per day and price, with how many posts it had
    (a flat cross-posted to many groups the same day is one line, not twenty). Oldest first."""
    timeline: list[dict] = []
    for post in posts:
        day = post["first_seen"][:10]
        if timeline and timeline[-1]["day"] == day and timeline[-1]["price"] == post["price_val"]:
            timeline[-1]["posts"] += 1
        else:
            timeline.append({"day": day, "price": post["price_val"], "posts": 1, "url": post["url"]})
    return timeline


def is_exact_flat(address: str) -> bool:
    """Whether the address has a house number — without one, posts "of the same flat" may be different
    flats on the same street with the same number of rooms."""
    return any(ch.isdigit() for ch in address or "")


def with_history(frame: pd.DataFrame, history: dict[tuple, list[dict]]) -> pd.DataFrame:
    """Adds how many times each listing's flat was posted, and its price now minus its first price."""
    frame = frame.copy()
    posts = [flat_history(history, a, r) for a, r in zip(frame[ADDRESS], frame[ROOMS])]
    frame[TIMES_POSTED] = [len(p) or 1 for p in posts]
    frame[PRICE_CHANGE] = [(price - p[0]["price_val"]) if p and pd.notna(price) and price != p[0]["price_val"] else None
                           for p, price in zip(posts, frame[PRICE])]
    return frame


def score_parts(row: pd.Series) -> list[tuple[str, float, float]]:
    """(part, points, out of) for the listing's fit score, recomputed with the current formula from its
    row; the location part includes the direction from the destination when the address was placed."""
    cells = ["" if pd.isna(row.get(h)) else str(row.get(h)) for h in SHEET_HEADERS]
    fields = _fields_from_sheet_row(cells)
    place = storage.get_address_cache(row[ADDRESS]) or {} if row[ADDRESS] else {}
    fields.update(lat=place.get("lat"), lon=place.get("lon"))
    parts = score_breakdown(fields)
    return [(label, parts[key], weight) for key, (label, weight) in _SCORE_PARTS.items()]


def compare_frame(listings: pd.DataFrame) -> pd.DataFrame:
    """Listings side by side: one column per listing (its address), one row per detail."""
    rows = {"ציון": SCORE, "מחיר": PRICE, "עלות חודשית כוללת": "עלות חודשית כוללת", "חדרים": ROOMS,
            "מרחק הליכה (ק\"מ)": DISTANCE, "דיוק מרחק": "דיוק מרחק", "תאריך כניסה": "תאריך כניסה",
            "קומה": "קומה", "מעלית": "מעלית", "חניה": "חניה", "ממ\"ד/מקלט": 'ממ"ד/מקלט', "תיווך/פרטי": "תיווך/פרטי",
            "ארנונה (לחודשיים)": "ארנונה (לחודשיים)", "ועד בית (לחודשיים)": "ועד בית (לחודשיים)",
            "תאריך פרסום": POST_DATE, "מקור": SOURCE, "מעקב": STATUS}
    return pd.DataFrame({_column_name(row, i): [display_text(row.get(col)) for col in rows.values()]
                         for i, (_, row) in enumerate(listings.iterrows())}, index=list(rows))


def display_text(value) -> str:
    """A cell as the app shows it: blank for missing, 3 not 3.0, 6,000 for thousands."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "None":
        return ""
    if isinstance(value, float):
        return f"{value:,.0f}" if value.is_integer() else f"{value:g}"
    return str(value)


def _column_name(row: pd.Series, i: int) -> str:
    # Two listings can share an address (different flats in one building): numbered so columns stay distinct.
    return f"{i + 1}. {row[ADDRESS] or 'בלי כתובת'}"


def groups_frame(groups: list[dict], overview: dict[str, dict]) -> pd.DataFrame:
    """The Groups tab as the web page shows it: one row per group with the bot's statistics."""
    rows = [[g["url"], g["name"] or overview.get(g["url"], {}).get("name", ""), g["active"]]
            + stats_cells(overview.get(g["url"], {})) for g in groups]
    return pd.DataFrame(rows, columns=["url", "name", "active"] + GROUP_HEADERS[5:])


def log_lines(path, only_problems: bool = False) -> list[str] | None:
    """A scan log's lines (only the ERROR/WARNING ones if asked), or None once the file was pruned."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return [line for line in lines if "ERROR" in line or "WARNING" in line] if only_problems else lines


def setup_missing() -> list[str]:
    """The quick-setup steps still to do ("destination", "ai", "groups"); empty when scans can run."""
    missing = []
    if not is_saved("DESTINATION_ADDRESS"):
        missing.append("destination")
    if config.LLM_PROVIDER == "gemini" and not env.get_gemini_api_key():
        missing.append("ai")
    if not storage.get_groups():
        missing.append("groups")
    return missing
