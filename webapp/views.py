"""
The web app's pages. Each is a function st.navigation runs; data comes from webapp/data.py and the
settings sections from webapp/components.py.
"""
import html
import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

import pandas as pd
import pydeck as pdk
import streamlit as st

import config
from core import scan_runner, storage
from core.normalize import _first_mobile_phone
from integrations.groups_sync import remove_group, set_group_active
from integrations.sheets import open_spreadsheet
from webapp import components, data

_CACHE_S = 60
_COMPARE_MAX = 4
DETAILS = "כרטיס"
_LOG_MAX_LINES = 1500
_TOOL_TIMEOUT_S = 1800
_TOOL_OUTPUT_CHARS = 6000
# (button, command, what it does) for the Tools page.
_MAINTENANCE = [
    ("💾 גיבוי עכשיו", ["backup_db.py"], "עותק של מסד הנתונים בתיקייה backups (נשמרים 14 האחרונים)"),
    ("🧹 ניקוי דירות ישנות", ["apartment_bot.py", "--prune"],
     "מוחק מהתוצאות דירות שפורסמו לפני יותר מ-14 יום ומאחד כפילויות"),
    ("🔁 בדיקה חוזרת של פוסטים שנדחו", ["apartment_bot.py", "--reparse-rejected"],
     "מריץ שוב את הסינון על פוסטים שנדחו על מחיר/חדרים/מרחק — שימושי אחרי שינוי תקציב. משתמש ב-AI שבחרתם"),
    ("🧮 חישוב מחדש של הציונים", ["apartment_bot.py", "--recompute-scores"],
     "מחשב מחדש את ציון ההתאמה והעלות הכוללת לכל הדירות"),
    ("🔄 סנכרון ומיון הקבוצות", ["apartment_bot.py", "--sync-groups"],
     "מעדכן את לשונית הקבוצות וממיין לפי אחוז התאמה"),
]


@st.cache_data(ttl=_CACHE_S, show_spinner="טוען את הדירות...")
def _listings() -> pd.DataFrame:
    # A Google Sheet is a network read; cached for a minute, and the refresh button clears it.
    return data.read_listings(open_spreadsheet())


def results_page():
    st.title("🏠 דירות")
    frame = _listings()
    if frame.empty:
        st.info("עוד אין דירות. הריצו סריקה מהתפריט בצד.")
        return
    shown = _filtered(_with_extras(frame), "results")
    sort_by = st.radio("מיון", [data.SCORE, data.POST_DATE, data.PRICE, data.DISTANCE], horizontal=True)
    if sort_by != data.POST_DATE:  # the file itself is kept newest post first
        shown = shown.sort_values(sort_by, ascending=sort_by != data.SCORE, na_position="last")
    st.caption(f"{len(shown)} מתוך {len(frame)} דירות · סמנו כל דירה בעמודת המעקב והוסיפו הערה · "
               "🏡 פותח את כרטיס הדירה (לחיצה לבחירת התא, ולחיצה נוספת לפתיחה)")
    edited = _listings_table(shown, height=600, editable=True)
    changes = data.tracking_changes(shown, edited)
    for url, status, note in changes:
        storage.set_listing_tracking(url, status, note)
    if changes:
        st.toast("נשמר")
    left, right = st.columns(2)
    if left.button("🔄 רענון"):
        _listings.clear()
        st.rerun()
    if config.OUTPUT_BACKEND == "excel" and right.button("📂 פתיחת הקובץ ב-Excel"):
        os.startfile(scan_runner.PROJECT_DIR / config.EXCEL_PATH)


def map_page():
    st.title("🗺 מפה")
    frame = _listings()
    if frame.empty:
        st.info("עוד אין דירות. הריצו סריקה מהתפריט בצד.")
        return
    shown = _filtered(_with_extras(frame), "map", more=True)
    placed = data.with_coordinates(shown).dropna(subset=[data.LAT, data.LON])
    st.caption(f"{len(placed)} דירות על המפה · {len(shown) - len(placed)} בלי מיקום (כתובת חסרה או לא נמצאה) · "
               f"{len(frame)} בסך הכול · הנקודה הצהובה הגדולה = היעד שלכם, והעיגול = מרחק ההליכה המקסימלי בקירוב")
    selected = st.pydeck_chart(_map_deck(placed), on_select="rerun", selection_mode="single-object",
                               height=560, key="listings_map")
    picked = (selected.selection.objects or {}).get("listings") if selected else None
    if picked:
        _listing_card(picked[0])
    else:
        st.caption("לחצו על דירה במפה כדי לראות את הפרטים.")
    with st.expander(f"הרשימה ({len(placed)})"):
        _listings_table(placed.sort_values(data.SCORE, ascending=False), height=400)


