from config import PROMPT_LANGUAGE


def get_apartment_prompt_hebrew(text: str) -> str:
    """
    מייצר פרומפט אופטימלי לשליחה למודל AI לחילוץ נתונים מפוסט נדל"ן.
    """
    return f"""אתה מומחה לחילוץ נתונים מתוך טקסט (Data Extraction).
המטרה שלך היא לקרוא את מודעת הנדל"ן הבאה ולהמיר את המידע שבה למבנה נתונים מסוג JSON.

חוקי ברזל:
1. החזר *אך ורק* אובייקט JSON חוקי וטהור. ללא טקסט מקדים, ללא סיכום, וללא עטיפת Markdown (אל תשתמש ב-```json).
2. אם נתון חסר או לא ידוע, החזר null (לכל סוגי הנתונים - מספרים, מחרוזות ובוליאנים). אל תחזיר טקסט כמו "לא צוין".
3. היצמד בקפידה לסוגי הנתונים המוגדרים מטה.

המפתחות הנדרשים וסוגי הנתונים:
- "rooms" (Number): מספר החדרים כעשרוני (למשל 3.0, 3.5). אם כתוב "סטודיו", החזר 1.0.
- "price" (Number): שכר דירה חודשי בשקלים. חלץ רק את המספר (למשל 5500). אם כתוב ב-K (למשל 5.5K), המר למספר המלא (5500).
- "arnona" (String): עלות ארנונה. ציין תמיד את יחידת הזמן כפי שהיא כתובה בטקסט המקורי — "לחודש" או "לחודשיים" (למשל "300 לחודשיים", "150 לחודש"). אם לא צוינה יחידת זמן בטקסט, כתוב את הסכום בלבד ללא יחידה (למשל "800"). אל תמציא יחידת זמן שלא מופיעה במקור.
- "vaad" (String): עלות ועד בית. אותו כלל יחידת זמן כמו ארנונה (למשל "200 לחודש", "400 לחודשיים", "כלול במחיר").
- "shelter" (Boolean): האם יש ממ"ד או מקלט? (true = יש, false = אין, null = לא צוין).
- "parking" (String): פרטי חניה (למשל "חניה בטאבו", "ברחוב", "אין חניה").
- "entry_date" (String): תאריך כניסה. למשל "1.8", "ספטמבר", "מיידי". סנן רעשי רקע וסמיילים.
- "floor" (String): קומה. למשל "2", "קרקע", "3 מתוך 4".
- "elevator" (Boolean): האם יש מעלית? (true = יש, false = אין, null = לא צוין).
- "is_agent" (Boolean): האם המודעה מתיווך? (true = תיווך, false = ללא תיווך, null = לא צוין).
- "address" (String): מיקום הדירה, בעברית בלבד, בדיוק כפי שכתוב בטקסט המקורי — לא כתובת מומצאת.
  - אם יש שם רחוב מפורש: החזר אותו + מספר בית אם קיים (למשל "רחוב טור הברושים 5"). הסר לחלוטין תארים ותוספות תיאוריות שאינן חלק משם הרחוב עצמו (למשל "השקט", "המבוקש", "הנחשק", "היוקרתי") — השאר רק את שם הרחוב הגולמי. אל תפרש צירופי סלנג כמו "שירוקלחת" (=שירותים+מקלחת) כשם רחוב.
  - אם אין שם רחוב מפורש אך הטקסט כן מזכיר קרבה מפורשת לציון דרך/שכונה (למשל "ליד הפארק הלאומי", "בשכונת שנקין") — החזר את הביטוי הזה בדיוק כפי שהוא מופיע בטקסט, בלי לשנות, לקצר או להוסיף עליו.
  - **חובה מוחלטת: המחרוזת חייבת להכיל אך ורק אותיות עבריות — אסור בהחלט שיופיעו בה אותיות לטיניות (A-Z/a-z), אפילו לא כתעתיק לפני או אחרי הכתיב העברי.** אל תתרגם, אל תתעתק, ואל תוסיף גרסה אנגלית "בשביל בהירות". דוגמה שגויה: "etur habrisot רחוב טור הברושים" (אסור). דוגמה נכונה: "רחוב טור הברושים 5".
  - **לעולם אל תמציא כתובת, רחוב, שכונה, או ציון דרך שאינו כתוב במפורש בטקסט המקורי — גם לא כניחוש סביר.** אם שום מידע מיקום אמיתי אינו מופיע בטקסט, החזר null.
  - אם עיר לא מוזכרת כלל בטקסט, הנח שהיא "רמת גן / גבעתיים" בלבד — בלי להמציא רחוב או ציון דרך.

הטקסט לפענוח:
---
{text[:3000]}
---
"""


