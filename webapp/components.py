"""
Building blocks shared by the web app's pages: the settings sections the Setup and Settings pages both
show, adding groups, and the scan panel in the sidebar. Each saves on its own button.
"""
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st
from google import genai

import config
from core import scan_runner, storage
from core.local_settings import is_saved, save_secrets, save_settings
from integrations.distance_fallback import nominatim_geocode
from integrations.groups_sync import add_groups, canonical_group_url
from integrations.sheets import open_spreadsheet
from webapp import data

_LOGIN_TIMEOUT_S = config.LOGIN_WAIT_S + 60
_OLLAMA_PULL_TIMEOUT_S = 3600
_AI_CHOICES = {"none": "בלי AI — בלי הגדרה, פחות מדויק", "gemini": "Gemini — מפתח חינמי של Google (מומלץ)",
               "ollama": "Ollama — AI על המחשב הזה, בלי חשבון"}


def is_scan_running() -> bool:
    return bool(scan_runner.running_since())


def edits_locked() -> bool:
    """While a scan runs it holds the results file and rewrites the Groups tab when it ends, so a change
    made now would be lost: the forms that write are disabled meanwhile."""
    if is_scan_running():
        st.info("סריקה רצה עכשיו — אפשר לשנות הגדרות וקבוצות כשהיא תסתיים.")
        return True
    return False


def destination_section(locked: bool):
    current = config.DESTINATION_ADDRESS if is_saved("DESTINATION_ADDRESS") else ""
    st.caption("מרחק ההליכה נמדד לכתובת אחת: עבודה, בית ספר, משפחה...")
    address = st.text_input("כתובת יעד (רחוב, מספר, עיר)", value=current, disabled=locked)
    if st.button("חיפוש במפה", disabled=locked or not address):
        found = nominatim_geocode(address)
        if found is None:
            st.error("הכתובת לא נמצאה במפה — נסו לכתוב אותה אחרת, עם שם העיר.")
        else:
            st.session_state["destination_found"] = (address, found[0], found[1])
    if "destination_found" in st.session_state:
        address, lat, lon = st.session_state["destination_found"]
        st.map([{"lat": lat, "lon": lon}], zoom=14, height=250)
        if st.button("זו הכתובת — שמירה", type="primary", disabled=locked):
            # The south-of cutoff in config.py belongs to the original Tel Aviv destination.
            save_settings({"DESTINATION_ADDRESS": address, "DESTINATION_LAT": lat, "DESTINATION_LON": lon,
                           "EXCLUDE_SOUTH_OF_LAT": None})
            del st.session_state["destination_found"]
            st.success("נשמר.")
            st.rerun()
    elif current:
        st.map([{"lat": config.DESTINATION_LAT, "lon": config.DESTINATION_LON}], zoom=14, height=250)


def budget_section(locked: bool):
    with st.form("budget"):
        left, right = st.columns(2)
        min_price = left.number_input("שכר דירה מינימלי (₪)", value=int(config.MIN_PRICE), step=100, key="min_price")
        max_price = right.number_input("שכר דירה מקסימלי (₪)", value=int(config.MAX_PRICE), step=100, key="max_price")
        min_rooms = left.number_input("מינימום חדרים", value=float(config.MIN_ROOMS), step=0.5, key="min_rooms")
        max_rooms = right.number_input("מקסימום חדרים", value=float(config.MAX_ROOMS), step=0.5, key="max_rooms")
        max_km = left.number_input('מרחק הליכה מקסימלי (ק"מ)', value=float(config.MAX_WALKING_DISTANCE_KM), step=0.5,
                                 key="max_km")
        if st.form_submit_button("שמירה", type="primary", disabled=locked):
            if min_price > max_price or min_rooms > max_rooms:
                st.error("המינימום גדול מהמקסימום.")
                return
            save_settings({"MIN_PRICE": int(min_price), "MAX_PRICE": int(max_price), "MIN_ROOMS": min_rooms,
                           "MAX_ROOMS": max_rooms, "MAX_WALKING_DISTANCE_KM": max_km})
            st.success("נשמר — יחול בסריקה הבאה.")