def _filtered(frame: pd.DataFrame, key: str, more: bool = False) -> pd.DataFrame:
    """The filter bar both listing pages share; the map adds fit score and how recent the post is."""
    with st.container(border=True):
        cols = st.columns([1, 1, 1, 1.4, 1.4])
        prices = frame[data.PRICE].dropna()
        max_price = cols[0].number_input("עד מחיר (₪)", value=int(prices.max()) if len(prices) else 0, step=100,
                                         key=f"{key}_price")
        min_rooms = cols[1].number_input("מינימום חדרים", value=float(frame[data.ROOMS].min()), step=0.5,
                                         key=f"{key}_rooms")
        max_km = cols[2].number_input('עד מרחק (ק"מ)', value=float(config.MAX_WALKING_DISTANCE_KM), step=0.5,
                                      key=f"{key}_km")
        sources = cols[3].multiselect("מקור", sorted(frame[data.SOURCE].unique()), placeholder="הכול",
                                      key=f"{key}_sources")
        text = cols[4].text_input("חיפוש בכתובת", key=f"{key}_text")
        left, right = st.columns([2, 1])
        statuses = left.multiselect("מעקב", list(data.STATUS_LABELS.values()),
                                    placeholder="הכול", key=f"{key}_statuses")
        hide_not_relevant = right.checkbox("הסתרת הלא רלוונטיות", value=True, key=f"{key}_hide")
        min_score = max_age_days = None
        if more:
            left, right = st.columns(2)
            min_score = left.slider("ציון התאמה מינימלי", 0, 100, 0, key=f"{key}_score") or None
            max_age_days = right.slider("פורסמו ב-X הימים האחרונים", 1, config.MAX_POST_AGE_DAYS,
                                        config.MAX_POST_AGE_DAYS, key=f"{key}_days")
    return data.filter_listings(frame, max_price, min_rooms, max_km, sources, text, min_score, max_age_days,
                                statuses, hide_not_relevant)


def _listings_table(frame: pd.DataFrame, height: int, editable: bool = False) -> pd.DataFrame:
    """The listings as a table; editable=True lets the tracking status and note be changed (returns the edits)."""
    show = st.data_editor if editable else st.dataframe
    # Keyed by the rows shown: the table keeps edits by row position, so a filter change must start a
    # fresh table rather than apply an old edit to whichever listing now sits in that row.
    # Shown as text: the table writes an empty number as "None".
    frame = frame.assign(**{DETAILS: frame["לינק למודעה"].map(lambda url: f"apartment?u={quote(url, safe='')}"),
                            data.PRICE_CHANGE: frame[data.PRICE_CHANGE].map(_price_change_cell)})
    extra = {"disabled": [c for c in frame.columns if c not in (data.STATUS, data.NOTE)],
             "key": f"listings_{hash(tuple(frame['לינק למודעה']))}"} if editable else {}
    return show(
        frame[[data.STATUS, data.NOTE, DETAILS, "לינק למודעה", data.SCORE, data.PRICE, data.PRICE_CHANGE, data.ROOMS,
               data.DISTANCE, "דיוק מרחק", data.ADDRESS, data.MAPS, "עלות חודשית כוללת", "תאריך כניסה", "קומה",
               "מעלית", "חניה", 'ממ"ד/מקלט', "תיווך/פרטי", data.TIMES_POSTED, data.POST_DATE, data.SOURCE]],
        hide_index=True, width="stretch", height=height,
        column_config={
            "לינק למודעה": st.column_config.LinkColumn("מודעה", display_text="פתיחה"),
            data.MAPS: st.column_config.LinkColumn("מפה", display_text="🗺"),
            data.SCORE: st.column_config.ProgressColumn("ציון", min_value=0, max_value=100, format="%d"),
            data.PRICE: st.column_config.NumberColumn("מחיר", format="₪%d"),
            "עלות חודשית כוללת": st.column_config.NumberColumn("עלות כוללת", format="₪%d"),
            data.STATUS: st.column_config.SelectboxColumn("מעקב", options=list(data.STATUS_LABELS.values())),
            data.NOTE: st.column_config.TextColumn("הערה"),
            DETAILS: st.column_config.LinkColumn("כרטיס", display_text="🏡"),
            data.PRICE_CHANGE: st.column_config.TextColumn("שינוי מחיר",
                                                           help="המחיר עכשיו פחות המחיר בפרסום הראשון של אותה דירה"),
            data.TIMES_POSTED: st.column_config.NumberColumn("פרסומים", help="כמה פעמים הדירה פורסמה"),
        }, **extra)