def get_apartment_prompt_english(text: str) -> str:
    """
    Same extraction rules as get_apartment_prompt_hebrew(), but with English
    instructions — field-rule content and all output values stay Hebrew
    (the source posts are Hebrew and the sheet is Hebrew). Same JSON keys.
    """
    return f"""You are a data extraction expert. Read the following Hebrew real-estate rental listing and convert it into a single JSON object.

Hard rules:
1. Return *only* a valid, pure JSON object. No preamble, no summary, no Markdown fences (do not use ```json).
2. If a field is missing or unknown, return null (for every type — numbers, strings, booleans). Never return placeholder text like "not specified".
3. Follow the field types below exactly.

Required keys and types:
- "rooms" (Number): room count as a decimal (e.g. 3.0, 3.5). If the text says "סטודיו" (studio), return 1.0.
- "price" (Number): monthly rent in NIS. Extract only the number (e.g. 5500). If written in K notation (e.g. "5.5K"), convert to the full number (5500).
- "arnona" (String): municipal tax amount. Always include the billing period exactly as written in the original Hebrew text — "לחודש" (per month) or "לחודשיים" (per two months), e.g. "300 לחודשיים", "150 לחודש". If no period is stated, write the amount alone with no unit (e.g. "800"). Never invent a billing period that isn't in the source text.
- "vaad" (String): building committee fee. Same billing-period rule as arnona, e.g. "200 לחודש", "400 לחודשיים", "כלול במחיר" (included in rent).
- "shelter" (Boolean): is there a ממ"ד/מקלט (safe room/shelter)? (true = yes, false = no, null = not mentioned).
- "parking" (String): parking details, e.g. "חניה בטאבו", "ברחוב", "אין חניה".
- "entry_date" (String): move-in date, e.g. "1.8", "ספטמבר", "מיידי" (immediate). Strip background noise and emoji.
- "floor" (String): floor number, e.g. "2", "קרקע" (ground floor), "3 מתוך 4".
- "elevator" (Boolean): is there an elevator? (true = yes, false = no, null = not mentioned).
- "is_agent" (Boolean): is this listing from a real-estate agent/agency? (true = agent, false = private/no agent, null = not mentioned).
- "address" (String): the apartment's location, **in Hebrew only**, exactly as written in the source text — never a fabricated address.
  - If an explicit street name is given: return it plus the house number if present (e.g. "רחוב טור הברושים 5"). Strip descriptive add-ons that are not part of the street name itself (e.g. "השקט", "המבוקש", "הנחשק", "היוקרתי") — keep only the raw street name. Do not interpret Hebrew slang like "שירוקלחת" (bathroom+shower) as a street name.
  - If there's no explicit street name but the text mentions an explicit nearby landmark or neighborhood (e.g. "ליד הפארק הלאומי", "בשכונת שנקין") — return that exact phrase as it appears in the text, unchanged, not shortened, not expanded.
  - **Absolute requirement: the string must contain Hebrew letters only — Latin letters (A-Z/a-z) are strictly forbidden, even as a transliteration before or after the Hebrew.** Do not translate, transliterate, or add an English version "for clarity". Wrong example: "etur habrisot רחוב טור הברושים" (forbidden). Correct example: "רחוב טור הברושים 5".
  - **Never invent an address, street, neighborhood, or landmark that isn't explicitly written in the source text — not even as a reasonable guess.** If no real location info appears in the text, return null.
  - If no city is mentioned at all, assume "רמת גן / גבעתיים" only — without inventing a street or landmark.

Text to parse:
---
{text[:3000]}
---
"""


def get_apartment_prompt_improved(text: str) -> str:
    """Dispatches to the Hebrew or English prompt variant per config.PROMPT_LANGUAGE (for A/B testing)."""
    if PROMPT_LANGUAGE == "hebrew":
        return get_apartment_prompt_hebrew(text)
    return get_apartment_prompt_english(text)
