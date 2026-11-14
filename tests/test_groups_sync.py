"""
Tests for the Groups tab sync: parsing what the user typed, the local mirror, filling blanks from what
scans learned without overwriting the user's cells, falling back when the sheet can't be read, and
writing stats back. The worksheet is faked (Sheets API is a network boundary); storage is a real temp DB.
"""
import pytest

from integrations import groups_sync
from core import storage

_G1 = "https://www.facebook.com/groups/111"
_G2 = "https://www.facebook.com/groups/222"


class FakeWorksheet:
    title = "Groups"

    def __init__(self, values):
        self.values = values
        self.written = None

    def get_all_values(self):
        return self.values

    def update(self, range_name, values, value_input_option):
        self.written = values

    def batch_clear(self, ranges):
        pass


class FakeSpreadsheet:
    def __init__(self, worksheet=None, fail=False):
        self.worksheet, self.fail = worksheet, fail

    def worksheets(self):
        if self.fail:
            raise RuntimeError("sheet unreachable")
        return [self.worksheet]


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


def _tab(*rows):
    return FakeWorksheet([groups_sync.HEADERS] + [list(r) for r in rows])


def test_a_pasted_link_with_query_string_becomes_the_canonical_group_url():
    assert groups_sync.canonical_group_url(_G1 + "/?ref=share") == _G1


def test_a_bare_group_id_is_accepted():
    assert groups_sync.canonical_group_url("222") == _G2


def test_a_non_group_facebook_link_is_rejected():
    assert groups_sync.canonical_group_url("https://www.facebook.com/marketplace/") is None


def test_paused_groups_are_not_scanned():
    sheet = FakeSpreadsheet(_tab([_G1, "", "", "כן"], [_G2, "", "", "לא"]))

    assert groups_sync.sync_groups(sheet, write=False) == [_G1]


def test_the_local_mirror_matches_the_tab_including_removals():
    storage.replace_groups([{"url": _G1}, {"url": _G2}])

    groups_sync.sync_groups(FakeSpreadsheet(_tab([_G2])), write=False)

    assert [g["url"] for g in storage.get_groups()] == [_G2]


def test_unreachable_sheet_falls_back_to_the_local_mirror():
    storage.replace_groups([{"url": _G1, "active": True}, {"url": _G2, "active": False}])

    assert groups_sync.sync_groups(FakeSpreadsheet(fail=True)) == [_G1]


def test_blank_name_is_filled_from_the_scanned_page_title():
    storage.save_group_city_hint(_G1, "דירות להשכרה ברמת גן | Facebook", ["רמת גן"])
    tab = _tab([_G1])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert tab.written[1][1:3] == ["דירות להשכרה ברמת גן", "רמת גן"]


def test_a_name_the_user_typed_is_kept():
    storage.save_group_city_hint(_G1, "שם מפייסבוק | Facebook", ["רמת גן"])
    tab = _tab([_G1, "השם שלי"])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert tab.written[1][1] == "השם שלי"


def test_stats_columns_show_posts_and_matches():
    storage.record_post(_G1 + "/posts/1/", _G1, "t", storage.VERDICT_ADDED)
    storage.record_post(_G1 + "/posts/2/", _G1, "t", storage.VERDICT_PREFILTERED)
    tab = _tab([_G1])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert tab.written[1][5:8] == [2, 1, "50.0%"]


def test_an_empty_tab_is_seeded_from_target_urls(monkeypatch):
    monkeypatch.setattr(groups_sync.config, "TARGET_URLS", [_G1 + "/", _G2])

    assert groups_sync.sync_groups(FakeSpreadsheet(FakeWorksheet([[]])), write=False) == [_G1, _G2]


def test_a_dry_run_never_writes_the_tab():
    tab = _tab([_G1])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=False)

    assert tab.written is None


def test_a_row_with_only_a_link_is_an_active_group():
    assert groups_sync.sync_groups(FakeSpreadsheet(_tab([_G1])), write=False) == [_G1]


def test_a_new_link_only_group_shows_it_awaits_its_first_scan():
    tab = _tab([_G1])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert tab.written[1][-1] == "ממתינה לסריקה ראשונה"


def test_a_link_pasted_with_invisible_direction_marks_is_accepted():
    assert groups_sync.canonical_group_url("\u200f" + _G1 + "\u200e") == _G1


def test_the_same_group_under_two_link_shapes_is_written_once():
    tab = _tab([_G1], [_G1 + "/?ref=share"])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert [row[0] for row in tab.written[1:]] == [_G1]


def test_notes_typed_on_a_duplicate_row_are_kept():
    tab = _tab([_G1], ["111", "", "", "", "קבוצה טובה"])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert tab.written[1][4] == "קבוצה טובה"


def test_a_group_the_account_has_not_joined_is_flagged():
    storage.set_group_status(_G1, "not_member")
    tab = _tab([_G1])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert tab.written[1][-1] == "לא חבר/ה בקבוצה — יש להצטרף בפייסבוק"


_G3 = "https://www.facebook.com/groups/333"


def _record_posts(group_url, total, added):
    for i in range(total):
        verdict = storage.VERDICT_ADDED if i < added else storage.VERDICT_PREFILTERED
        storage.record_post(f"{group_url}/posts/{i}/", group_url, "", verdict)


def test_sync_command_sorts_groups_by_match_rate_with_unscanned_groups_last():
    _record_posts(_G1, total=10, added=1)
    _record_posts(_G2, total=10, added=3)
    tab = _tab([_G3], [_G1], [_G2])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True, sort_by_yield=True)

    assert [row[0] for row in tab.written[1:]] == [_G2, _G1, _G3]


def test_a_regular_sync_keeps_the_tab_order():
    _record_posts(_G2, total=10, added=3)
    tab = _tab([_G1], [_G2])

    groups_sync.sync_groups(FakeSpreadsheet(tab), write=True)

    assert [row[0] for row in tab.written[1:]] == [_G1, _G2]


def test_add_groups_appends_a_new_group_after_the_listed_ones(db):
    worksheet = FakeWorksheet([groups_sync.HEADERS, [_G1]])

    groups_sync.add_groups(FakeSpreadsheet(worksheet), ["https://m.facebook.com/groups/222/?ref=share"])

    assert [row[0] for row in worksheet.written[1:]] == [_G1, _G2]


def test_add_groups_skips_a_group_already_listed_under_another_link_shape(db):
    worksheet = FakeWorksheet([groups_sync.HEADERS, [_G1]])

    assert groups_sync.add_groups(FakeSpreadsheet(worksheet), [_G1 + "/?ref=share"]) == []


def test_pausing_a_group_writes_no_in_its_active_column(db):
    worksheet = FakeWorksheet([groups_sync.HEADERS, [_G1], [_G2]])

    groups_sync.set_group_active(FakeSpreadsheet(worksheet), _G2, False)

    assert worksheet.written[2][groups_sync.HEADERS.index("פעיל")] == "לא"


def test_removing_a_group_drops_its_row(db):
    worksheet = FakeWorksheet([groups_sync.HEADERS, [_G1], [_G2]])

    groups_sync.remove_group(FakeSpreadsheet(worksheet), _G1)

    assert [row[0] for row in worksheet.written[1:]] == [_G2]


def test_added_per_day_counts_only_added_listings(db):
    storage.record_post("https://x/1", _G1, "text", storage.VERDICT_ADDED)
    storage.record_post("https://x/2", _G1, "text", storage.VERDICT_ADDED)
    storage.record_post("https://x/3", _G1, "text", storage.VERDICT_REJECTED_PRICE)

    assert list(storage.get_added_per_day(7).values()) == [2]