def _map_deck(placed: pd.DataFrame, zoom: float = 12.5) -> pdk.Deck:
    points = pd.DataFrame({
        "lat": placed[data.LAT], "lon": placed[data.LON],
        "color": placed[data.SCORE].map(_score_color),
        "price": placed[data.PRICE].map(lambda p: f"₪{p:,.0f}" if pd.notna(p) else "?"),
        "rooms": placed[data.ROOMS].map(lambda r: f"{r:g}" if pd.notna(r) else "?"),
        "score": placed[data.SCORE].map(lambda v: f"{v:.0f}" if pd.notna(v) else "-"),
        "distance": placed[data.DISTANCE].map(lambda d: f"{d:g}" if pd.notna(d) else "?"),
        "address": placed[data.ADDRESS], "url": placed["לינק למודעה"], "maps": placed[data.MAPS],
    })
    home = pd.DataFrame([{"lat": config.DESTINATION_LAT, "lon": config.DESTINATION_LON}])
    # The walking limit drawn as a circle: a straight-line radius, so the walk is shorter by the same
    # calibration factor the free distance estimate uses.
    reach = pdk.Layer("ScatterplotLayer", home, id="reach", get_position="[lon, lat]",
                      get_radius=config.MAX_WALKING_DISTANCE_KM * 1000 / config.STRAIGHT_LINE_CALIBRATION_FACTOR,
                      filled=False, stroked=True, get_line_color=[255, 215, 0, 140], line_width_min_pixels=2)
    listings = pdk.Layer("ScatterplotLayer", points, id="listings", get_position="[lon, lat]", get_fill_color="color",
                         get_radius=70, radius_min_pixels=6, radius_max_pixels=18, pickable=True,
                         stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=1)
    destination = pdk.Layer("ScatterplotLayer", home, id="destination", get_position="[lon, lat]", get_radius=120,
                            radius_min_pixels=11, get_fill_color=[255, 200, 0], stroked=True,
                            get_line_color=[0, 0, 0], line_width_min_pixels=3)
    # One listing (the apartment page): centred between it and the destination; else on the destination.
    lat = (placed[data.LAT].iloc[0] + config.DESTINATION_LAT) / 2 if len(placed) == 1 else config.DESTINATION_LAT
    lon = (placed[data.LON].iloc[0] + config.DESTINATION_LON) / 2 if len(placed) == 1 else config.DESTINATION_LON
    view = pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom)
    return pdk.Deck(layers=[reach, listings, destination], initial_view_state=view,
                    tooltip={"html": "<b>{price}</b> · {rooms} חד' · ציון {score}<br/>{address}<br/>{distance} ק\"מ הליכה",
                             "style": {"direction": "rtl"}})


def _score_color(score) -> list[int]:
    """Red for a low fit score through yellow to green for a high one; grey without a score."""
    if pd.isna(score):
        return [150, 150, 150, 200]
    share = max(0.0, min(1.0, score / 100))
    red = 255 if share < 0.5 else int(255 * (1 - share) * 2)
    green = 255 if share > 0.5 else int(255 * share * 2)
    return [red, green, 60, 220]


def _listing_card(listing: dict):
    with st.container(border=True):
        st.markdown(f"**{listing['price']}** · {listing['rooms']} חדרים · ציון {listing['score']} · "
                    f"{listing['distance']} ק\"מ הליכה  \n{listing['address']}")
        left, middle, right = st.columns([2, 1, 1])
        tracked = storage.get_listing_tracking().get(listing["url"], {})
        labels = list(data.STATUS_LABELS.values())
        status = left.selectbox("מעקב", labels, index=labels.index(data.STATUS_LABELS[tracked.get("status", "")]),
                                key=f"status_{listing['url']}")
        if data.STATUS_KEYS[status] != tracked.get("status", ""):
            storage.set_listing_tracking(listing["url"], data.STATUS_KEYS[status], tracked.get("note", ""))
            st.toast("נשמר")
        middle.link_button("🏡 כרטיס מלא", f"apartment?u={quote(listing['url'], safe='')}")
        right.link_button("פתיחת המודעה", listing["url"])


