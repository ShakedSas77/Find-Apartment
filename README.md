# 🏠 בוט חיפוש דירות בפייסבוק

סורק **7 קבוצות פייסבוק**, משתמש ב-**Gemini LLM** לניתוח חכם של מודעות, מחשב **מרחק מרחוב הדוגמה 1 ת"א** דרך Google Maps, ומעדכן **Google Sheets** בזמן אמת.

## הקריטריונים הנוכחיים

| קריטריון | ערך |
|-----------|-----|
| **מיקום** | רמת גן / גבעתיים |
| **חדרים** | 3 |
| **מחיר** | ₪5,500 – ₪6,500 |
| **מרחק מ** | רחוב הדוגמה 1, תל אביב |

## עמודות בטבלה

| עמודה | מקור |
|-------|------|
| לינק למודעה | Playwright (שליפת URL) |
| מחיר | Gemini LLM |
| ארנונה | Gemini LLM |
| ועד | Gemini LLM |
| מקלט/ממד | Gemini LLM |
| האם זה מתיווך | Gemini LLM |
| כתובת | Gemini LLM |
| מרחק מרחוב הדוגמה 1 | Google Maps Distance Matrix |

## הכנות (חד-פעמיות)

### 1. התקנת ספריות

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Google Cloud – Service Account

1. היכנס ל-[Google Cloud Console](https://console.cloud.google.com/)
2. צור פרויקט חדש (או השתמש בקיים)
3. הפעל את **Google Sheets API** ו-**Distance Matrix API**
4. צור **Service Account** ← הורד את קובץ ה-JSON ← שמור כ-`credentials.json` בתיקייה
5. **שתף את ה-Google Sheet** עם כתובת האימייל של ה-Service Account (הרשאת עריכה)

### 3. Gemini API Key

1. היכנס ל-[Google AI Studio](https://aistudio.google.com/app/apikey)
2. צור מפתח API
3. הכנס אותו ב-`config.py` בשדה `GEMINI_API_KEY`

### 4. Google Maps API Key

1. ב-Google Cloud Console, הפעל את **Distance Matrix API**
2. צור מפתח API
3. הכנס אותו ב-`config.py` בשדה `GMAPS_API_KEY`

### 5. config.py

ערוך את [config.py](config.py) והכנס את שלושת המפתחות.

## הרצה

```bash
# הרצה ראשונה (דפדפן גלוי להתחברות)
python apartment_bot.py

# הרצות הבאות (רקע אוטומטי)
python apartment_bot.py --headless
```

## איך זה עובד

```
Playwright (סריקת פייסבוק)
    ↓ סינון ראשוני לפי מיקום
Gemini LLM (חילוץ נתונים מובנה)
    ↓ סינון לפי חדרים + מחיר
Google Maps (חישוב מרחק)
    ↓
Google Sheets (עדכון הטבלה)
```

**בכל הרצה הטבלה מתנקה ומתמלאת מחדש** — כך מודעות שנמחקו מפייסבוק פשוט לא יופיעו.

## טיפים

- **אל תקטין את ה-scroll delay** — פייסבוק חוסם חשבונות שגוללים מהר מדי.
- **הגדל `SCROLL_COUNT`** כדי לסרוק פוסטים ישנים יותר.
- **מחק `fb_session.json`** אם ההתחברות פגה.
- **הוסף קבוצות** — הוסף קישורים לרשימת `TARGET_URLS` ב-`config.py`.
