# Apartment Bot — User Guide

The bot reads apartment-for-rent posts in Facebook groups, keeps the ones that match what you're
looking for, and saves them to an Excel file with the price, rooms, walking distance to an address
you choose, and a fit score. Add-ons can send each new apartment to your phone on Telegram, read
Yad2's alert emails, save to a Google Sheet, and scan by themselves every day.

It only ever **reads**: it never posts, comments, likes or messages anyone on Facebook.

**Contents**

- [Part 1 — Basic setup (required)](#part-1--basic-setup-required)
  - [Before you start](#before-you-start)
  - [Step 1: Install Python](#step-1-install-python)
  - [Step 2: Download the bot](#step-2-download-the-bot)
  - [Step 3: Run the installer](#step-3-run-the-installer)
  - [Step 4: What you're looking for](#step-4-what-youre-looking-for)
  - [Step 5: The AI that reads posts](#step-5-the-ai-that-reads-posts)
  - [Step 6: Where results go](#step-6-where-results-go)
  - [Step 7: Facebook login and groups](#step-7-facebook-login-and-groups)
  - [Step 8: Health check and first scan](#step-8-health-check-and-first-scan)
- [Part 2 — Using the bot](#part-2--using-the-bot)
- [Part 3 — Add-ons (optional, each on its own)](#part-3--add-ons-optional-each-on-its-own)
  - [A. Telegram alerts and chat commands](#a-telegram-alerts-and-chat-commands)
  - [B. Daily automatic scans](#b-daily-automatic-scans)
  - [C. Yad2 email alerts](#c-yad2-email-alerts)
  - [D. Google Sheet instead of Excel](#d-google-sheet-instead-of-excel)
  - [E. Google Maps walking routes](#e-google-maps-walking-routes)
- [Part 4 — Changing settings, troubleshooting, questions](#part-4--changing-settings-troubleshooting-questions)

---

# Part 1 — Basic setup (required)

About **15 minutes**. At the end, scans save apartments to `listings.xlsx` in the bot's folder.

## Before you start

You need:

| | Why |
|---|---|
| A **Windows** PC | the installer and launchers are Windows files (Mac/Linux: see "Manual setup" in the README) |
| ~2 GB free disk space | the bot's own browser and libraries (~7 GB more if you pick the offline AI) |
| A **Facebook account** that has **joined** the groups you want to follow | the bot reads groups as you, so it sees what you see |
| A **Google account** (Gmail) | for the free AI key — or install Ollama instead (step 5) |

Two things to know:

- **Area.** The bot is tuned for **Ramat Gan, Givatayim and Tel Aviv**. It works elsewhere, but posts
  that don't say their city may come out without a distance.
- **Facebook language must be English.** The bot reads post times like "3 hrs" and "Yesterday". If
  your Facebook is in Hebrew: Facebook → your picture → **Settings & privacy → Language** → English.

## Step 1: Install Python

Skip if Python 3.11 or newer is already installed (the installer in step 3 checks and tells you).

1. Open [python.org/downloads](https://www.python.org/downloads/) and click **Download Python**.
2. Run the downloaded file.
3. On the first screen, **tick "Add python.exe to PATH"** (at the bottom) — this matters.
4. Click **Install Now**, wait, then **Close**.

Alternative: open Command Prompt and run `winget install Python.Python.3.12`.

## Step 2: Download the bot

1. On the bot's GitHub page, click the green **Code** button → **Download ZIP**.
2. Right-click the ZIP → **Extract All** → choose a folder you'll keep, e.g. `Documents\apartment-bot`.

If you use git, `git clone` the repository instead — updating later is one command.

## Step 3: Run the installer

1. Open the bot's folder and double-click **`install.bat`**.
2. If Windows shows *"Windows protected your PC"*: click **More info → Run anyway**. (It's a plain
   text script; right-click → Edit to read it.)
3. Wait while it:
   - creates a private Python environment in the folder (`venv` — nothing is installed system-wide),
   - installs the libraries (a few minutes the first time),
   - downloads the bot's own browser (~150 MB).
4. The **setup wizard** starts by itself in the same window.

**How the wizard works:** each question shows the current answer in `[brackets]`; press **Enter** to
keep it. Secret keys stay hidden while you paste them (paste with right-click or Ctrl+V, then Enter —
nothing appears, that's normal). **Ctrl+C** stops; answers so far are saved. Run it again any time
with `venv\Scripts\python setup_wizard.py`.

## Step 4: What you're looking for

| Question | Example | Meaning |
|---|---|---|
| Fewest rooms | `2.5` | apartments with fewer rooms are skipped |
| Most rooms | `3.5` | ...and with more |
| Lowest rent | `5500` | ₪ a month |
| Highest rent | `6700` | ₪ a month |
| Longest walk to your destination | `5` | km; apartments farther away are skipped |
| Destination address | `ביאליק 30, רמת גן` | the place you'll walk to: work, a school, family. Street, number and city |

After the address, the wizard looks it up on the map and prints a link. **Open the link** and check the
pin is in the right place, then answer `y`. If it isn't found, write it differently (add the city, use
the official street name) — the wizard asks again until it finds it.

## Step 5: The AI that reads posts

Posts are free text; an AI reads each one and pulls out price, rooms, address, entry date and so on.
Pick one:

### Option 1 — Gemini (recommended)

Google's AI, free with a personal key. Fast and accurate.

1. Type `1` and Enter. The wizard opens **Google AI Studio** in your browser.
2. Sign in with your Google account and accept the terms if asked.
3. Click **Create API key** (if asked for a project, pick the suggested one or "Create a new project").
4. Click the copy icon next to the key (a long string starting with `AIza`).
5. Back in the wizard window, paste it and press Enter. It stays hidden — that's normal.
6. The wizard tests the key. *"That key didn't work"* means part of it was missed — copy it again.

The free key has a daily limit; on very busy days the bot switches between several free Google models
by itself. If you also install Ollama (option 2), it takes over when all of them are used up.

### Option 2 — Ollama (offline, no account)

Runs the AI on your own computer. Needs ~5 GB of disk space and a reasonably strong PC (8 GB+ RAM;
a graphics card helps). Slower, slightly less accurate.

1. Type `2` and Enter.
2. If Ollama isn't installed, the wizard opens [ollama.com/download/windows](https://ollama.com/download/windows):
   download, run the installer, then press Enter in the wizard.
3. The wizard downloads the AI model (several GB, once). Wait until it finishes.

Ollama has to be installed, but needn't be open: a scan starts it by itself when it isn't running.

## Step 6: Where results go

1. Press Enter for **1 — Excel file** (recommended for a start): results go to `listings.xlsx` in the
   bot's folder. Nothing to set up.
2. To use a Google Sheet instead, see [add-on D](#d-google-sheet-instead-of-excel) — you can switch any time.

## Step 7: Facebook login and groups

### Choose how the bot uses Facebook

**1 — Log in (recommended).**

1. Type `1` (or Enter) and answer `y` to *"Open the browser to log in now?"*.
2. A **separate browser window** opens — the bot's own, not your usual browser.
3. Log in to Facebook there as usual (including any two-step code).
4. Wait until you see your Facebook feed.
5. Go back to the wizard window and press **Enter**.

The login is remembered in the bot's folder (`chrome_profile`), so scans run without asking again.
If Facebook ever logs the bot out, repeat with `venv\Scripts\python setup_wizard.py --add facebook`.

**2 — No login.** The bot visits groups as a logged-out visitor. Facebook usually shows visitors a
"log in to see this" screen; the bot respects it, never closes it, and marks that group
**"פייסבוק דורש התחברות"**. Expect few or no apartments. You can switch to login later the same way.

### Add your groups

1. On Facebook, open an apartment group you've joined.
2. Copy the link from the browser's address bar — it looks like
   `https://www.facebook.com/groups/1234567890` (any extra text after it is fine).
3. Paste it into the wizard and press Enter. Repeat for each group.
4. Press Enter on an empty line when you're done.

The groups go into the **Groups** tab of your results file; manage them there later
([Part 2](#managing-your-groups)).

## Step 8: Health check and first scan

1. The wizard offers the add-ons (Part 3). Answer `n` to each for now — add any of them later.
2. It runs the **health check**: every line should be **PASS** or **OFF** (OFF = a feature you didn't
   turn on — fine). A **FAIL** line says how to fix it.
3. It asks *"Run the first scan now?"* — answer `y`. The first scan reads two weeks back in each group,
   so it takes longer than later ones.
4. Open **`listings.xlsx`** in the bot's folder.

✅ Basic setup done.

---

# Part 2 — Using the bot

## Running a scan

- **Double-click `run_bot.bat`.** The window shows progress (`[####----] 3/8 groups | 2 added`); when
  it says *Scraping finished*, press any key to close it.
- Each scan only reads posts newer than the previous scan, so frequent scans are quick.
- **Close `listings.xlsx` in Excel while a scan runs** — Excel locks the file. If it's open, the bot
  warns you and saves the waiting rows on its next write.
- To scan automatically, add [daily scans](#b-daily-automatic-scans).

## Reading the results

### "Facebook" tab — one row per apartment, newest post first

| Column | Meaning |
|---|---|
| לינק למודעה | the original post — click to open it |
| מחיר | monthly rent, ₪ |
| חדרים | rooms |
| מרחק הליכה (ק"מ) | walking distance to your destination, km |
| תאריך כניסה | entry date, when the post says |
| קומה | floor (0 = ground) |
| מעלית / חניה / ממ"ד/מקלט | elevator / parking / safe room |
| ארנונה / ועד בית | municipal tax / building fee, per two months |
| תיווך/פרטי | agent or private |
| תאריך פרסום | when it was posted |
| כתובת | the address as the post gives it |
| זמן סריקה | when the bot found it |
| ציון התאמה | **fit score 0-100** — higher is better |
| עלות חודשית כוללת | total per month: rent + tax + building fee (+ an agent fee spread over 2 years) |
| דיוק מרחק | `Google` = a real walking route; `הערכה ±0.4` = a free estimate, give or take 0.4 km |

- A blank cell means the post didn't say.
- The same apartment posted in several groups appears once; apartments older than 14 days are removed.
- **Fit score** weighs price (cheaper is better), distance (closer), how soon you can move in,
  floor/elevator/safe room, rooms and private parking. It's for deciding what to look at first, not a filter.

## Managing your groups

The **Groups** tab is the list the bot scans. Edit it in Excel while the bot isn't running.

| Column | Who fills it | Meaning |
|---|---|---|
| קישור לקבוצה | you | the group's link — the only thing needed to add a group (new row) |
| שם הקבוצה, ערים | you, or the bot | filled from the group's name if you leave them blank |
| פעיל | you | **לא** pauses the group; blank = active |
| הערות | you | your notes — ignored by the bot |
| the rest | the bot | posts scanned, apartments added, match %, last scan, status |

| Status | Meaning / what to do |
|---|---|
| תקין | all good |
| לא חבר/ה בקבוצה | the account hasn't joined — join on Facebook, or pause the group |
| בקשת הצטרפות נשלחה | waiting for the group's admins to approve you |
| לא נמצאו פוסטים | check the link opens the group; it may be empty |
| פייסבוק דורש התחברות | you're in no-login mode — log in (step 7) |
| ממתינה לסריקה ראשונה | added but not scanned yet |

A group with hundreds of posts and no matches is worth pausing — it only slows scans down.

---

# Part 3 — Add-ons (optional, each on its own)

Each add-on is independent: add any, in any order, any time. To run one, open a **Command Prompt in
the bot's folder** (in File Explorer, click the address bar, type `cmd`, press Enter) and run its
command. To turn an add-on off, open `config_local.py` in Notepad and change its line as shown.

| Add-on | Time | Needs | Command |
|---|---|---|---|
| [A. Telegram](#a-telegram-alerts-and-chat-commands) | 5 min | Telegram app | `venv\Scripts\python setup_wizard.py --add telegram` |
| [B. Daily scans](#b-daily-automatic-scans) | 1 min | — | `venv\Scripts\python setup_wizard.py --add schedule` |
| [C. Yad2 emails](#c-yad2-email-alerts) | 10 min | Gmail with 2-step verification | `venv\Scripts\python setup_wizard.py --add yad2` |
| [D. Google Sheet](#d-google-sheet-instead-of-excel) | 15 min | Google account | `venv\Scripts\python setup_wizard.py --add results` |
| [E. Google Maps](#e-google-maps-walking-routes) | 15 min | Google account + payment card | `venv\Scripts\python setup_wizard.py --add maps` |

---

## A. Telegram alerts and chat commands

**What you get:** a Telegram message for every new apartment (price, rooms, distance, total cost,
buttons to open the post, open the address in Maps and WhatsApp the poster), a start/finish message
for each scan with a live progress bar, and commands to run the bot from your phone.
**You need:** the Telegram app.

### Set it up

1. Run `venv\Scripts\python setup_wizard.py --add telegram`. It opens @BotFather.
2. **Create your bot** in Telegram:
   1. In the chat with **@BotFather**, tap **Start**, then send `/newbot`.
   2. Send a display name, e.g. `Apartment finder`.
   3. Send a username ending in `bot`, e.g. `dana_apartments_bot` (must be unique).
   4. BotFather replies with a **token** like `7123456789:AAH...`. Copy it.
3. Paste the token into the wizard (hidden) and press Enter. *"Telegram didn't accept that token"* →
   copy the whole token again.
4. **Choose the chat** that gets the alerts — the wizard waits up to 2 minutes:
   - **Just you:** open your new bot in Telegram (tap the `t.me/...` link BotFather sent), tap
     **Start**, send any message.
   - **A group with a partner/roommates:** create a Telegram group, add the bot to it (group → name →
     **Add members** → search the bot's username), then send `/start` in the group.
5. The wizard says *"Got it: ..."* and sends **"✅ הבוט מחובר"** to that chat.
6. *"Also answer chat commands whenever this PC is on?"* → `y` to use commands (below). This adds a
   small background program that starts with Windows.

### Check it works

In the chat, send `/help` — a menu of buttons appears. Tap **ℹ️ סטטוס**.

### Use it

| Command | What it does |
|---|---|
| /scan | full scan now (the start message has a ⏹ stop button) |
| /scan_fb, /scan_yad2 | Facebook groups only / Yad2 emails only |
| /scan_group | choose one group from a list to scan |
| /stop | stop the running scan |
| /status | is a scan running, how far along, last scan result |
| /new | apartments added in the last 24 hours |
| /groups | groups that need attention (not joined, no posts, never match) |
| /pause, /resume | pause / resume the daily scans (away, or found a place) |
| /stats | totals and the last scans |
| /doctor | health check |

Commands work only from the chat you connected, and only while the PC is on.

### Turn it off

In `config_local.py`: `TELEGRAM_ENABLED = False`. To stop the background program from starting with
Windows: Win+R → `shell:startup` → delete *"Apartment bot Telegram listener"*.

---

## B. Daily automatic scans

**What you get:** scans that run by themselves at fixed times, no window popping up.
**You need:** nothing — but the PC must be on (or it catches up after waking), and for logged-in mode
the Facebook login from step 7.

### Set it up

1. Run `venv\Scripts\python setup_wizard.py --add schedule`.
2. Type the times, 24-hour, separated by commas, e.g. `09:00, 13:00, 20:00` (Enter keeps `11:00, 20:00`).
3. Each time prints *"ApartmentBot 0900: scheduled"*.

### Check it works

Open **Task Scheduler** (Start menu → type "Task Scheduler") → **Task Scheduler Library** → tasks
named *ApartmentBot 0900* etc. Right-click one → **Run** to test it now; results appear in
`listings.xlsx`, and a log in the `logs` folder.

### Use it

- Change a time: Task Scheduler → the task → **Properties → Triggers → Edit**, or run the wizard step
  again with new times (then delete the old tasks).
- Pause everything for a while: `/pause` in Telegram (add-on A), or right-click the tasks → **Disable**.

### Turn it off

Task Scheduler → right-click each *ApartmentBot ...* task → **Delete**.

---

## C. Yad2 email alerts

**What you get:** Yad2 listings too — Yad2 emails a daily digest of new listings for a saved search,
and the bot reads those emails into a separate **Yad2** tab, with the same filters, distance and score.
**You need:** a Gmail account with **2-step verification** turned on (needed for an app password).

### Set it up

1. **Save a search on Yad2 with email alerts:**
   1. On [yad2.co.il](https://www.yad2.co.il), sign in, search apartments for rent with your filters
      (city, rooms, price).
   2. Click **שמירת חיפוש** (save search) and turn on **email** alerts, sent to your Gmail address.
2. **Put the Yad2 emails under a Gmail label** (so the bot reads only those):
   1. In Gmail, click the search options icon (the sliders at the right of the search box).
   2. **From:** `alerts@yad2.co.il` → **Create filter**.
   3. Tick **Apply the label** → **New label** → name it `yad2` → **Create filter**.
3. **Create a Gmail app password** (a separate password just for the bot; your real password is never used):
   1. Make sure 2-step verification is on: [myaccount.google.com/security](https://myaccount.google.com/security).
   2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (the wizard opens it).
   3. Name it `apartment bot` → **Create**. Copy the 16-letter password.
4. Run `venv\Scripts\python setup_wizard.py --add yad2` and answer:
   - **Gmail address** — e.g. `dana@gmail.com`
   - **Gmail label** — `yad2` (exactly as you named it)
   - **App password** — paste it (spaces don't matter; hidden while pasting)
5. The wizard logs in to test it. *"Couldn't read the mailbox"* tells you why: a wrong app password,
   or *"no label named ..."* if the label name differs.

The bot opens the mailbox **read-only** — it never marks, moves or deletes email.

### Check it works

Run a scan (or `/scan_yad2` in Telegram). The scan says *"Yad2 alert emails: N listing(s) read"*, and
a **Yad2** tab appears in the results. Each scan reads the last 2 days of emails.

### Turn it off

In `config_local.py`: `ALERTS_SCAN_ENABLED = False`. Revoke the app password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

---

## D. Google Sheet instead of Excel

**What you get:** results in a Google Sheet — open on your phone, share with a partner, always up to
date. Same tabs and columns as the Excel file.
**You need:** a Google account. Google Cloud is free for this (no payment card).

### Set it up

1. **Create a Google Cloud project and turn on the Sheets API:**
   1. Open [console.cloud.google.com](https://console.cloud.google.com/) and sign in. Accept the terms if asked.
   2. Top bar → project picker → **New project** → name it `apartment-bot` → **Create** → select it.
   3. Open [the Google Sheets API page](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
      (the wizard opens it) → **Enable**.
2. **Create a service account** (a robot Google user the bot writes as):
   1. Left menu → **IAM & Admin → Service Accounts** → **Create service account**.
   2. Name: `apartment-bot` → **Create and continue** → skip the roles → **Done**.
3. **Download its key:**
   1. Click the new service account → **Keys** tab → **Add key → Create new key → JSON → Create**.
   2. A `.json` file downloads. Keep it private — it's a password for that robot account.
4. **Create the sheet:** at [sheets.new](https://sheets.new), create a blank sheet (name it e.g. "Apartments").
5. Run `venv\Scripts\python setup_wizard.py --add results`, type `2`, then:
   1. **Path of the downloaded JSON key** — e.g. `C:\Users\Dana\Downloads\apartment-bot-1234.json`
      (in File Explorer: Shift+right-click the file → **Copy as path**, then paste). The wizard copies it
      into the bot's folder as `credentials.json`.
   2. The wizard prints an email like `apartment-bot@apartment-bot-1234.iam.gserviceaccount.com`.
      In your sheet: **Share** → paste that email → **Editor** → untick "Notify" → **Share**.
   3. **Paste the sheet's link** (from the browser address bar).
6. The wizard connects: *"Connected to the sheet"*. If it says *"Couldn't open the sheet yet"*, the
   sheet usually isn't shared with that email as Editor — fix it and run the health check.

Your groups move with you: the first scan fills the sheet's empty Groups tab from the bot's own copy
of your group list.

### Check it works

`venv\Scripts\python doctor.py` → *google_sheets: PASS*. Run a scan; the sheet fills in.

### Turn it off

Run `venv\Scripts\python setup_wizard.py --add results` and choose `1` (Excel).

---

## E. Google Maps walking routes

**What you get:** the real walking distance by Google Maps (shown as `Google` in the דיוק מרחק column)
instead of the free estimate (shown as `הערכה ±0.4`). Optional — the estimate is good enough for most.
**You need:** a Google account and a payment card. Google gives a monthly free credit; the bot stops
itself at 9,000 lookups a month, inside it, so you shouldn't be charged.

### Set it up

1. In [console.cloud.google.com](https://console.cloud.google.com/), select (or create) a project.
2. **Turn on billing:** left menu → **Billing** → link a billing account (add the card).
3. **Enable two APIs** (left menu → **APIs & Services → Library**, search each → **Enable**):
   - **Distance Matrix API**
   - **Geocoding API**
4. **Create the key:** **APIs & Services → Credentials → Create credentials → API key**. Copy it.
5. **Restrict the key** (recommended): click it → **API restrictions → Restrict key** → tick Distance
   Matrix API and Geocoding API → **Save**.
6. **Optional safety cap:** **APIs & Services → Distance Matrix API → Quotas** → lower *Elements per
   day* to ~300.
7. Run `venv\Scripts\python setup_wizard.py --add maps` and paste the key.

### Check it works

After the next scan, new rows show `Google` in the **דיוק מרחק** column. The scan's first lines say
how many Maps lookups were used this month.

### Turn it off

In `config_local.py`: `GMAPS_ENABLED = False`. Delete the key in Google Cloud if you won't use it again.

---

# Part 4 — Changing settings, troubleshooting, questions

## Changing your settings

- **Everything again:** `venv\Scripts\python setup_wizard.py` (Enter keeps each current answer).
- **One part:** `venv\Scripts\python setup_wizard.py --add <step>` — steps: `search` (rooms, rent,
  distance, destination), `ai`, `results`, `facebook` (log in again / add groups), and the add-ons
  `telegram`, `schedule`, `yad2`, `maps`.
- **Advanced:** every setting is in `config.py`, each with a comment. Don't edit that file — copy the
  line into `config_local.py` (your settings) and change it there; it's kept when you update the bot.
- Your keys and passwords are in `.env` in the bot's folder. Don't share it.

## Troubleshooting

Start with the health check: `venv\Scripts\python doctor.py`. Each line is **PASS**, **WARN**,
**FAIL** (with how to fix it), or **OFF** (a feature you didn't turn on — fine).

| Problem | Fix |
|---|---|
| `install.bat`: Python is missing | install Python with "Add python.exe to PATH" ticked (step 1), run `install.bat` again |
| `install.bat`: install failed | check the internet connection, run `install.bat` again |
| "listings.xlsx is open in another program" | close it in Excel; the waiting rows save on the next write |
| "Facebook session is gone" / login required | `venv\Scripts\python setup_wizard.py --add facebook`, log in again |
| Facebook asks for a security check (CAPTCHA) | `run_bot.bat --headful`, solve it in the window that opens |
| "setup incomplete" | a turned-on feature is missing a key — the message lists it; run its wizard step |
| Groups show "לא חבר/ה בקבוצה" | join them on Facebook with the account the bot uses |
| No apartments at all | widen rent/rooms (`--add search`); check the Groups tab statuses |
| Dates missing / "unrecognized Facebook post-date format" | switch Facebook to English (Before you start) |
| Ollama: "local Ollama analysis failed" | open Ollama from the Start menu and scan again; if it repeats, reinstall it (step 5) |
| Telegram: no messages | `doctor.py` → telegram line; make sure the bot is still in the chat |
| Anything else | the newest file in the `logs` folder — its last lines usually say what went wrong |

## Questions

**Is my Facebook account at risk?** The bot reads slowly, like a person scrolling, and never posts.
Facebook may still ask for a security check now and then (solve it as above). Fewer groups and fewer
daily scans lower the chance.

**What's stored, and where?** Everything stays in the bot's folder: results, a database of posts already
seen (`bot_data.db`, so a post is never read twice), logs, and the bot browser's Facebook login
(`chrome_profile`). Post text goes only to the AI you chose (Google's Gemini, or nowhere with Ollama),
and results only to your Google Sheet / Telegram chat if you added those.

**Does it cost anything?** No. Google Maps is the only add-on asking for a card, and the bot caps itself
inside Google's free credit.

**Can two people use it?** Yes — a Telegram group chat (add-on A) or a shared Google Sheet (add-on D).

**Can I add my own features?** Yes — see [extending.md](extending.md).

## Updating

Download the new version (or `git pull`) into the same folder, then run `install.bat` again. Your
settings and data stay: `config_local.py`, `.env`, `credentials.json`, `listings.xlsx`, `bot_data.db`
and `chrome_profile` aren't part of the download — when unzipping over the folder, don't delete them.

## Uninstalling

1. Daily scans: Task Scheduler → delete the *ApartmentBot ...* tasks.
2. Telegram commands: Win+R → `shell:startup` → delete *"Apartment bot Telegram listener"*.
3. Delete the bot's folder.