def apartment_page():
    st.title("🏡 כרטיס דירה")
    frame = _with_extras(_listings())
    if frame.empty:
        st.info("עוד אין דירות. הריצו סריקה מהתפריט בצד.")
        return
    urls = list(frame["לינק למודעה"])
    wanted = st.query_params.get("u")
    picked = st.selectbox("דירה", urls, index=urls.index(wanted) if wanted in urls else 0,
                          format_func=lambda url: _listing_title(frame[frame["לינק למודעה"] == url].iloc[0]))
    st.query_params["u"] = picked
    row = frame[frame["לינק למודעה"] == picked].iloc[0]

    cols = st.columns(5)
    cols[0].metric("מחיר", _money(row[data.PRICE]), _price_change_text(row[data.PRICE_CHANGE]), delta_color="inverse")
    cols[1].metric("עלות חודשית כוללת", _money(row["עלות חודשית כוללת"]))
    cols[2].metric("חדרים", f"{row[data.ROOMS]:g}" if pd.notna(row[data.ROOMS]) else "—")
    accuracy = str(row["דיוק מרחק"]).replace("הערכה ", "") if row["דיוק מרחק"] else ""
    cols[3].metric('הליכה (ק"מ)', f"{row[data.DISTANCE]:g} {accuracy}".strip() if pd.notna(row[data.DISTANCE]) else "—",
                   help="Google = מסלול הליכה אמיתי; ± = הערכה חינמית, בטווח הזה" if accuracy else None)
    cols[4].metric("ציון התאמה", f"{row[data.SCORE]:.0f}" if pd.notna(row[data.SCORE]) else "—")

    post = storage.get_post(picked) or {}
    _contact_buttons(row, post.get("raw_text") or "")
    _tracking_editor(picked)

    left, right = st.columns(2)
    with left:
        st.subheader("למה הציון הזה")
        parts = data.score_parts(row)
        st.dataframe(pd.DataFrame([{"חלק": label, "נקודות": round(points, 1), "מתוך": weight}
                                   for label, points, weight in parts]),
                     hide_index=True, width="stretch",
                     column_config={"נקודות": st.column_config.ProgressColumn("נקודות", min_value=0, max_value=25,
                                                                               format="%.1f")})
        total = min(100, round(sum(points for _, points, _ in parts)))
        if pd.notna(row[data.SCORE]) and total != round(row[data.SCORE]):
            st.caption(f"לפי הנוסחה היום: {total}. הציון בטבלה ({row[data.SCORE]:.0f}) חושב כשהדירה נוספה, "
                       "או קיבל תוספת מהצבעות בטלגרם.")
        st.subheader("פרטים")
        details = ["תאריך כניסה", "קומה", "מעלית", "חניה", 'ממ"ד/מקלט', "תיווך/פרטי", "ארנונה (לחודשיים)",
                   "ועד בית (לחודשיים)", data.POST_DATE, "זמן סריקה", data.SOURCE]
        st.dataframe(pd.DataFrame({"": details, "ערך": [data.display_text(row[d]) or "—" for d in details]}),
                     hide_index=True, width="stretch")
    with right:
        placed = data.with_coordinates(frame[frame["לינק למודעה"] == picked]).dropna(subset=[data.LAT, data.LON])
        if placed.empty:
            st.caption("הכתובת לא מוקמה במפה.")
        else:
            st.pydeck_chart(_map_deck(placed, zoom=14), height=330)
        _price_history(row)

    st.subheader("המודעה המלאה")
    if post.get("raw_text"):
        st.markdown(f'<div dir="rtl" style="white-space: pre-wrap; line-height: 1.6">{html.escape(post["raw_text"])}</div>',
                    unsafe_allow_html=True)
    else:
        st.caption("הטקסט כבר לא שמור (נשמר 14 יום) — פתחו את המודעה.")


