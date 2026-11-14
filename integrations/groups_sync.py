"""
=== Facebook groups managed from the sheet's "Groups" tab ===

The Groups tab is the source of truth for which groups a scrape covers. The
user adds a group by pasting its link (or bare id) in a new row, pauses it with
פעיל = "לא", or deletes the row to drop it. The bot fills the name and cities
from the group's FB page title once it has scanned it (only when those cells
are blank — the user's own wording is never overwritten) and refreshes the
per-group stats columns from bot_data.db.

A local mirror (storage `groups` table) is kept in sync on every read, so a run
still has its group list when the sheet is unreachable. If both are empty, the
tab is seeded from config.TARGET_URLS (the pre-sheet group list).
"""
import re

import config
from core import storage
from core.normalize import BIDI_RE, _sheet_safe_cell
from core.util import _with_retries

TAB_NAME = "Groups"
HEADERS = ["קישור לקבוצה", "שם הקבוצה", "ערים", "פעיל", "הערות",
           "פוסטים שנסרקו", "דירות שנוספו", "אחוז התאמה", "דירה אחרונה שנוספה", "סריקה אחרונה", "סטטוס"]
_COL = {name: i for i, name in enumerate(HEADERS)}
_INACTIVE_VALUES = {"לא", "no", "false", "0", "x"}
# The latest scan outcome (storage.set_group_status), as the user should read it.
_STATUS_TEXT = {"ok": "תקין", "not_member": "לא חבר/ה בקבוצה — יש להצטרף בפייסבוק", "no_posts": "לא נמצאו פוסטים",
                "pending": "בקשת הצטרפות נשלחה — ממתינה לאישור",
                "needs_login": "פייסבוק דורש התחברות כדי לקרוא את הקבוצה"}
_GROUP_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*Facebook\s*$")


def sync_groups(spreadsheet, write: bool = True, sort_by_yield: bool = False) -> list[str]:
    """
    Reads the Groups tab, mirrors it into the local DB and returns the active group URLs, in tab order.
    write=True also rewrites the tab with filled-in names/cities and fresh stats (a dry run passes False:
    it reads only). sort_by_yield=True (the --sync-groups command) also reorders the rows by match rate,
    best first, with groups not scanned yet at the bottom. Never raises for a sheet problem — falls back
    to the local mirror, then TARGET_URLS.
    """
    try:
        worksheet = _groups_worksheet(spreadsheet)
        groups = parse_group_rows(_with_retries(worksheet.get_all_values))
    except Exception as e:
        print(f"WARNING: could not read the '{TAB_NAME}' sheet tab ({e}) — using the local group list.")
        return _active_urls(storage.get_groups() or _seed_from_config())

    if not groups:
        groups = storage.get_groups() or _seed_from_config()
        print(f"'{TAB_NAME}' tab is empty — seeding it with {len(groups)} group(s).")
    overview = storage.get_group_overview()
    for group in groups:
        _fill_from_scans(group, overview.get(group["url"], {}))
    if sort_by_yield:
        groups.sort(key=lambda g: _yield_sort_key(overview.get(g["url"], {})))
    storage.replace_groups(groups)
    if write:
        try:
            _write_tab(worksheet, groups, overview)
        except Exception as e:
            print(f"WARNING: could not update the '{TAB_NAME}' sheet tab: {e}")
    return _active_urls(groups)


def canonical_group_url(raw: str) -> str | None:
    """A group link in any shape ("https://www.facebook.com/groups/123/?ref=..", "facebook.com/groups/x",
    or a bare id/slug) -> "https://www.facebook.com/groups/<id>", the form stored throughout the DB."""
    # Links copied from FB's Hebrew UI often carry invisible direction marks — strip them first.
    text = BIDI_RE.sub("", raw or "").strip().split("?")[0].split("#")[0]
    if "/groups/" in text:
        group_id = text.split("/groups/", 1)[1].split("/")[0]
    elif "facebook.com" in text or "/" in text.strip("/"):
        return None
    else:
        group_id = text.strip("/")
    return f"https://www.facebook.com/groups/{group_id}" if _GROUP_ID_RE.fullmatch(group_id) else None


def add_groups(spreadsheet, links: list[str]) -> list[str]:
    """
    Appends the groups in `links` (any link shape) to the Groups tab as active rows, after the ones
    already listed; a group already there, or a link that isn't a group, is skipped. Returns the
    URLs added. Used by setup_wizard.py.
    """
    added = []

    def append_new(groups):
        known = {g["url"] for g in groups}
        added.extend(url for url in dict.fromkeys(canonical_group_url(link) for link in links) if url and url not in known)
        return groups + [{"url": url, "name": "", "cities": "", "active": True, "notes": ""} for url in added]

    _edit_groups(spreadsheet, append_new)
    return added


