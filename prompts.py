def get_apartment_prompt_improved(text: str) -> str:
    """
    English instructions; field-rule content and all output values stay Hebrew
    (the source posts are Hebrew and the sheet is Hebrew).
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
- "entry_date" (String): move-in date. If an exact date is stated, return it in "D.M" format (e.g. "1.8"). If only a month is stated, return the Hebrew month name, optionally with "תחילת"/"אמצע"/"סוף" (beginning/middle/end) — e.g. "ספטמבר", "תחילת אוקטובר". If the text says immediate/flexible/ASAP, return "מיידי". On a date range, return the earliest date in the range. Strip background noise and emoji.
- "floor" (String): floor number, e.g. "2", "קרקע" (ground floor), "3 מתוך 4".
- "elevator" (Boolean): is there an elevator? (true = yes, false = no, null = not mentioned).
- "is_agent" (Boolean): is this listing from a real-estate agent/agency? (true = agent, false = private/no agent, null = not mentioned).
- "address" (String): the apartment's location, **in Hebrew only**, exactly as written in the source text — never a fabricated address.
  - If an explicit street name is given: return it plus the house number if present (e.g. "רחוב טור הברושים 5"). Strip descriptive add-ons that are not part of the street name itself (e.g. "השקט", "המבוקש", "הנחשק", "היוקרתי") — keep only the raw street name. Do not interpret Hebrew slang like "שירוקלחת" (bathroom+shower) as a street name.
  - If there's no explicit street name but the text mentions an explicit nearby landmark or neighborhood (e.g. "ליד הפארק הלאומי", "בשכונת שנקין") — return that exact phrase as it appears in the text, unchanged, not shortened, not expanded.
  - **Absolute requirement: the string must contain Hebrew letters only — Latin letters (A-Z/a-z) are strictly forbidden, even as a transliteration before or after the Hebrew.** Do not translate, transliterate, or add an English version "for clarity". Wrong example: "etur habrisot רחוב טור הברושים" (forbidden). Correct example: "רחוב טור הברושים 5".
  - **Never invent an address, street, neighborhood, or landmark that isn't explicitly written in the source text — not even as a reasonable guess.** If no real location info appears in the text, return null.
  - If no city is mentioned at all, assume "רמת גן / גבעתיים" only — without inventing a street or landmark. Exception: these neighborhoods belong to Tel Aviv, not Ramat Gan / Givatayim: תל חיים, ביצרון, רמת ישראל, שיכון חברת החשמל, שיכון עממי ג', יד אליהו, נחלת יצחק, רמת הטייסים.

Text to parse:
---
{text[:3000]}
---
"""