def compare_page():
    st.title("⚖️ השוואה")
    frame = _with_extras(_listings())
    if frame.empty:
        st.info("עוד אין דירות. הריצו סריקה מהתפריט בצד.")
        return
    urls = list(frame["לינק למודעה"])
    chosen = st.multiselect("בחרו עד 4 דירות", urls, default=[u for u in st.session_state.get("compare", []) if u in urls],
                            max_selections=_COMPARE_MAX, placeholder="דירות להשוואה",
                            format_func=lambda url: _listing_title(frame[frame["לינק למודעה"] == url].iloc[0]))
    st.session_state["compare"] = chosen
    if len(chosen) < 2:
        st.caption("בחרו לפחות 2 דירות — או לחצו ⚖️ להשוואה בכרטיס של דירה.")
        return
    listings = frame.set_index("לינק למודעה").loc[chosen].reset_index()
    table = data.compare_frame(listings)
    st.dataframe(table.style.apply(_best_in_row, axis=1), width="stretch", height=640)
    st.caption("ירוק = הכי טוב בשורה (הכי זול, הכי קרוב, הכי גבוה בציון).")
    cols = st.columns(len(chosen))
    for col, (_, row) in zip(cols, listings.iterrows()):
        col.link_button("כרטיס דירה", f"apartment?u={quote(row['לינק למודעה'], safe='')}")


@st.cache_data(ttl=_CACHE_S, show_spinner=False)
def _history() -> dict:
    return data.history_by_flat(storage.get_priced_posts())


def _with_extras(frame: pd.DataFrame) -> pd.DataFrame:
    """The listings with the user's tracking and each flat's posting history."""
    return data.with_history(data.with_tracking(frame, storage.get_listing_tracking()), _history())


def _listing_title(row: pd.Series) -> str:
    price = _money(row[data.PRICE])
    rooms = f"{row[data.ROOMS]:g} חד'" if pd.notna(row[data.ROOMS]) else ""
    return " · ".join(part for part in (row[data.ADDRESS] or "בלי כתובת", price, rooms) if part)


def _money(value) -> str:
    return f"₪{value:,.0f}" if pd.notna(value) else "—"


def _price_change_text(change) -> str | None:
    return None if change is None or pd.isna(change) else f"{change:+,.0f} ₪ מאז הפרסום הראשון"


def _contact_buttons(row: pd.Series, post_text: str):
    cols = st.columns(4)
    cols[0].link_button("פתיחת המודעה", row["לינק למודעה"])
    if row[data.MAPS]:
        cols[1].link_button("Google Maps", row[data.MAPS])
    phone = _first_mobile_phone(post_text)
    if phone:
        cols[2].link_button("וואטסאפ", f"https://wa.me/972{phone[1:]}")
    compare = st.session_state.setdefault("compare", [])
    if row["לינק למודעה"] not in compare and cols[3].button("⚖️ להשוואה", disabled=len(compare) >= _COMPARE_MAX):
        compare.append(row["לינק למודעה"])
        st.toast("נוספה להשוואה")


def _tracking_editor(url: str):
    tracked = storage.get_listing_tracking().get(url, {})
    labels = list(data.STATUS_LABELS.values())
    with st.form(f"tracking_{url}"):
        left, right = st.columns([1, 3])
        status = left.selectbox("מעקב", labels, index=labels.index(data.STATUS_LABELS[tracked.get("status", "")]))
        note = right.text_input("הערה", value=tracked.get("note", ""))
        if st.form_submit_button("שמירה"):
            storage.set_listing_tracking(url, data.STATUS_KEYS[status], note)
            st.toast("נשמר")


def _price_history(row: pd.Series):
    posts = data.flat_history(_history(), row[data.ADDRESS], row[data.ROOMS])
    if len(posts) < 2:
        st.caption("פורסמה פעם אחת עד עכשיו.")
        return
    st.subheader(f"פורסמה {len(posts)} פעמים")
    if not data.is_exact_flat(row[data.ADDRESS]):
        st.caption("בלי מספר בית — ייתכן שחלק מהפרסומים הם דירה אחרת באותו רחוב עם אותו מספר חדרים.")
    st.dataframe(pd.DataFrame([{"תאריך": t["day"], "מחיר": t["price"], "פרסומים": t["posts"], "מודעה": t["url"]}
                               for t in data.price_timeline(posts)]),
                 hide_index=True, width="stretch",
                 column_config={"מחיר": st.column_config.NumberColumn("מחיר", format="₪%d"),
                                "מודעה": st.column_config.LinkColumn("מודעה", display_text="פתיחה")})


