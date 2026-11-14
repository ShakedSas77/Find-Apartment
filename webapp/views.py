"""
The web app's pages. Each is a function st.navigation runs; data comes from webapp/data.py and the
settings sections from webapp/components.py.
"""
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import pydeck as pdk
import streamlit as st

import config
from core import scan_runner, storage
from integrations.groups_sync import remove_group, set_group_active
from integrations.sheets import open_spreadsheet
from webapp import components, data

_CACHE_S = 60


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
    shown = _filtered(frame, "results")
    sort_by = st.radio("מיון", [data.SCORE, data.POST_DATE, data.PRICE, data.DISTANCE], horizontal=True)
    if sort_by != data.POST_DATE:  # the file itself is kept newest post first
        shown = shown.sort_values(sort_by, ascending=sort_by != data.SCORE, na_position="last")
    st.caption(f"{len(shown)} מתוך {len(frame)} דירות")
    _listings_table(shown, height=600)
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
    shown = _filtered(frame, "map", more=True)
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
        min_score = max_age_days = None
        if more:
            left, right = st.columns(2)
            min_score = left.slider("ציון התאמה מינימלי", 0, 100, 0, key=f"{key}_score") or None
            max_age_days = right.slider("פורסמו ב-X הימים האחרונים", 1, config.MAX_POST_AGE_DAYS,
                                        config.MAX_POST_AGE_DAYS, key=f"{key}_days")
    return data.filter_listings(frame, max_price, min_rooms, max_km, sources, text, min_score, max_age_days)


def _listings_table(frame: pd.DataFrame, height: int):
    st.dataframe(
        frame[["לינק למודעה", data.SCORE, data.PRICE, data.ROOMS, data.DISTANCE, "דיוק מרחק", data.ADDRESS, data.MAPS,
               "עלות חודשית כוללת", "תאריך כניסה", "קומה", "מעלית", "חניה", 'ממ"ד/מקלט', "תיווך/פרטי",
               data.POST_DATE, data.SOURCE]],
        hide_index=True, width="stretch", height=height,
        column_config={
            "לינק למודעה": st.column_config.LinkColumn("מודעה", display_text="פתיחה"),
            data.MAPS: st.column_config.LinkColumn("מפה", display_text="🗺"),
            data.SCORE: st.column_config.ProgressColumn("ציון", min_value=0, max_value=100, format="%d"),
            data.PRICE: st.column_config.NumberColumn("מחיר", format="₪%d"),
            "עלות חודשית כוללת": st.column_config.NumberColumn("עלות כוללת", format="₪%d"),
        })


def _map_deck(placed: pd.DataFrame) -> pdk.Deck:
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
    view = pdk.ViewState(latitude=config.DESTINATION_LAT, longitude=config.DESTINATION_LON, zoom=12.5)
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
        left, right = st.columns(2)
        left.link_button("פתיחת המודעה", listing["url"])
        if listing.get("maps"):
            right.link_button("Google Maps", listing["maps"])


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