def ai_section(locked: bool):
    st.caption("ה-AI קורא כל פוסט ומוציא ממנו מחיר, חדרים, כתובת ותאריך כניסה.")
    keys = list(_AI_CHOICES)
    choice = st.radio("מי קורא את הפוסטים?", keys, format_func=_AI_CHOICES.get,
                      index=keys.index(config.LLM_PROVIDER), disabled=locked)
    if choice == "none":
        st.caption("כללים פשוטים מוצאים מחיר, חדרים ורחוב כשהפוסט כותב 'רחוב ...'. בבדיקה על פוסטים אמיתיים "
                   "זה תפס כ-9 מכל 10 דירות שה-AI מצא, אבל בלי תאריך כניסה, קומה ופרטים נוספים.")
        if st.button("שמירה", key="ai_none", disabled=locked):
            save_settings({"LLM_PROVIDER": "none"})
            st.success("נשמר.")
    elif choice == "gemini":
        st.markdown("1. פתחו את [Google AI Studio](https://aistudio.google.com/app/apikey) והתחברו עם חשבון Google.\n"
                    "2. לחצו **Create API key** והעתיקו את המפתח.\n3. הדביקו אותו כאן:")
        key = st.text_input("מפתח Gemini", type="password",
                            placeholder="שמור — השאירו ריק כדי לא לשנות" if os.getenv("GEMINI_API_KEY") else "AIza...")
        if st.button("בדיקה ושמירה", key="ai_gemini", disabled=locked):
            key = key.strip() or os.getenv("GEMINI_API_KEY") or ""
            if not _gemini_key_works(key):
                st.error("המפתח לא עובד — ודאו שהעתקתם את כולו.")
                return
            save_secrets({"GEMINI_API_KEY": key})
            save_settings({"LLM_PROVIDER": "gemini"})
            st.success("המפתח עובד ונשמר.")
    else:
        exe = _ollama_exe()
        if not exe:
            st.markdown("Ollama לא מותקן: [הורדה](https://ollama.com/download/windows), התקנה, ואז רעננו את הדף.")
            return
        st.caption(f"המודל {config.OLLAMA_MODEL} יורד פעם אחת (כמה GB).")
        if st.button("הורדת המודל ושמירה", key="ai_ollama", disabled=locked):
            with st.spinner("מוריד את המודל... זה לוקח זמן בפעם הראשונה"):
                result = subprocess.run([exe, "pull", config.OLLAMA_MODEL], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=_OLLAMA_PULL_TIMEOUT_S)
            if result.returncode != 0:
                st.error(f"ההורדה נכשלה: {result.stderr.strip()[-300:]}")
                return
            save_settings({"LLM_PROVIDER": "ollama"})
            st.success("המודל מוכן ונשמר.")


def facebook_section(locked: bool):
    logged_in_before = (scan_runner.PROJECT_DIR / "chrome_profile").exists()
    choice = st.radio("איך הבוט קורא את פייסבוק?", ["login", "guest"], index=0 if config.FACEBOOK_LOGIN else 1,
                      format_func={"login": "עם חשבון הפייסבוק שלי (מומלץ)",
                                   "guest": "בלי התחברות — מעט מאוד פוסטים, פייסבוק חוסם מבקרים"}.get,
                      disabled=locked)
    if choice != ("login" if config.FACEBOOK_LOGIN else "guest"):
        save_settings({"FACEBOOK_LOGIN": choice == "login"})
    if choice == "guest":
        return
    st.caption("נפתח חלון דפדפן נפרד של הבוט. התחברו בו לפייסבוק כרגיל; החלון ייסגר לבד כשתראו את הפיד. "
               + ("(הבוט כבר התחבר בעבר — צריך שוב רק אם פייסבוק ניתק אותו.)" if logged_in_before else ""))
    if st.button("התחברות לפייסבוק", disabled=locked):
        with st.spinner("מחכה שתתחברו בחלון שנפתח..."):
            code, _, _ = scan_runner.run_cli(["apartment_bot.py", "--login"], timeout_s=_LOGIN_TIMEOUT_S)
        if code != 0:
            st.error("ההתחברות לא הושלמה — נסו שוב.")
        else:
            st.success("מחובר.")


