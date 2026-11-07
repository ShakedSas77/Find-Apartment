import pytest



def test_tel_aviv_focus_neighborhood_anchors_to_tel_aviv():
    from maps import _anchor_default_city
    assert _anchor_default_city("נחלת יצחק") == "נחלת יצחק, תל אביב, ישראל"


def test_unnamed_city_still_defaults_to_ramat_gan_givatayim():
    from maps import _anchor_default_city
    assert _anchor_default_city("הרצל 5") == "הרצל 5, רמת גן, גבעתיים, ישראל"


def test_tel_aviv_focus_neighborhood_south_of_the_cutoff_is_excluded(monkeypatch):
    import config
    import maps

    monkeypatch.setattr(maps.storage, "get_address_cache", lambda address: None)
    monkeypatch.setattr(maps.storage, "save_address_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(maps, "_validate_address_with_geocoding",
                        lambda address, confidence: (address, "תל אביב", "high", "", "ok", config.EXCLUDE_SOUTH_OF_LAT - 0.01, 34.79))

    assert maps.get_walking_distance("נחלת יצחק, תל אביב")[4] == "excluded_south"


def _cached(distance_source, distance_text=""):
    return {"distance_text": distance_text, "distance_meters": 1500 if distance_text else 999999,
            "confidence": "high", "warning": "", "distance_source": distance_source, "lat": None, "lon": None}


def test_a_cached_failed_lookup_is_retried(monkeypatch):
    import maps

    monkeypatch.setattr(maps.storage, "get_address_cache", lambda address: _cached("quota"))
    monkeypatch.setattr(maps.storage, "save_address_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(maps, "_validate_address_with_geocoding",
                        lambda address, confidence: (address, "גבעתיים", "high", "", "quota", None, None))
    monkeypatch.setattr(maps, "_handle_gmaps_cap_if_needed", lambda: (True, ""))
    monkeypatch.setattr(maps.distance_fallback, "estimate_straight_line_distance", lambda address: ("1.4", 1400, 32.07, 34.81))

    assert maps.get_walking_distance("רמב\"ם 3, גבעתיים")[0] == "1.4"


def test_a_cached_real_distance_is_served_without_a_new_lookup(monkeypatch):
    import maps

    monkeypatch.setattr(maps.storage, "get_address_cache", lambda address: _cached("google_maps", "2.1"))
    monkeypatch.setattr(maps, "_validate_address_with_geocoding",
                        lambda address, confidence: pytest.fail("a cached answer must not be looked up again"))

    assert maps.get_walking_distance("ביאליק 14, רמת גן")[0] == "2.1"