def _price_change_cell(change) -> str:
    return "" if change is None or pd.isna(change) else f"{'↓' if change < 0 else '↑'} {abs(change):,.0f} ₪"


def _best_in_row(values: pd.Series) -> list[str]:
    # Lower is better for money and distance, higher for the score; other rows aren't ranked.
    better = {"ציון": max, "מחיר": min, "עלות חודשית כוללת": min, 'מרחק הליכה (ק"מ)': min}.get(values.name)
    numbers = pd.to_numeric(values.str.replace(",", ""), errors="coerce")
    if better is None or numbers.isna().all():
        return [""] * len(values)
    best = better(numbers.dropna())
    return ["background-color: rgba(40, 167, 69, 0.35)" if n == best else "" for n in numbers]


def groups_page():
    st.title("👥 קבוצות")
    locked = components.edits_locked()
    groups = storage.get_groups()
    if groups:
        table = data.groups_frame(groups, storage.get_group_overview())
        edited = st.data_editor(
            table, hide_index=True, width="stretch", disabled=[c for c in table.columns if c != "active"],
            column_config={"url": st.column_config.LinkColumn("קישור", display_text="פתיחה"),
                           "name": "שם הקבוצה", "active": st.column_config.CheckboxColumn("פעילה")},
            key="groups_table")
        changed = edited[edited["active"] != table["active"]]
        if not changed.empty and st.button("שמירת השינויים", type="primary", disabled=locked):
            spreadsheet = open_spreadsheet()
            for _, row in changed.iterrows():
                set_group_active(spreadsheet, row["url"], bool(row["active"]))
            st.rerun()
        with st.expander("הסרת קבוצה"):
            doomed = st.selectbox("קבוצה", groups, format_func=lambda g: g["name"] or g["url"], index=None)
            if doomed and st.button("הסרה", disabled=locked):
                remove_group(open_spreadsheet(), doomed["url"])
                st.rerun()
    st.subheader("הוספת קבוצות")
    components.add_groups_section(locked)


def settings_page():
    st.title("⚙️ הגדרות")
    locked = components.edits_locked()
    with st.expander("📍 כתובת יעד", expanded=False):
        components.destination_section(locked)
    with st.expander("💰 תקציב וגודל", expanded=True):
        components.budget_section(locked)
    with st.expander("🤖 ה-AI שקורא את הפוסטים"):
        components.ai_section(locked)
    with st.expander("📘 פייסבוק"):
        components.facebook_section(locked)
    with st.expander("🔧 סינון מתקדם"):
        components.advanced_section(locked)
    with st.expander("📂 איפה התוצאות נשמרות"):
        components.sheets_section(locked)
    st.subheader("🧩 תוספים")
    with st.expander("📱 טלגרם"):
        components.telegram_section(locked)
    with st.expander("🕒 סריקות אוטומטיות"):
        components.schedule_section(locked)
    with st.expander("📧 מיילים של יד2"):
        components.yad2_section(locked)
    with st.expander("🗺 מסלולי הליכה של Google Maps"):
        components.maps_section(locked)


def tools_page():
    st.title("🛠 כלים")
    st.subheader("יומני סריקה")
    runs = [r for r in storage.get_recent_runs(30) if r.get("log_path")]
    if not runs:
        st.caption("עוד אין סריקות.")
    else:
        run = st.selectbox("סריקה", runs, format_func=_run_label)
        only_problems = st.checkbox("רק שגיאות ואזהרות", value=False)
        lines = data.log_lines(scan_runner.PROJECT_DIR / run["log_path"], only_problems)
        if lines is None:
            st.caption("קובץ היומן כבר נמחק (נשמרים 14 יום).")
        else:
            st.caption(f"{run.get('errors') or 0} שגיאות · {run.get('warnings') or 0} אזהרות · {len(lines)} שורות")
            st.code("\n".join(lines[-_LOG_MAX_LINES:]) or "(ריק)", language=None, height=400)
    st.subheader("תחזוקה")
    locked = components.edits_locked()
    for label, args, help_text in _MAINTENANCE:
        if st.button(label, help=help_text, disabled=locked, key=f"tool_{label}"):
            with st.spinner(f"{label}..."):
                code, stdout, stderr = scan_runner.run_cli(args, timeout_s=_TOOL_TIMEOUT_S)
            (st.success if code == 0 else st.error)(f"{label}: {'הסתיים' if code == 0 else f'נכשל (קוד {code})'}")
            st.code(((stdout + stderr).strip() or "(אין פלט)")[-_TOOL_OUTPUT_CHARS:], language=None)


