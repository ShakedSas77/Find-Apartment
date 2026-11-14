"""
The web app's pages. Each is a function st.navigation runs; data comes from webapp/data.py and the
settings sections from webapp/components.py.
"""
import json
import os
from datetime import datetime, timedelta

import pandas as pd
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
    with st.container(border=True):
        cols = st.columns([1, 1, 1, 1.4, 1.4])
        prices = frame[data.PRICE].dropna()
        max_price = cols[0].number_input("עד מחיר (₪)", value=int(prices.max()) if len(prices) else 0, step=100)
        min_rooms = cols[1].number_input("מינימום חדרים", value=float(frame[data.ROOMS].min()), step=0.5)
        max_km = cols[2].number_input('עד מרחק (ק"מ)', value=float(config.MAX_WALKING_DISTANCE_KM), step=0.5)
        sources = cols[3].multiselect("מקור", sorted(frame[data.SOURCE].unique()), placeholder="הכול")
        text = cols[4].text_input("חיפוש בכתובת")
        sort_by = st.radio("מיון", [data.SCORE, data.POST_DATE, data.PRICE, data.DISTANCE], horizontal=True)
    shown = data.filter_listings(frame, max_price, min_rooms, max_km, sources, text)
    if sort_by != data.POST_DATE:  # the file itself is kept newest post first
        shown = shown.sort_values(sort_by, ascending=sort_by != data.SCORE, na_position="last")
    st.caption(f"{len(shown)} מתוך {len(frame)} דירות")
    st.dataframe(
        shown[["לינק למודעה", data.SCORE, data.PRICE, data.ROOMS, data.DISTANCE, "דיוק מרחק", data.ADDRESS, data.MAPS,
               "עלות חודשית כוללת", "תאריך כניסה", "קומה", "מעלית", "חניה", "ממ\"ד/מקלט", "תיווך/פרטי",
               data.POST_DATE, data.SOURCE]],
        hide_index=True, width="stretch", height=600,
        column_config={
            "לינק למודעה": st.column_config.LinkColumn("מודעה", display_text="פתיחה"),
            data.MAPS: st.column_config.LinkColumn("מפה", display_text="🗺"),
            data.SCORE: st.column_config.ProgressColumn("ציון", min_value=0, max_value=100, format="%d"),
            data.PRICE: st.column_config.NumberColumn("מחיר", format="₪%d"),
            "עלות חודשית כוללת": st.column_config.NumberColumn("עלות כוללת", format="₪%d"),
        })
    left, right = st.columns(2)
    if left.button("🔄 רענון"):
        _listings.clear()
        st.rerun()
    if config.OUTPUT_BACKEND == "excel" and right.button("📂 פתיחת הקובץ ב-Excel"):
        os.startfile(scan_runner.PROJECT_DIR / config.EXCEL_PATH)


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
        if config.OUTPUT_BACKEND == "excel":
            st.write(f"בקובץ Excel: `{scan_runner.PROJECT_DIR / config.EXCEL_PATH}`")
        else:
            st.write("ב-Google Sheet.")
        st.caption("מעבר בין Excel ל-Google Sheet: `venv\\Scripts\\python setup_wizard.py --add results` (מדריך: docs/user-guide.md, תוסף D)")
    with st.expander("🧩 תוספים"):
        add_ons = [("טלגרם — כל דירה חדשה לטלפון", config.TELEGRAM_ENABLED, "telegram"),
                   ("התראות יד2 מהמייל", config.ALERTS_SCAN_ENABLED, "yad2"),
                   ("מסלולי הליכה של Google Maps", config.GMAPS_ENABLED, "maps"),
                   ("סריקות יומיות אוטומטיות", None, "schedule")]
        for label, enabled, step in add_ons:
            state = "" if enabled is None else ("✅ פעיל" if enabled else "⚪ כבוי")
            st.markdown(f"**{label}** {state} — `venv\\Scripts\\python setup_wizard.py --add {step}`")
        st.caption("פירוט שלב-אחר-שלב לכל תוסף: docs/user-guide.md, חלק 3.")


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
