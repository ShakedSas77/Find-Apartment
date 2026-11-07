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


def test_spelling_variants_add_gershayim_to_an_acronym_street():
    from distance_fallback import spelling_variants

    assert 'רמב"ם 3, גבעתיים' in spelling_variants("רמבם 3, גבעתיים")


def test_spelling_variants_drop_the_street_prefix_and_normalize_quotes():
    from distance_fallback import spelling_variants

    assert 'אצ"ל 13, רמת גן' in spelling_variants("רח אצ״ל 13, רמת גן")


def test_spelling_variants_end_with_the_street_without_its_house_number():
    from distance_fallback import spelling_variants

    assert spelling_variants("ביאליק 14, רמת גן") == ["ביאליק 14, רמת גן", "ביאליק, רמת גן"]


def test_fallback_tries_variants_only_after_the_plain_query_misses(monkeypatch):
    import distance_fallback

    queried = []
    monkeypatch.setattr(distance_fallback, "nominatim_geocode",
                        lambda q: queried.append(q) or ((32.07, 34.80) if '"' in q else None))

    distance_fallback.estimate_straight_line_distance("רמבם 3, גבעתיים")

    assert queried == ["רמבם 3, גבעתיים", 'רמב"ם 3, גבעתיים']