def add_groups_section(locked: bool):
    if config.FACEBOOK_LOGIN:
        if st.button("הצגת הקבוצות שלי מפייסבוק", disabled=locked):
            with st.spinner("קורא את רשימת הקבוצות שלכם מפייסבוק (כדקה)..."):
                _, stdout, _ = scan_runner.run_cli(["apartment_bot.py", "--list-my-groups"])
            try:
                st.session_state["my_groups"] = json.loads(stdout)
            except ValueError:
                st.error("לא הצלחתי לקרוא את הקבוצות — התחברו לפייסבוק קודם.")
        if st.session_state.get("my_groups"):
            known = {g["url"] for g in storage.get_groups()}
            options = [g for g in st.session_state["my_groups"] if g["url"] not in known]
            if not options:
                st.caption("כל הקבוצות שלכם כבר ברשימה.")
            else:
                st.caption("קבוצות שנשמעות כמו דירות להשכרה כבר מסומנות.")
                table = st.data_editor(
                    [{"הוספה": g["looks_like_rentals"], "קבוצה": g["name"] or g["url"], "url": g["url"]} for g in options],
                    column_config={"url": None}, disabled=["קבוצה"], hide_index=True, key="pick_groups")
                if st.button("הוספת המסומנות", type="primary", disabled=locked):
                    _add([row["url"] for row in table if row["הוספה"]])
    links = st.text_area("או הדביקו קישורים לקבוצות, אחד בכל שורה (facebook.com/groups/...)", disabled=locked)
    if st.button("הוספת הקישורים", disabled=locked or not links.strip()):
        valid = [line.strip() for line in links.splitlines() if canonical_group_url(line.strip())]
        if len(valid) < len([line for line in links.splitlines() if line.strip()]):
            st.warning("חלק מהשורות אינן קישור לקבוצה בפייסבוק ודולגו.")
        _add(valid)


def scan_panel():
    with st.sidebar:
        st.header("סריקה")
        _scan_status()
        running = is_scan_running()
        left, right = st.columns(2)
        if left.button("▶️ סריקה מלאה", disabled=running, width="stretch"):
            _start([])
        if right.button("⏹ עצירה", disabled=not running, width="stretch"):
            scan_runner.stop_scan()
            st.rerun()
        left, right = st.columns(2)
        if left.button("📘 רק פייסבוק", disabled=running, width="stretch"):
            _start(["--source", "fb"])
        if config.ALERTS_SCAN_ENABLED and right.button("📧 רק יד2", disabled=running, width="stretch"):
            _start(["--source", "yad2"])
        groups = [g for g in storage.get_groups() if g["active"]]
        if groups:
            group = st.selectbox("קבוצה אחת", groups, format_func=lambda g: g["name"] or g["url"], index=None,
                                 placeholder="בחרו קבוצה לסריקה")
            if group and st.button("סריקת הקבוצה", disabled=running):
                _start(["--group", group["url"]])
        paused = scan_runner.paused_since()
        if st.toggle("השהיית הסריקות האוטומטיות", value=bool(paused),
                     help="משפיע רק על הסריקות המתוזמנות ב-Windows; סריקה מכאן תמיד עובדת."):
            if not paused:
                storage.set_bot_state(storage.SCANS_PAUSED_KEY, datetime.now().isoformat(timespec="seconds"))
        elif paused:
            storage.set_bot_state(storage.SCANS_PAUSED_KEY, "")


@st.fragment(run_every=5)
def _scan_status():
    # Re-runs alone every 5s, so the progress line moves without reloading the page.
    running = scan_runner.running_since()
    if running:
        st.info(f"⏳ סריקה רצה (התחילה {running})  \n{scan_runner.fresh_progress() or 'מתחילה...'}")
        return
    last = scan_runner.last_finished_run()
    if last:
        st.caption(f"סריקה אחרונה: {last['started_at'].replace('T', ' ')[:16]} · "
                   f"{data.RUN_STATUSES.get(last['status'], last['status'])} · {last.get('added') or 0} דירות נוספו")


def _start(args: list[str]):
    running = scan_runner.start_scan(args)
    if running:
        st.warning(f"סריקה כבר רצה (התחילה {running})")
    else:
        st.toast("הסריקה התחילה")
        st.rerun()


def _add(links: list[str]):
    if not links:
        return
    added = add_groups(open_spreadsheet(), links)
    st.session_state.pop("my_groups", None)
    st.success(f"נוספו {len(added)} קבוצות.")
    st.rerun()


def _gemini_key_works(key: str) -> bool:
    try:
        return any(True for _ in genai.Client(api_key=key).models.list())
    except Exception:
        return False


def _ollama_exe() -> str | None:
    found = shutil.which("ollama")
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
    return found or (str(default) if default.exists() else None)
