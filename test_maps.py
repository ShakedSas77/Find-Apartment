import os
import googlemaps
from dotenv import load_dotenv

# טעינת המפתח מתוך קובץ ה-.env
load_dotenv()
GMAPS_API_KEY = os.getenv("GMAPS_API_KEY")

print("🔍 בודק חיבור לגוגל מפות...")

try:
    gmaps_client = googlemaps.Client(key=GMAPS_API_KEY)
    result = gmaps_client.distance_matrix(
        origins="רחוב הדוגמה 2, גבעתיים, ישראל",
        destinations="רחוב הדוגמה 1, תל אביב, ישראל",
        mode="walking"
    )
    
    status = result['rows'][0]['elements'][0]['status']
    if status == "OK":
        distance = result['rows'][0]['elements'][0]['distance']['text']
        duration = result['rows'][0]['elements'][0]['duration']['text']
        print(f"✅ הצלחה! גוגל מפות עובד.")
        print(f"מרחק: {distance}, זמן הליכה: {duration}")
    else:
        print(f"⚠️ גוגל החזיר סטטוס שגיאה פנימי: {status}")
        print("תשובה מלאה:", result)

except Exception as e:
    print("❌ שגיאת API:")
    print(e)