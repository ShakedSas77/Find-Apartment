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
    monkeypatch.setattr(distance_fallback, "nominatim_geocode", lambda q: (32.0717, 34.8125))

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
                        lambda q: queried.append(q) or ((32.07, 34.80) if '"' in q else None))

    distance_fallback.estimate_straight_line_distance("רמבם 3, גבעתיים")

    assert queried == ["רמבם 3, גבעתיים", 'רמב"ם 3, גבעתיים']
