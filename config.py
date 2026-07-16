"""
=== הגדרות בוט דירות ===

ערוך את הערכים למטה כדי להתאים את הסריקה לצרכים שלך.
"""

# ─── מפתחות API ─────────────────────────────────────────────────────────────────
# Google Sheets – קובץ JSON של Service Account (הורד מ-Google Cloud Console)
CREDENTIALS_FILE = "credentials.json"

# Google Sheets – מזהה הטבלה (מתוך כתובת ה-URL)
SHEET_ID = "YOUR_SHEET_ID_HERE"

# Google Maps – מפתח API עבור Distance Matrix
# צור ב: https://console.cloud.google.com/apis/credentials

# Gemini LLM – מפתח API לניתוח מודעות
# צור ב: https://aistudio.google.com/app/apikey

# ─── כתובות יעד ────────────────────────────────────────────────────────────────
# רשימת קבוצות פייסבוק לסריקה
TARGET_URLS = [
    "https://www.facebook.com/groups/1870209196564360",
    "https://www.facebook.com/groups/1380680752778760",
    "https://www.facebook.com/groups/2098391913533248",
    "https://www.facebook.com/groups/402682483445663",
    "https://www.facebook.com/groups/1092766584127776",
    "https://www.facebook.com/groups/115046608513246",
    "https://www.facebook.com/groups/520940308003364",
]

# ─── מיקומים ───────────────────────────────────────────────────────────────────
# סינון ראשוני (מינימלי) לפני שליחה ל-LLM — חוסך קריאות מיותרות.
LOCATIONS = [
    "רמת גן", "רמת-גן", 'ר"ג', "ר״ג",
    "גבעתיים",
]

# ─── קריטריונים ──────────────────────────────────────────────────────────────────
TARGET_ROOMS = 3
MIN_PRICE = 5500
MAX_PRICE = 6500

# ─── מרחק ────────────────────────────────────────────────────────────────────────
# כתובת היעד לחישוב מרחק מכל דירה
DESTINATION_ADDRESS = "רחוב הדוגמה 1, תל אביב, ישראל"

# ─── גלילה ──────────────────────────────────────────────────────────────────────
SCROLL_COUNT = 10
SCROLL_DELAY_MS = 2500

# ─── שמירת סשן ──────────────────────────────────────────────────────────────────
SESSION_FILE = "fb_session.json"
