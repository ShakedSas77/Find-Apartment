"""
Tests for maps.py's distance pipeline. Storage is a real temp DB; only the network is
faked (the Google Maps client and the free Nominatim geocoder).
"""
import pytest

import config
from core import storage
from integrations import distance_fallback, maps
from integrations.distance_fallback import spelling_variants
from integrations.maps import _anchor_default_city


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "bot_data.db")
    storage.init_db()


class _FakeGmapsClient:
    def __init__(self, lat):
        self.lat = lat

    def geocode(self, query, language, region):
        return [{
            "formatted_address": "נחלת יצחק 10, תל אביב-יפו, ישראל",
            "types": ["street_address"],
            "address_components": [{"long_name": "תל אביב-יפו", "types": ["locality"]}],
            "geometry": {"location": {"lat": self.lat, "lng": 34.79}},
        }]

    def distance_matrix(self, **kwargs):
        pytest.fail("a south-excluded address must not reach Distance Matrix")


def test_tel_aviv_focus_neighborhood_anchors_to_tel_aviv():
    assert _anchor_default_city("נחלת יצחק") == "נחלת יצחק, תל אביב, ישראל"


def test_unnamed_city_still_defaults_to_ramat_gan_givatayim():
    assert _anchor_default_city("הרצל 5") == "הרצל 5, רמת גן, גבעתיים, ישראל"


def test_tel_aviv_focus_neighborhood_south_of_the_cutoff_is_excluded(monkeypatch):
    monkeypatch.setattr(maps, "GMAPS_ENABLED", True)
    monkeypatch.setattr(maps, "get_gmaps_client", lambda: _FakeGmapsClient(lat=config.EXCLUDE_SOUTH_OF_LAT - 0.01))

    assert maps.get_walking_distance("נחלת יצחק 10, תל אביב")[4] == "excluded_south"


def test_a_cached_failed_lookup_is_retried(monkeypatch):
    address = "רמב\"ם 3, גבעתיים"
    storage.save_address_cache(address, address, "גבעתיים", "high", "", "", 999999, "quota", "quota", None, None)
    monkeypatch.setattr(maps, "GMAPS_ENABLED", False)
    monkeypatch.setattr(distance_fallback, "nominatim_geocode", lambda q: (32.0717, 34.8125, 30))

    assert maps.get_walking_distance(address)[4] == "straight_line_estimate"


def test_a_cached_real_distance_is_served_without_a_new_lookup(monkeypatch):
    address = "ביאליק 14, רמת גן"
    storage.save_address_cache(address, address, "רמת גן", "high", "", "2.1", 2100, "google_maps", "ok", None, None)
    monkeypatch.setattr(maps, "get_gmaps_client", lambda: pytest.fail("a cached answer must not be looked up again"))

    assert maps.get_walking_distance(address)[0] == "2.1"


def test_spelling_variants_add_gershayim_to_an_acronym_street():
    assert 'רמב"ם 3, גבעתיים' in spelling_variants("רמבם 3, גבעתיים")


def test_spelling_variants_drop_the_street_prefix_and_normalize_quotes():
    assert 'אצ"ל 13, רמת גן' in spelling_variants("רח אצ״ל 13, רמת גן")


def test_spelling_variants_end_with_the_street_without_its_house_number():
    assert spelling_variants("ביאליק 14, רמת גן") == ["ביאליק 14, רמת גן", "ביאליק, רמת גן"]


def test_fallback_tries_variants_only_after_the_plain_query_misses(monkeypatch):
    queried = []
    monkeypatch.setattr(distance_fallback, "nominatim_geocode",
                        lambda q: queried.append(q) or ((32.07, 34.80, 30) if '"' in q else None))

    distance_fallback.estimate_straight_line_distance("רמבם 3, גבעתיים")

    assert queried == ["רמבם 3, גבעתיים", 'רמב"ם 3, גבעתיים']



def _geocode_everything_at(lat, lon, rank, monkeypatch):
    monkeypatch.setattr(maps, "GMAPS_ENABLED", False)
    monkeypatch.setattr(distance_fallback, "nominatim_geocode", lambda q: (lat, lon, rank))


def test_an_address_found_only_at_street_level_is_marked_a_street_estimate(monkeypatch):
    _geocode_everything_at(32.0717, 34.8125, 26, monkeypatch)

    assert maps.get_walking_distance("שינקין, גבעתיים")[4] == "street_estimate"


def test_a_neighbourhood_is_estimated_when_google_is_off(monkeypatch):
    _geocode_everything_at(32.0717, 34.8125, 20, monkeypatch)

    assert maps.get_walking_distance("שכונת רמת יצחק")[4] == "area_estimate"


def test_a_match_for_a_whole_city_gives_no_estimate(monkeypatch):
    _geocode_everything_at(32.0717, 34.8125, 16, monkeypatch)

    assert distance_fallback.estimate_straight_line_distance("משהו, גבעתיים") is None


def test_a_street_found_by_google_only_in_general_is_retried_while_google_is_off(monkeypatch):
    address = "שינקין, גבעתיים"
    storage.save_address_cache(address, "", "גבעתיים", "low", "", "", 999999, "skipped", "not_street_precision", None, None)
    _geocode_everything_at(32.0717, 34.8125, 26, monkeypatch)

    assert maps.get_walking_distance(address)[4] == "street_estimate"


def test_estimate_error_grows_with_the_distance():
    assert distance_fallback.estimate_error_km("straight_line_estimate", 3.0) == 0.5


def test_a_neighbourhood_estimate_has_a_wider_error_than_a_house():
    assert distance_fallback.estimate_error_km("area_estimate", 3.0) == 1.1


def test_a_google_distance_has_no_estimate_error():
    assert distance_fallback.estimate_error_km("google_maps", 3.0) is None


def test_the_sheet_label_marks_an_estimate_with_its_error():
    assert distance_fallback.distance_accuracy_label("street_estimate", 0.7) == "הערכה ±0.7"


def test_the_estimate_error_is_read_back_from_the_sheet_label():
    assert distance_fallback.error_km_from_label("הערכה ±0.7") == 0.7
