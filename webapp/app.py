"""
=== Web app ===

The bot's browser interface, for this PC only: first-run setup, results, groups, settings and stats,
with a scan panel on every page. Start it with open_app.bat, or:

    streamlit run webapp/app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
"""
import os
import sys
from pathlib import Path

import streamlit as st

# `streamlit run` puts only webapp/ on the import path; the bot's modules live in the project root,
# and its files (bot_data.db, listings.xlsx, config_local.py) are found relative to it.
_PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_DIR))
os.chdir(_PROJECT_DIR)

from core import storage  # noqa: E402
from webapp import components, data, views  # noqa: E402

_RTL_CSS = """<style>
.stApp, .stMarkdown, .stTextInput, .stNumberInput, .stRadio, .stSelectbox, .stMultiSelect, .stTextArea,
[data-testid="stSidebar"], [data-testid="stExpander"], [data-testid="stMetric"] { direction: rtl; text-align: right; }
/* Logs and command output are English: keep them left-to-right. */
[data-testid="stCode"], [data-testid="stCode"] * { direction: ltr; text-align: left; }
</style>"""

st.set_page_config(page_title="מחפש דירות", page_icon="🏠", layout="wide")
st.markdown(_RTL_CSS, unsafe_allow_html=True)
storage.init_db()

results = st.Page(views.results_page, title="דירות", icon="🏠", default=True)
if data.setup_missing():
    page = st.navigation([st.Page(views.setup_page, title="הגדרה ראשונה", icon="👋")])
else:
    page = st.navigation([results, st.Page(views.map_page, title="מפה", icon="🗺"),
                          st.Page(views.groups_page, title="קבוצות", icon="👥"),
                          st.Page(views.settings_page, title="הגדרות", icon="⚙️"),
                          st.Page(views.stats_page, title="סטטיסטיקה", icon="📊"),
                          st.Page(views.tools_page, title="כלים", icon="🛠")])
    components.scan_panel()
page.run()
