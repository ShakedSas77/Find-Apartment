# Apartment Bot — User Guide

The bot reads apartment-for-rent posts in Facebook groups, keeps the ones that match what you're
looking for, and shows them in its own app in your browser — a sortable list, a map, and the price,
rooms, walking distance to an address you choose and a fit score for each. Add-ons can send each new
apartment to your phone on Telegram, read Yad2's alert emails, save to a Google Sheet, and scan by
themselves every day.

It only ever **reads**: it never posts, comments, likes or messages anyone on Facebook.

**Contents**

- [Part 1 — Setup (required, ~10 minutes)](#part-1--setup-required-10-minutes)
  - [Before you start](#before-you-start)
  - [Step 1: Install Python](#step-1-install-python)
  - [Step 2: Download the bot](#step-2-download-the-bot)
  - [Step 3: Run the installer](#step-3-run-the-installer)
  - [Step 4: Where you walk to](#step-4-where-you-walk-to)
  - [Step 5: Budget and size](#step-5-budget-and-size)
  - [Step 6: Who reads the posts](#step-6-who-reads-the-posts)
  - [Step 7: Facebook](#step-7-facebook)
  - [Step 8: Your groups](#step-8-your-groups)
- [Part 2 — Using the app](#part-2--using-the-app)
- [Part 3 — Add-ons (optional, each on its own)](#part-3--add-ons-optional-each-on-its-own)
  - [A. Telegram alerts and chat commands](#a-telegram-alerts-and-chat-commands)
  - [B. Daily automatic scans](#b-daily-automatic-scans)
  - [C. Yad2 email alerts](#c-yad2-email-alerts)
  - [D. Google Sheet instead of Excel](#d-google-sheet-instead-of-excel)
  - [E. Google Maps walking routes](#e-google-maps-walking-routes)
- [Part 4 — Troubleshooting, questions, updating](#part-4--troubleshooting-questions-updating)

---

# Part 1 — Setup (required, ~10 minutes)

At the end, the app is open in your browser and the first scan is running.

## Before you start

| You need | Why |
|---|---|
| A **Windows** PC | the installer and launchers are Windows files (Mac/Linux: "Manual setup" in the README) |
| ~2 GB free disk space | the bot's own browser and libraries (~7 GB more for the offline AI) |
| A **Facebook account** that has **joined** the groups you want to follow | the bot reads groups as you, so it sees what you see |

Good to know:

- **No account or key is needed for the basic setup.** The AI is optional (step 6).
- **Area.** The bot is tuned for **Ramat Gan, Givatayim and Tel Aviv**. It works elsewhere, but posts
  that don't say their city may come out without a distance.
- **Facebook language must be English.** The bot reads post times like "3 hrs" and "Yesterday". If
  your Facebook is in Hebrew: Facebook → your picture → **Settings & privacy → Language** → English.
- **The app is for this PC only.** It runs on your computer and opens in your browser; nobody else can
  reach it. For your phone, use [Telegram](#a-telegram-alerts-and-chat-commands).

## Step 1: Install Python

Skip if Python 3.11 or newer is installed (the installer checks and tells you).

1. Open [python.org/downloads](https://www.python.org/downloads/) and click **Download Python**.
2. Run the downloaded file.
3. On the first screen, **tick "Add python.exe to PATH"** (at the bottom) — this matters.
4. Click **Install Now**, wait, then **Close**.

Alternative: open Command Prompt and run `winget install Python.Python.3.12`.

## Step 2: Download the bot

1. On the bot's GitHub page, click the green **Code** button → **Download ZIP**.
2. Right-click the ZIP → **Extract All** → choose a folder you'll keep, e.g. `Documents\apartment-bot`.

(With git: `git clone` the repository — updating later is one command.)

## Step 3: Run the installer

1. Open the bot's folder and double-click **`install.bat`**.
2. If Windows shows *"Windows protected your PC"*: **More info → Run anyway**. (It's a plain text
   script; right-click → Edit to read it.)
3. Wait while it creates a private Python environment in the folder (nothing is installed
   system-wide), installs the libraries (a few minutes the first time) and downloads the bot's browser.
4. **The app opens in your browser** on the **הגדרה ראשונה** (first-time setup) page. Keep the black
   installer window open — it's the app running; closing it closes the app.

The setup page has five sections. Each one saves on its own button, and a ✅ appears next to each
finished section. Once the address, the AI and at least one group are set, the app switches to its
normal menu.

## Step 4: Where you walk to

Walking distance is measured to one address: your work, a school, family.

1. Type it in **כתובת יעד** — street, number and city, e.g. `ביאליק 30, רמת גן`.
2. Click **חיפוש במפה**. A map shows where it was found.
3. If the pin is right, click **זו הכתובת — שמירה**. If not (or *"not found"*), write it differently —
   add the city, use the official street name — and search again.

## Step 5: Budget and size

Set the lowest and highest rent (₪ a month), the fewest and most rooms, and the longest walk (km),
then **שמירה**. Apartments outside these are skipped; you can change them any time.

## Step 6: Who reads the posts

Posts are free text; something has to pull out the price, rooms and address. Pick one:

- **בלי AI — no setup.** Simple rules find the price, rooms and a street the post names ("ברחוב
  ביאליק 30"). On real posts this found about 9 of every 10 apartments the AI found, but it misses
  entry dates, floors and other details. Good to start with; switch later in Settings. Click **שמירה**.
- **Gemini — free key from Google (recommended for full details).**
  1. Click the **Google AI Studio** link and sign in with a Google account.
  2. Click **Create API key** (pick or create a project if asked) and copy the key (it starts with `AIza`).
  3. Paste it into **מפתח Gemini** and click **בדיקה ושמירה**. *"The key doesn't work"* means part of
     it was missed — copy it again.

  The free key has a daily limit; on busy days the bot switches between several free Google models.
- **Ollama — AI on this computer, no account.** Needs ~5 GB and a reasonably strong PC; slower.
  1. If it isn't installed, click the download link, install it, and refresh the page.
  2. Click **הורדת המודל ושמירה** — it downloads the AI model once (several GB; wait for it).

## Step 7: Facebook

- **עם חשבון הפייסבוק שלי (recommended).** Click **התחברות לפייסבוק**. A **separate browser window**
  opens — the bot's own, not your usual browser. Log in to Facebook there (including any two-step
  code). When your feed shows, the window **closes by itself** and the page says *מחובר*. The login
  is remembered; repeat this only if Facebook ever logs the bot out.
- **בלי התחברות.** The bot visits groups as a logged-out visitor. Facebook usually shows visitors a
  "log in to see this" screen; the bot respects it, never closes it, and marks that group
  **"פייסבוק דורש התחברות"**. Expect few or no apartments.

## Step 8: Your groups

- **Pick from your groups (after logging in):** click **הצגת הקבוצות שלי מפייסבוק**. After about a
  minute you get the groups your account has joined, with the ones that sound like apartment groups
  already ticked. Untick any you don't want and click **הוספת המסומנות**.
- **Or paste links:** open a group on Facebook, copy the address bar (`facebook.com/groups/...`), paste
  it into the box (one per line) and click **הוספת הקישורים**.

When this is done the app opens its normal menu. Click **▶️ סריקה מלאה** in the side panel to run the
first scan — it reads two weeks back in each group, so it takes longer than later ones.

✅ Setup done. (Prefer a console? `venv\Scripts\python setup_wizard.py` asks the same questions in text.)

---

# Part 2 — Using the app

**Open it** any time by double-clicking **`open_app.bat`** in the bot's folder. It opens in your browser;
keep its black window open while you use it.

## The side panel: scans

On every page:

| Control | What it does |
|---|---|
| ▶️ סריקה מלאה | scan all active groups (and Yad2 emails if turned on) |
| 📘 רק פייסבוק / 📧 רק יד2 | only the Facebook groups / only the Yad2 emails (when that add-on is on) |
| קבוצה אחת | pick a group and scan just it |
| ⏹ עצירה | stop the running scan (also one started by the daily schedule or from Telegram) |
| progress line | groups done, apartments added, time left — updates by itself |
| השהיית הסריקות האוטומטיות | pause the daily scans (e.g. when you're away or found a place); scans from here still work |

Each scan only reads posts newer than the previous scan, so frequent scans are quick.

## Results (דירות)

Every apartment found, newest post first by default.

- **Filters** at the top: highest price, fewest rooms, longest walk, source (Facebook/Yad2), a word in
  the address, and your tracking status. **הסתרת הלא רלוונטיות** hides the ones you marked not relevant.
- **Sort** by fit score, post date, price or distance.
- **Track each apartment** right in the table: the **מעקב** column — ⭐ מעניינת (interested),
  📞 פניתי (contacted), 👀 ראיתי (visited), ❌ לא רלוונטית (not relevant) — and a free **הערה** (note).
  Changes save by themselves.
- **פתיחה** opens the post; **🗺** opens the address in Google Maps.
- **🔄 רענון** reloads after a scan; **📂 פתיחת הקובץ ב-Excel** opens the results file.

| Column | Meaning |
|---|---|
| ציון | **fit score 0-100** — weighs price, distance, how soon you can move in, floor/elevator/safe room, rooms and parking; for deciding what to look at first, not a filter |
| מחיר / חדרים | monthly rent / rooms |
| מרחק הליכה, דיוק מרחק | walking km to your destination; `Google` = a real route, `הערכה ±0.4` = a free estimate, give or take 0.4 km |
| עלות כוללת | rent + municipal tax + building fee per month (+ an agent fee spread over 2 years) |
| תאריך כניסה, קומה, מעלית, חניה, ממ"ד/מקלט, תיווך/פרטי | as the post says; blank when it doesn't |
| תאריך פרסום, מקור | when it was posted, and where (Facebook or Yad2) |

The same apartment posted in several groups appears once; apartments older than 14 days drop off.

## Map (מפה)

Every apartment the bot could locate, as a dot coloured by fit score (red = low, green = high), around
a **gold dot** for your destination and a circle showing roughly how far you said you'd walk.

- Hover a dot for its price, rooms, score and walk; **click it** for a card with its tracking status
  and buttons to open the post and Google Maps.
- Filters as on Results, plus **minimum fit score** and **posted in the last X days**.
- The line under the filters says how many apartments couldn't be placed (no address, or one the map
  couldn't find) — they're still on the Results page.

## Groups (קבוצות)

The groups the bot scans, with how many posts it read and apartments it found in each.

- Untick **פעילה** to pause a group, then **שמירת השינויים**.
- **הסרת קבוצה** removes one; **הוספת קבוצות** adds more (from your Facebook groups, or by link).
- A group with hundreds of posts and no matches is worth pausing — it only slows scans down.

| Status | Meaning / what to do |
|---|---|
| תקין | all good |
| לא חבר/ה בקבוצה | the account hasn't joined — join on Facebook, or pause the group |
| בקשת הצטרפות נשלחה | waiting for the group's admins to approve you |
| לא נמצאו פוסטים | check the link opens the group; it may be empty |
| פייסבוק דורש התחברות | you're in no-login mode — log in (Settings → פייסבוק) |
| ממתינה לסריקה ראשונה | added but not scanned yet |

## Settings (הגדרות)

The setup sections again — destination, budget, AI, Facebook — plus where results are saved and every
add-on (Part 3). Changes apply from the next scan. While a scan runs, settings and groups can't be
changed (the scan rewrites the results file when it ends); the page says so.

## Stats (סטטיסטיקה)

Totals, apartments added per day (30 days), the last scans, each group's match rate, and a
**בדיקת תקינות** (health check) button: one line per part of the bot — ✅ OK, ⚠️ warning,
❌ problem (with how to fix it), ⚪ off (an add-on you didn't turn on — fine).

## Tools (כלים)

- **יומני סריקה** — pick a scan and read its log, or only its errors and warnings.
- **תחזוקה** — one click each: back up the database, clean out old apartments, re-check posts that were
  rejected (useful after you widen your budget), recompute fit scores, sync and sort the groups.

---

# Part 3 — Add-ons (optional, each on its own)

Each add-on is independent: add any, in any order, any time, from **Settings (הגדרות) → תוספים**. Each
section explains its steps, checks what you enter, and has a button to turn it off again.

| Add-on | Time | Needs |
|---|---|---|
| [A. Telegram](#a-telegram-alerts-and-chat-commands) | 5 min | the Telegram app |
| [B. Daily scans](#b-daily-automatic-scans) | 1 min | — |
| [C. Yad2 emails](#c-yad2-email-alerts) | 10 min | Gmail with 2-step verification |
| [D. Google Sheet](#d-google-sheet-instead-of-excel) | 15 min | a Google account |
| [E. Google Maps](#e-google-maps-walking-routes) | 15 min | a Google account + a payment card |

(Each can also be set up in a console: `venv\Scripts\python setup_wizard.py --add telegram`, `schedule`,
`yad2`, `results`, `maps`.)

---

## A. Telegram alerts and chat commands

**What you get:** a Telegram message for every new apartment (price, rooms, distance, total cost,
buttons to open the post, the address in Maps and WhatsApp to the poster), a start/finish message for
each scan with a live progress bar, and commands to run the bot from your phone.

### Set it up — Settings → 📱 טלגרם

1. **Create your bot** in Telegram:
   1. Open **@BotFather** (the link on the page), tap **Start**, send `/newbot`.
   2. Send a display name, e.g. `Apartment finder`, then a username ending in `bot`, e.g.
      `dana_apartments_bot`.
   3. BotFather replies with a **token** like `7123456789:AAH...`. Copy it.
2. Paste it into **טוקן הבוט** and click **בדיקת הטוקן**.
3. **Choose the chat** that gets the alerts:
   - **Just you:** open your bot (the link the page shows) and send it `/start`.
   - **A group with a partner/roommates:** add the bot to a Telegram group and send `/start` there.
4. Click **חיבור לצ'אט**. The page says which chat it found, and **"✅ הבוט מחובר"** arrives there.
5. Click **הפעלת פקודות מהצ'אט** so the bot answers chat commands whenever the PC is on (it adds a
   small background program that starts with Windows).

### Use it

In the chat, send `/help` for a menu of buttons:

| Command | What it does |
|---|---|
| /scan, /scan_fb, /scan_yad2, /scan_group | full scan / Facebook only / Yad2 only / one group from a list |
| /stop | stop the running scan |
| /status | is a scan running, how far along |
| /new | apartments added in the last 24 hours |
| /groups | groups that need attention |
| /pause, /resume | pause / resume the daily scans |
| /stats, /doctor | totals and the last scans / health check |

### Turn it off

Settings → 📱 טלגרם → **כיבוי טלגרם**. To stop the background program starting with Windows:
Win+R → `shell:startup` → delete *"Apartment bot Telegram listener"*.

---

## B. Daily automatic scans

**What you get:** scans that run by themselves at fixed times, with no window. The PC must be on and
awake at that time (a missed time is skipped); for the logged-in mode, the Facebook login from step 7.

### Set it up — Settings → 🕒 סריקות אוטומטיות

Type a time like `20:00` and click **הוספה**. Repeat for each time. The page lists each scheduled scan
with its next run.

### Use it

Pause them all for a while with **השהיית הסריקות האוטומטיות** in the side panel (or `/pause` in
Telegram). See each scan in **Tools → יומני סריקה**.

### Turn it off

**מחיקה** next to each time.

---

## C. Yad2 email alerts

**What you get:** Yad2 listings too — Yad2 emails a daily digest of new listings for a saved search, and
the bot reads those emails into its results (marked יד2), with the same filters, distance and score.
**You need:** a Gmail account with **2-step verification** turned on (needed for an app password).

### Set it up — Settings → 📧 מיילים של יד2

1. **Save a search on Yad2 with email alerts:** on [yad2.co.il](https://www.yad2.co.il), sign in, search
   apartments for rent with your filters, click **שמירת חיפוש** and turn on **email** alerts to your Gmail.
2. **Put the Yad2 emails under a Gmail label:**
   1. In Gmail, click the search options icon (the sliders at the right of the search box).
   2. **From:** `alerts@yad2.co.il` → **Create filter**.
   3. Tick **Apply the label** → **New label** → name it `yad2` → **Create filter**.
3. **Create a Gmail app password** (a separate password just for the bot; your real one is never used):
   1. Check 2-step verification is on at [myaccount.google.com/security](https://myaccount.google.com/security).
   2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), name it
      `apartment bot` → **Create**, and copy the 16-letter password.
4. In the section, fill in your Gmail address, the label (`yad2`) and the app password, then
   **בדיקה והפעלה**. If it can't read the mailbox it says why (a wrong app password, or no such label).

The mailbox is opened **read-only** — the bot never marks, moves or deletes email. Each scan reads the
last 2 days of emails.

### Turn it off

**כיבוי מיילי יד2**, and revoke the app password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

---

## D. Google Sheet instead of Excel

**What you get:** results in a Google Sheet — open on your phone, share with a partner. Same tabs and
columns as the Excel file. **You need:** a Google account (Google Cloud is free for this).

### Set it up — Settings → 📂 איפה התוצאות נשמרות

1. **Create a Google Cloud project with the Sheets API:**
   1. Open [console.cloud.google.com](https://console.cloud.google.com/) and sign in (accept the terms).
   2. Top bar → project picker → **New project** → `apartment-bot` → **Create** → select it.
   3. Open the [Google Sheets API page](https://console.cloud.google.com/apis/library/sheets.googleapis.com) → **Enable**.
2. **Create a service account** (a robot Google user the bot writes as): left menu → **IAM & Admin →
   Service Accounts → Create service account** → name `apartment-bot` → **Create and continue** →
   skip the roles → **Done**.
3. **Download its key:** click the service account → **Keys** → **Add key → Create new key → JSON →
   Create**. A `.json` file downloads; keep it private.
4. In the section, **upload that JSON file**. The page shows an address like
   `apartment-bot@apartment-bot-1234.iam.gserviceaccount.com`.
5. Create a blank sheet at [sheets.new](https://sheets.new), click **Share**, paste that address, choose
   **Editor**, untick "Notify" → **Share**.
6. Paste the sheet's link into the section and click **חיבור**. *"Saved, but the sheet didn't open
   yet"* almost always means it isn't shared with that address as Editor.

Your groups carry over by themselves on the first scan.

### Turn it off

**חזרה לקובץ Excel** in the same section.

---

## E. Google Maps walking routes

**What you get:** the real walking distance by Google Maps (`Google` in the דיוק מרחק column) instead
of the free estimate (`הערכה ±0.4`). Optional — the estimate is good enough for most. **You need:** a
Google account and a payment card. Google gives a monthly free credit; the bot stops itself at 9,000
lookups a month, inside it.

### Set it up — Settings → 🗺 מסלולי הליכה של Google Maps

1. In [console.cloud.google.com](https://console.cloud.google.com/), select (or create) a project.
2. **Billing** → link a billing account (add the card).
3. **APIs & Services → Library**: enable **Distance Matrix API** and **Geocoding API**.
4. **APIs & Services → Credentials → Create credentials → API key**; copy it.
5. Recommended: click the key → **Restrict key** to those two APIs → **Save**; and in **Distance
   Matrix API → Quotas**, lower *Elements per day* to about 300.
6. Paste the key into the section → **שמירה והפעלה**.

### Turn it off

**כיבוי Google Maps**; delete the key in Google Cloud if you won't use it again.

---

# Part 4 — Troubleshooting, questions, updating

## Troubleshooting

Start with **Stats → בדיקת תקינות** (or `venv\Scripts\python doctor.py`), and **Tools → יומני סריקה**
for the last scan's errors.

| Problem | Fix |
|---|---|
| `install.bat`: Python is missing | install Python with "Add python.exe to PATH" ticked (step 1), run `install.bat` again |
| `install.bat`: install failed | check the internet connection, run `install.bat` again |
| The app page doesn't open / says "connection error" | double-click `open_app.bat` and keep its window open; if a browser tab didn't open, go to the address it prints (`http://127.0.0.1:8501`) |
| "listings.xlsx is open in another program" | close it in Excel; the waiting rows save on the next write |
| A scan ends with "צריך להתחבר" (login required) | Settings → פייסבוק → **התחברות לפייסבוק** |
| Facebook asks for a security check (CAPTCHA) | run `run_bot.bat --headful` and solve it in the window that opens |
| Groups show "לא חבר/ה בקבוצה" | join them on Facebook with the account the bot uses |
| No apartments at all | widen the budget (Settings), check the group statuses (Groups), then Tools → **בדיקה חוזרת של פוסטים שנדחו** |
| Dates missing / "unrecognized Facebook post-date format" in a log | switch Facebook to English (Before you start) |
| Telegram: no messages | Settings → טלגרם shows whether it's on; make sure the bot is still in the chat |

## Questions

**Can I use it from my phone?** Not the app — it stays on your PC so no one else can reach it. Use
Telegram (add-on A): alerts, scans and status from the chat, or a Google Sheet (add-on D) to browse.

**Is my Facebook account at risk?** The bot reads slowly, like a person scrolling, and never posts.
Facebook may still ask for a security check now and then. Fewer groups and fewer daily scans lower the chance.

**What's stored, and where?** Everything stays in the bot's folder: results, a database of posts already
seen (so a post is never read twice) with your tracking marks, logs, and the bot browser's Facebook
login. Post text goes only to the AI you chose (Google's Gemini, nowhere with no-AI or Ollama), and
results only to your Google Sheet / Telegram chat if you added those.

**Does it cost anything?** No. Google Maps is the only add-on asking for a card, and the bot caps
itself inside Google's free credit.

**Can I add my own features?** Yes — see [extending.md](extending.md).

## Updating

Download the new version (or `git pull`) into the same folder, then run `install.bat` again. Your
settings and data stay: `config_local.py`, `.env`, `credentials.json`, `listings.xlsx`, `bot_data.db`
and `chrome_profile` aren't part of the download — when unzipping over the folder, don't delete them.

## Uninstalling

1. Settings → סריקות אוטומטיות → delete each time.
2. Telegram commands: Win+R → `shell:startup` → delete *"Apartment bot Telegram listener"*.
3. Delete the bot's folder.