def _run_label(run: dict) -> str:
    kind = data.RUN_TYPES.get(run["mode"], "ניסיון" if run["mode"].startswith("dry_run") else run["mode"])
    return (f"{run['started_at'].replace('T', ' ')[:16]} · {kind} · "
            f"{data.RUN_STATUSES.get(run['status'], run['status'])} · {run.get('added') or 0} נוספו")


def stats_page():
    st.title("📊 סטטיסטיקה")
    counts, _, gmaps_calls = storage.get_stats()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    cols = st.columns(4)
    cols[0].metric("דירות שנוספו", f"{counts.get(storage.VERDICT_ADDED, 0):,}")
    cols[1].metric("בשבוע האחרון", len(storage.get_run_activity(week_ago)["added"]))
    cols[2].metric("פוסטים שנקראו", f"{sum(counts.values()):,}")
    cols[3].metric("קבוצות פעילות", sum(1 for g in storage.get_groups() if g["active"]))
    per_day = storage.get_added_per_day(30)
    if per_day:
        st.subheader("דירות שנוספו ביום (30 יום)")
        st.bar_chart(pd.Series({f"{day[8:10]}/{day[5:7]}": n for day, n in per_day.items()}, name="דירות"))
    st.subheader("סריקות אחרונות")
    runs = storage.get_recent_runs(10)
    if runs:
        st.dataframe(pd.DataFrame([{
            "התחלה": r["started_at"].replace("T", " ")[:16],
            "סוג": data.RUN_TYPES.get(r["mode"], "ניסיון" if r["mode"].startswith("dry_run") else r["mode"]),
            "סטטוס": data.RUN_STATUSES.get(r["status"], r["status"]), "נוספו": r.get("added") or 0, "פוסטים": r.get("posts_seen") or 0,
            "שגיאות": r.get("errors") or 0} for r in runs]), hide_index=True, width="stretch")
    st.subheader("הקבוצות לפי אחוז התאמה")
    groups = storage.get_groups()
    if groups:
        table = data.groups_frame(groups, storage.get_group_overview())
        st.dataframe(table.drop(columns=["url", "active"]), hide_index=True, width="stretch")
    if gmaps_calls:
        st.caption(f"קריאות Google Maps החודש: {gmaps_calls}")
    st.subheader("בדיקת תקינות")
    if st.button("הרצת בדיקה"):
        with st.spinner("בודק..."):
            _, stdout, _ = scan_runner.run_cli(["doctor.py", "--json"])
        try:
            rows = json.loads(stdout)
        except ValueError:
            st.error("הבדיקה לא הצליחה לרוץ.")
            return
        icons = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "OFF": "⚪"}
        st.dataframe(pd.DataFrame([{"": icons.get(s, s), "רכיב": n, "פרטים": d, "תיקון": r} for n, s, d, r in rows]),
                     hide_index=True, width="stretch")


def setup_page():
    st.title("👋 ברוכים הבאים — הגדרה ראשונה")
    st.write("כמה שאלות קצרות, ואפשר להתחיל לחפש. כל שלב נשמר לבד.")
    missing = data.setup_missing()
    locked = components.edits_locked()
    with st.container(border=True):
        st.subheader(("✅ " if "destination" not in missing else "1. ") + "לאן הולכים ברגל?")
        components.destination_section(locked)
    with st.container(border=True):
        st.subheader("2. מה התקציב והגודל?")
        components.budget_section(locked)
    with st.container(border=True):
        st.subheader(("✅ " if "ai" not in missing else "3. ") + "מי קורא את הפוסטים?")
        components.ai_section(locked)
    with st.container(border=True):
        st.subheader("4. פייסבוק")
        components.facebook_section(locked)
    with st.container(border=True):
        st.subheader(("✅ " if "groups" not in missing else "5. ") + "אילו קבוצות לסרוק?")
        components.add_groups_section(locked)
    if missing:
        st.warning("חסר עוד: " + ", ".join({"destination": "כתובת יעד", "ai": "בחירת AI", "groups": "קבוצות"}[m]
                                             for m in missing))
    elif st.button("🎉 סיום — לסריקה הראשונה", type="primary"):
        scan_runner.start_scan([])
        st.rerun()  # setup is complete now, so the app reopens on the results page