def set_group_active(spreadsheet, url: str, active: bool):
    """Pauses (active=False writes "לא" in פעיל) or resumes the group with this canonical URL."""
    _edit_groups(spreadsheet, lambda groups: [dict(g, active=active) if g["url"] == url else g for g in groups])


def remove_group(spreadsheet, url: str):
    """Drops the group's row from the Groups tab. Its posts and stats stay in the local DB."""
    _edit_groups(spreadsheet, lambda groups: [g for g in groups if g["url"] != url])


def _edit_groups(spreadsheet, change):
    worksheet = _groups_worksheet(spreadsheet)
    groups = change(parse_group_rows(_with_retries(worksheet.get_all_values)) or storage.get_groups())
    storage.replace_groups(groups)
    _write_tab(worksheet, groups, storage.get_group_overview())


def parse_group_rows(values: list[list[str]]) -> list[dict]:
    """
    Sheet rows (header first) -> groups. Rows without a usable link are skipped. Duplicates — the same
    group under any link shape — collapse into the first row: its cells win, a blank one is filled from
    the duplicate (so notes typed on a second copy aren't lost), and the extra row is reported and
    dropped on the next write.
    """
    groups, by_url = [], {}
    for line, row in enumerate(values[1:], start=2):
        cell = lambda name: (row[_COL[name]] if len(row) > _COL[name] else "").strip()
        url = canonical_group_url(cell("קישור לקבוצה"))
        if not url:
            continue
        entry = {"url": url, "name": cell("שם הקבוצה"), "cities": cell("ערים"),
                 "active": cell("פעיל").lower() not in _INACTIVE_VALUES, "notes": cell("הערות")}
        first = by_url.get(url)
        if first is None:
            by_url[url] = entry
            groups.append(entry)
            continue
        print(f"WARNING: '{TAB_NAME}' row {line} duplicates {url} (already listed) — merged into the first row.")
        for field in ("name", "cities", "notes"):
            first[field] = first[field] or entry[field]
    return groups


def _fill_from_scans(group: dict, seen: dict):
    # Filled name/cities are the user's own edits; only blanks take what the scans learned.
    if not group.get("name") and seen.get("name"):
        group["name"] = _TITLE_SUFFIX_RE.sub("", seen["name"])
    if not group.get("cities") and seen.get("cities"):
        group["cities"] = ", ".join(seen["cities"])


def _yield_sort_key(seen: dict) -> tuple:
    # Same rate as the אחוז התאמה column; ties go to the group with more listings added.
    posts, added = seen.get("posts", 0), seen.get("added", 0)
    return (not posts, -(added / posts) if posts else 0, -added)


def _stats_cells(seen: dict) -> list:
    posts, added = seen.get("posts", 0), seen.get("added", 0)
    status = _STATUS_TEXT.get(seen.get("status"), "") or ("" if posts else "ממתינה לסריקה ראשונה")
    return [posts, added, f"{100 * added / posts:.1f}%" if posts else "",
            (seen.get("last_added") or "")[:10], (seen.get("last_scanned") or "")[:16].replace("T", " "), status]


def _write_tab(worksheet, groups: list[dict], overview: dict):
    rows = [HEADERS] + [
        [_sheet_safe_cell(v) for v in (g["url"], g["name"], g["cities"], "כן" if g["active"] else "לא", g["notes"])]
        + _stats_cells(overview.get(g["url"], {}))
        for g in groups
    ]
    old_row_count = len(_with_retries(worksheet.get_all_values))
    # Write first, then clear only the leftover rows below — a failed write must never leave the tab empty.
    _with_retries(lambda: worksheet.update(range_name="A1", values=rows, value_input_option="USER_ENTERED"))
    if old_row_count > len(rows):
        last_col = chr(ord("A") + len(HEADERS) - 1)
        _with_retries(lambda: worksheet.batch_clear([f"A{len(rows) + 1}:{last_col}{old_row_count}"]))


def _groups_worksheet(spreadsheet):
    for worksheet in spreadsheet.worksheets():
        if worksheet.title.strip().lower() == TAB_NAME.lower():
            return worksheet
    return spreadsheet.add_worksheet(title=TAB_NAME, rows=100, cols=len(HEADERS))


def _seed_from_config() -> list[dict]:
    urls = [canonical_group_url(u) for u in config.TARGET_URLS]
    return [{"url": u, "name": "", "cities": "", "active": True, "notes": ""} for u in dict.fromkeys(u for u in urls if u)]


def _active_urls(groups: list[dict]) -> list[str]:
    return [g["url"] for g in groups if g["active"]]
